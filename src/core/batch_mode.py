"""
============================================================
batch_mode.py — Asynchronous Batch Image Processing
============================================================
Implements the "Submit, Exit, and Resume" lifecycle:

Phase 1 (Submit):
  - Pull batch_chunk_size Pending images → resize.
  - Upload to Gemini File API in parallel (ThreadPoolExecutor).
  - Build JSONL input file → upload JSONL.
  - Submit batch job → store in BatchJobs table.
  - Mark images as Processing → EXIT script.

Phase 2 (Resume & Poll):
  - Check for Running batch jobs in DB.
  - Poll Gemini API for job status.
  - If still Running → notify user, exit.
  - If SUCCEEDED → download output, parse, move files, cleanup.
  - If FAILED → revert images to Pending, cleanup uploads.

Crucial Cleanup:
  - Delete all temporary File API uploads after completion
    to free user quota.
============================================================
"""

import json
import logging
import os
import sys
import tempfile
import threading
import textwrap
import time
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Tuple

from google import genai
from google.genai import types
from tqdm import tqdm

from src.models.config import AppConfig
from src.utils.cost_tracker import CostTracker
from src.utils.database import (
    BATCH_FAILED,
    BATCH_RUNNING,
    BATCH_SUCCEEDED,
    Database,
)
from src.utils.file_mover import move_image, move_to_unprocessable
from src.utils.image_utils import extract_date, resize_image
from src.core.prompt_builder import build_batch_request
from src.utils.retry import retry_with_backoff

logger = logging.getLogger("whatsapp_sorter")


def run_batch_mode(
    config: AppConfig,
    db: Database,
    cost_tracker: CostTracker,
    test_mode: bool = False,
    dry_run: bool = False,
) -> int:
    """
    Process images in Batch (asynchronous) mode.

    Lifecycle:
      1. Check for existing Running batch jobs → resume/poll. If any are STILL running, exit.
      2. If all running jobs succeeded/failed OR no jobs existed → submit new jobs.

    Returns:
        Total number of images successfully processed.
    """
    logger.info("Starting Batch mode")

    # ── Initialise the Gemini client ─────────────────────────
    client = None
    if not dry_run:
        client = genai.Client(api_key=config.gemini_api_key)

    # ── Phase 1 (Resume) ─────────────────────────────────────────
    running_jobs = db.get_running_batch_jobs()
    total_processed = 0

    if running_jobs:
        logger.info(
            "Found %d running/pending batch job(s) from previous runs — polling status first",
            len(running_jobs),
        )
        for job in running_jobs:
            processed = _resume_batch_job(
                client=client,
                config=config,
                db=db,
                cost_tracker=cost_tracker,
                job=job,
                dry_run=dry_run,
            )
            total_processed += processed
        
        # If any jobs remain running, exit to wait for them. Don't start new queue iterations.
        if db.get_running_batch_jobs():
            logger.info("Some batch jobs are still running. Exiting to wait for completion before submitting new ones.")
            return total_processed
        else:
            logger.info("All previous batch jobs have resolved. Proceeding to pending queue.")

    # ── Phase 2 (Submit) ─────────────────────────────────────────
    # We attempt to submit any pending images into the batch queue.
    # The `_submit_batch_job` function will pull `config.batch_chunk_size` images at a time.
    while True:
        submitted = _submit_batch_job(
            client=client,
            config=config,
            db=db,
            test_mode=test_mode,
            dry_run=dry_run,
        )
        if not submitted or test_mode:
            break
            
    # ── Post-Submit ──────────────────────────────────────────────
    new_jobs = db.get_running_batch_jobs()
    if new_jobs:
        logger.info("New batch job(s) submitted successfully. They are currently processing.")
        logger.info("Run the script again later to check their status and collect results.")

    return total_processed


# ── Helper: Single image upload (thread-safe) ───────────────

def _upload_single_image(
    client: genai.Client,
    config: AppConfig,
    row: Dict,
    label: str,
) -> Optional[Dict]:
    """
    Resize and upload a single image to the Gemini File API.

    Thread-safe. Called from within a ThreadPoolExecutor.

    Returns:
        Dict with {label, file_api_name, file_uri, db_row, date}
        on success, or None on failure.
    """
    # ── Step 0: Pre-flight checks (AppleDouble & Ignored Extensions) ──
    file_path = row.file_path
    tmp_path = None
    original_filename = os.path.basename(file_path)
    
    if original_filename.startswith("._"):
        logger.warning(
            "Skipping AppleDouble metadata file %s: %s", label, file_path
        )
        try:
            move_to_unprocessable(file_path, config.output_dir)
        except OSError as e:
            logger.debug("Failed to quarantine AppleDouble file: %s", e)
        return {"error": "Failed", "db_row": row}
        
    _, ext = os.path.splitext(original_filename)
    if ext.lower() in config.ignored_extensions:
        logger.warning(
            "Skipping explicitly ignored extension %s: %s", label, file_path
        )
        try:
            move_to_unprocessable(file_path, config.output_dir)
        except OSError as e:
            logger.debug("Failed to quarantine ignored file: %s", e)
        return {"error": "Failed", "db_row": row}

    try:
        # Resize the image
        jpeg_bytes = resize_image(file_path)
        date = extract_date(file_path)

        # Write resized bytes to a temp file for upload
        with tempfile.NamedTemporaryFile(
            suffix=".jpg", delete=False, prefix=f"batch_{label}_"
        ) as tmp:
            tmp.write(jpeg_bytes)
            tmp_path = tmp.name

        # Upload to Gemini File API with retry
        uploaded_file = retry_with_backoff(
            fn=lambda: client.files.upload(
                file=tmp_path,
                config=types.UploadFileConfig(
                    display_name=f"{label}_{os.path.basename(file_path)}",
                    mime_type="image/jpeg",
                ),
            ),
            description=f"Upload {label}",
        )

        # Clean up temp file
        os.unlink(tmp_path)

        return {
            "label": label,
            "file_api_name": uploaded_file.name,
            "file_uri": uploaded_file.uri,
            "db_row": row,
            "date": date,
        }

    except (FileNotFoundError, PermissionError) as exc:
        logger.warning("File missing or unreadable, skipping %s: %s", label, exc)
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)
        return {"error": "Missing", "db_row": row}

    except Exception as exc:
        # Catch PIL exceptions (UnidentifiedImageError, DecompressionBombError, etc)
        logger.error("Failed to prepare/upload %s (%s): %s", label, file_path, exc)
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)
            
        # Move broken files to quarantine
        try:
            move_to_unprocessable(file_path, config.output_dir)
        except OSError as e:
            logger.debug("Failed to cascade quarantine file: %s", e)
            
        return {"error": "Failed", "db_row": row}


# ── Phase 1: Submit ──────────────────────────────────────────

def _submit_batch_job(
    client: Optional[genai.Client],
    config: AppConfig,
    db: Database,
    test_mode: bool,
    dry_run: bool,
) -> bool:
    """
    Phase 1: Submit a new batch job.

    Uploads images in parallel using ThreadPoolExecutor,
    creates JSONL, submits batch, and EXITS.

    Returns:
        True if a batch was successfully submitted, False otherwise.
    """
    # Determine batch size
    batch_limit = config.standard_club_size if test_mode else config.batch_chunk_size
    pending = db.get_pending_batch(batch_limit)

    if not pending:
        logger.info("No Pending images for batch submission")
        return False

    logger.info(
        "Phase 1 (Submit): Preparing %d images for batch upload (%d threads)",
        len(pending), config.upload_threads,
    )

    if dry_run:
        logger.info(
            "[DRY RUN] Would upload %d images and submit batch job — skipping",
            len(pending),
        )
        return False

    # Initialize tracking variables for cleanup
    uploaded_files: List[Dict] = []
    uploaded_lock = threading.Lock()
    jsonl_file = None

    try:
        # ── Step 1: Resize and upload images in parallel ─────────
        pbar = tqdm(total=len(pending), desc="Uploading to Gemini", unit="img")
        failed_ids: List[int] = []
        missing_ids: List[int] = []

        with ThreadPoolExecutor(max_workers=config.upload_threads) as executor:
            # Submit all upload tasks
            future_to_meta = {}
            for row in pending:
                label = f"img_{row.id}"
                future = executor.submit(
                    _upload_single_image, client, config, row, label,
                )
                future_to_meta[future] = (label, row)

            # Collect results as they complete
            for future in as_completed(future_to_meta):
                label, row = future_to_meta[future]
                try:
                    result = future.result()
                    if result is not None:
                        if "error" in result:
                            if result["error"] == "Missing":
                                missing_ids.append(row.id)
                            elif result["error"] == "Failed":
                                failed_ids.append(row.id)
                        else:
                            with uploaded_lock:
                                uploaded_files.append(result)
                    else:
                        failed_ids.append(row.id)
                except Exception as exc:
                    logger.error("Unexpected error for %s: %s", label, exc)
                    failed_ids.append(row.id)
                finally:
                    pbar.update(1)

        pbar.close()

        # Mark failed and missing images
        for fid in failed_ids:
            db.mark_failed(fid)
        for mid in missing_ids:
            db.mark_missing(mid)

        if not uploaded_files:
            logger.error("No images could be uploaded — aborting batch submission")
            return False

        logger.info("Uploaded %d images to File API", len(uploaded_files))

        # ── Step 2: Build JSONL input file ───────────────────────
        jsonl_lines = []

        for item in uploaded_files:
            request_obj = build_batch_request(
                image_uri=item["file_uri"],
                image_label=item["label"],
                categories=config.whatsapp_categories,
                fallback_category=config.fallback_category,
                global_rules=config.global_rules,
                model=config.active_model,
            )
            jsonl_lines.append(json.dumps(request_obj))

        # Write JSONL to a temp file
        jsonl_content = "\n".join(jsonl_lines)
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".jsonl", delete=False, prefix="batch_input_"
        ) as jf:
            jf.write(jsonl_content)
            jsonl_path = jf.name

        logger.debug("JSONL input file created: %s (%d lines)", jsonl_path, len(jsonl_lines))

        # ── Step 3: Upload JSONL and submit batch job ────────────
        try:
            # Upload the JSONL file with retry
            jsonl_file = retry_with_backoff(
                fn=lambda: client.files.upload(
                    file=jsonl_path,
                    config=types.UploadFileConfig(
                        display_name="batch_input.jsonl",
                        mime_type="application/jsonl",
                    ),
                ),
                description="Upload JSONL",
            )
            logger.debug("JSONL uploaded → %s", jsonl_file.name)

            # Clean up local JSONL temp file
            os.unlink(jsonl_path)

            # Submit the batch job with retry
            batch_job = retry_with_backoff(
                fn=lambda: client.batches.create(
                    model=config.active_model,
                    src=jsonl_file.name,
                    config=types.CreateBatchJobConfig(
                        display_name="whatsapp_image_sort_batch",
                    ),
                ),
                description="Submit batch job",
            )

            api_job_name = batch_job.name
            logger.info("Batch job submitted: %s", api_job_name)

        except Exception as exc:
            logger.error("Failed to submit batch job: %s", exc, exc_info=True)
            # Revert all uploaded images to Pending
            image_ids = [item["db_row"].id for item in uploaded_files]
            db.revert_to_pending(image_ids)
            # Attempt to clean up File API uploads
            _cleanup_file_api(
                client, 
                config,
                [item["file_api_name"] for item in uploaded_files]
            )
            if os.path.exists(jsonl_path):
                os.unlink(jsonl_path)
            return False

        # ── Step 4: Record in DB and EXIT ────────────────────────
        job_id = db.create_batch_job(api_job_name)

        # Mark all images as Processing with the batch job ID
        image_ids = [item["db_row"].id for item in uploaded_files]
        db.mark_processing(image_ids, batch_job_id=job_id)

        # Store the file API names for later cleanup
        _save_batch_metadata(job_id, uploaded_files)

        logger.info("╔════════════════════════════════════════════════════╗")
        logger.info("║  Batch job submitted successfully!                 ║")
        logger.info("║  Job: %-42s ║", api_job_name)
        logger.info("║  Images: %-39d   ║", len(uploaded_files))
        logger.info("╚════════════════════════════════════════════════════╝")

        return True  # Submitted successfully

    except KeyboardInterrupt:
        logger.warning(
            "Upload interrupted by user (Ctrl+C). Cleaning up %d orphaned files...",
            len(uploaded_files),
        )

        file_names_to_delete = [item["file_api_name"] for item in uploaded_files]
        if jsonl_file:
            file_names_to_delete.append(jsonl_file.name)

        if file_names_to_delete:
            _cleanup_file_api(client, config, file_names_to_delete)

        # Re-raise so main.py can catch it and print the session summary
        raise

    except Exception as exc:
        logger.error("Failed during batch submission: %s", exc, exc_info=True)
        file_names_to_delete = [item["file_api_name"] for item in uploaded_files]
        if jsonl_file:
            file_names_to_delete.append(jsonl_file.name)
        if file_names_to_delete:
            _cleanup_file_api(client, config, file_names_to_delete)
        raise


# ── Phase 2: Resume & Poll ───────────────────────────────────

def _resume_batch_job(
    client: Optional[genai.Client],
    config: AppConfig,
    db: Database,
    cost_tracker: CostTracker,
    job: Dict,
    dry_run: bool,
) -> int:
    """
    Phase 2: Resume and poll an existing batch job.

    Returns:
        Number of images successfully processed.
    """
    job_id = job.job_id
    api_job_name = job.api_job_name

    logger.info("Checking batch job: %s (DB id: %d)", api_job_name, job_id)

    if dry_run:
        logger.info("[DRY RUN] Would poll batch job %s — skipping", api_job_name)
        return 0

    # ── Poll the Gemini API ──────────────────────────────────
    try:
        while True:
            batch_job = client.batches.get(name=api_job_name)
            job_state = batch_job.state.name if hasattr(batch_job.state, "name") else str(batch_job.state)
            
            if job_state in ("JOB_STATE_RUNNING", "JOB_STATE_PENDING", "RUNNING", "PENDING"):
                now_str = datetime.now().strftime("%I:%M:%S %p")
                
                # Countdown for 60 seconds
                for remaining in range(60, 0, -1):
                    sys.stdout.write(f"\rBatch job {job_state}. Last polled at {now_str}. Next poll in {remaining}s... (Press Ctrl+C to exit)")
                    sys.stdout.flush()
                    try:
                        time.sleep(1)
                    except KeyboardInterrupt:
                        # Clear the line before printing the log
                        sys.stdout.write("\r" + " " * 100 + "\r")
                        sys.stdout.flush()
                        logger.warning("Polling interrupted by user (Ctrl+C). Script will exit, but Google is still processing your job.")
                        raise
                
                # Clear line after countdown finishes, before polling again
                sys.stdout.write("\r" + " " * 100 + "\r")
                sys.stdout.flush()
                continue
            
            break # Exit the polling loop if not pending/running

    except Exception as exc:
        if isinstance(exc, KeyboardInterrupt):
            raise
        logger.error("Failed to poll batch job %s: %s", api_job_name, exc)
        return 0

    # Ensure the line is cleared if it broke out of the loop successfully
    sys.stdout.write("\r" + " " * 100 + "\r")
    sys.stdout.flush()
    logger.info("Batch job %s finished with state: %s", api_job_name, job_state)

    if job_state in ("JOB_STATE_SUCCEEDED", "SUCCEEDED"):
        return _handle_batch_success(
            client=client,
            config=config,
            db=db,
            cost_tracker=cost_tracker,
            job_id=job_id,
            batch_job=batch_job,
        )

    if job_state in ("JOB_STATE_FAILED", "FAILED"):
        return _handle_batch_failure(
            client=client,
            config=config,
            db=db,
            job_id=job_id,
            api_job_name=api_job_name,
        )

    # Unknown state
    logger.warning("Unexpected batch job state: %s for %s", job_state, api_job_name)
    return 0


def _handle_batch_success(
    client: genai.Client,
    config: AppConfig,
    db: Database,
    cost_tracker: CostTracker,
    job_id: int,
    batch_job,
) -> int:
    """
    Process successful batch job results.

    Downloads output, parses results, moves files, and cleans up.

    Returns:
        Number of images successfully processed.
    """
    logger.info("Batch job SUCCEEDED — processing results")

    # Get images associated with this batch job
    images = db.get_images_by_batch_job(job_id)
    if not images:
        logger.warning("No images found for batch job %d", job_id)
        db.update_batch_job_status(job_id, BATCH_SUCCEEDED)
        return 0

    # Build a lookup: label → image row
    image_by_label: Dict[str, Dict] = {f"img_{img.id}": img for img in images}

    # ── Download and parse output ────────────────────────────
    processed = 0
    matched_labels = set()

    try:
        if hasattr(batch_job, "dest") and batch_job.dest:
            dest_file_name = batch_job.dest.file_name if hasattr(batch_job.dest, "file_name") else None

            if dest_file_name:
                # Download the output file content (returns bytes in the new SDK)
                output_content = client.files.download(file=dest_file_name)
                if isinstance(output_content, bytes):
                    output_content = output_content.decode("utf-8")

                # Parse each line of the JSONL output
                lines = [line for line in output_content.strip().split("\n") if line.strip()]
                for line in tqdm(lines, desc="Processing results", unit="img", dynamic_ncols=True):
                    try:
                        result = json.loads(line)
                        key = result.get("key", "")
                        response_body = result.get("response", {})

                        # Extract category from the generated text
                        candidates = response_body.get("candidates", [])
                        if candidates:
                            text_parts = candidates[0].get("content", {}).get("parts", [])
                            if text_parts:
                                response_text = text_parts[0].get("text", "")
                                try:
                                    parsed = json.loads(response_text)
                                    category = parsed.get("category", "Uncategorized_Review")
                                except json.JSONDecodeError:
                                    category = "Uncategorized_Review"
                            else:
                                category = "Uncategorized_Review"
                        else:
                            category = "Uncategorized_Review"

                        if key in image_by_label:
                            matched_labels.add(key)
                            row = image_by_label[key]
                            date = extract_date(row.file_path)

                            move_image(
                                src_path=row.file_path,
                                category=category,
                                date=date,
                                output_dir=config.output_dir,
                                exif_restore=config.features.restore_exif_date,
                            )
                            db.mark_completed(row.id, category)
                            processed += 1

                    except Exception as exc:
                        logger.error("Error processing batch result line: %s — %s", line[:100], exc)

                    # Record usage if available
                    usage = response_body.get("usageMetadata") or response_body.get("usage_metadata")
                    if usage:
                        cost_tracker.record_usage(
                            input_tokens=usage.get("promptTokenCount") or usage.get("prompt_token_count", 0),
                            output_tokens=usage.get("candidatesTokenCount") or usage.get("candidates_token_count", 0),
                            images_in_request=1,
                        )

    except Exception as exc:
        logger.error("Failed to process batch results: %s", exc, exc_info=True)

    # ── Handle mismatches ────────────────────────────────────
    unmatched_labels = set(image_by_label.keys()) - matched_labels
    if unmatched_labels:
        logger.warning(
            "BATCH MISMATCH: %d/%d images missing from results — reverting to Pending",
            len(unmatched_labels), len(image_by_label),
        )
        unmatched_ids = [image_by_label[lbl].id for lbl in unmatched_labels]
        db.revert_to_pending(unmatched_ids)

    # ── Update batch job status ──────────────────────────────
    db.update_batch_job_status(job_id, BATCH_SUCCEEDED)

    # ── Cleanup File API uploads ─────────────────────────────
    metadata = _load_batch_metadata(job_id)
    if metadata:
        file_names = [item["file_api_name"] for item in metadata]
        _cleanup_file_api(client, config, file_names)

    logger.info(
        "Batch job complete: %d/%d processed, %d mismatched",
        processed, len(image_by_label), len(unmatched_labels),
    )
    return processed


def _handle_batch_failure(
    client: genai.Client,
    config: AppConfig,
    db: Database,
    job_id: int,
    api_job_name: str,
) -> int:
    """
    Handle a failed batch job.

    Reverts all images to Pending with retry increment,
    cleans up File API uploads.

    Returns:
        0 (no images processed).
    """
    logger.error("Batch job FAILED: %s", api_job_name)

    # Revert all associated images
    images = db.get_images_by_batch_job(job_id)
    if images:
        image_ids = [img.id for img in images]
        db.revert_to_pending_with_retry(image_ids)

    # Update batch job status
    db.update_batch_job_status(job_id, BATCH_FAILED)

    # Cleanup File API uploads
    metadata = _load_batch_metadata(job_id)
    if metadata:
        file_names = [item["file_api_name"] for item in metadata]
        _cleanup_file_api(client, config, file_names)

    return 0


# ── File API Cleanup (parallelized) ──────────────────────────

def _cleanup_file_api(
    client: Optional[genai.Client],
    config: AppConfig,
    file_names: List[str],
) -> None:
    """
    Delete temporary files from the Gemini File API in parallel.

    Frees user quota. Logs but does not raise on failure
    (cleanup failures are non-fatal). Uses thread counts directly
    from the application configuration.

    Args:
        client: Gemini API client.
        config: Loaded AppConfig to read `upload_threads`.
        file_names: List of File API resource names to delete.
    """
    if not client or not file_names:
        return

    logger.info("Cleaning up %d File API uploads (showing progress below)...", len(file_names))
    deleted = 0
    lock = threading.Lock()

    def _delete_one(name: str) -> bool:
        try:
            retry_with_backoff(
                fn=lambda: client.files.delete(name=name),
                description=f"Delete {name}",
            )
            logger.debug("Deleted File API resource: %s", name)
            return True
        except Exception as exc:
            logger.warning("Failed to delete %s: %s", name, exc)
            return False

    actual_workers = min(config.upload_threads, len(file_names))
    with ThreadPoolExecutor(max_workers=actual_workers) as executor:
        futures = {executor.submit(_delete_one, name): name for name in file_names}
        
        with tqdm(total=len(file_names), desc="Cleaning up API files", unit="file", dynamic_ncols=True) as pbar:
            for future in as_completed(futures):
                if future.result():
                    with lock:
                        deleted += 1
                pbar.update(1)

    logger.info("File API cleanup: %d/%d deleted", deleted, len(file_names))


# ── Batch metadata persistence ───────────────────────────────

_METADATA_DIR = "batch_metadata"


def _save_batch_metadata(job_id: int, uploaded_files: List[Dict]) -> None:
    """
    Save batch metadata (file API names) to a local JSON file.

    This persists across script restarts so that Phase 2 can
    clean up File API uploads even if the script was restarted.

    Args:
        job_id: Database batch job ID.
        uploaded_files: List of upload info dicts.
    """
    os.makedirs(_METADATA_DIR, exist_ok=True)
    meta_path = os.path.join(_METADATA_DIR, f"batch_{job_id}.json")

    meta = [
        {
            "label": item["label"],
            "file_api_name": item["file_api_name"],
            "file_uri": item["file_uri"],
            "file_path": item["db_row"].file_path,        }
        for item in uploaded_files
    ]

    with open(meta_path, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2)

    logger.debug("Saved batch metadata: %s", meta_path)


def _load_batch_metadata(job_id: int) -> Optional[List[Dict]]:
    """
    Load batch metadata from a local JSON file.

    Returns None if the file doesn't exist (graceful fallback).

    Args:
        job_id: Database batch job ID.

    Returns:
        List of metadata dicts, or None.
    """
    meta_path = os.path.join(_METADATA_DIR, f"batch_{job_id}.json")
    if not os.path.isfile(meta_path):
        logger.debug("No batch metadata file: %s", meta_path)
        return None

    try:
        with open(meta_path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to load batch metadata %s: %s", meta_path, exc)
        return None
