"""
============================================================
standard_mode.py — Synchronous Image Processing
============================================================
Implements the Standard API mode:

1. Pull up to standard_club_size Pending images from the DB.
2. Resize each → base64 JPEG bytes.
3. Build prompt + interleaved parts via prompt_builder.
4. Call Gemini generate_content with response_mime_type JSON.
5. Parse JSON → list of {image, category}.
6. Mismatch handling: move matched files, revert unmatched.
7. Update DB, log session stats.
8. Repeat until no Pending remain (or --test-mode after 1).

All API calls are wrapped in try/except with error logging.
============================================================
"""

import base64
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional, Tuple

from google import genai
from google.genai import types
from tqdm import tqdm

from src.config_manager import AppConfig
from src.cost_tracker import CostTracker
from src.database import Database, STATUS_PENDING
from src.file_mover import move_image
from src.image_utils import extract_date, resize_image
from src.prompt_builder import build_standard_prompt
from src.retry import retry_with_backoff

logger = logging.getLogger("whatsapp_sorter")


def _build_content_parts(
    prompt: str,
    images: List[Tuple[str, bytes]],
) -> List[types.Part]:
    """
    Build the list of Part objects for a generate_content call.

    Starts with the text prompt, then interleaves text labels
    ("Image_N:") with inline image data for each image.

    Args:
        prompt: The system/instruction prompt text.
        images: List of (label, jpeg_bytes) tuples.

    Returns:
        List of types.Part objects ready for the API call.
    """
    parts = [types.Part.from_text(text=prompt)]

    for label, jpeg_bytes in images:
        # Text label for this image
        parts.append(types.Part.from_text(text=f"{label}:"))

        # Inline image data
        parts.append(
            types.Part.from_bytes(
                data=jpeg_bytes,
                mime_type="image/jpeg",
            )
        )

    return parts


def run_standard_mode(
    config: AppConfig,
    db: Database,
    cost_tracker: CostTracker,
    test_mode: bool = False,
    dry_run: bool = False,
) -> int:
    """
    Process images in Standard (synchronous) mode.

    Fetches batches of Pending images, sends them to the
    Gemini API in clubbed requests, parses responses, and
    moves files to sorted directories.

    Args:
        config: Validated application configuration.
        db: Initialised database instance.
        cost_tracker: Cost tracking instance.
        test_mode: If True, process only one batch then stop.
        dry_run: If True, skip API calls and file moves.

    Returns:
        Total number of images successfully processed.
    """
    logger.info("Starting Standard mode processing")
    total_processed = 0
    batch_number = 0

    # ── Initialise the Gemini client ─────────────────────────
    client = None
    if not dry_run:
        client = genai.Client(api_key=config.gemini_api_key)
        logger.debug("Gemini client initialised for model: %s", config.active_model)

    # ── Calculate total batches ──────────────────────────────
    total_pending = db.get_total_count()
    # We only care about pending here for the progress indicator
    queue_stats = db.get_queue_stats()
    pending = queue_stats.get(STATUS_PENDING, 0)
    club_size = config.standard_club_size
    # Ceiling division for total batches
    total_batches = (pending + club_size - 1) // club_size if pending > 0 else 0

    # ── Main processing loop ─────────────────────────────────
    
    # Create the progress bar for the total pending images
    pbar = tqdm(total=pending, desc="Processing Images", unit="img")
    
    try:
        while True:
            # Fetch next batch of Pending images
            pending_batch = db.get_pending_batch(club_size)
            if not pending_batch:
                logger.debug("No more Pending images — Standard mode complete")
                break

            batch_number += 1
            batch_size = len(pending_batch)
            progress_str = f"{batch_number}/{total_batches}" if total_batches > 0 else f"{batch_number}"
            
            logger.info(
                "━━━ Batch %s: %d image(s) ━━━",
                progress_str, batch_size,
            )

            if dry_run:
                logger.info(
                    "[DRY RUN] Would process %d images — skipping API call",
                    batch_size,
                )
                # Update the progress bar for the skipped batch
                pbar.update(batch_size)
                break

            # ── Step 1: Resize images and prepare labels ─────────
            images_data: List[Tuple[str, bytes]] = []  # (label, jpeg_bytes)
            image_map: Dict[str, Dict] = {}  # label → db row + metadata

            for i, row in enumerate(pending_batch, start=1):
                label = f"Image_{i}"
                file_path = row["file_path"]

                try:
                    jpeg_bytes = resize_image(file_path)
                    date = extract_date(file_path)

                    images_data.append((label, jpeg_bytes))
                    image_map[label] = {
                        "db_row": row,
                        "date": date,
                        "jpeg_bytes": jpeg_bytes,
                    }
                    logger.debug("Prepared %s: %s", label, file_path)

                except (FileNotFoundError, PermissionError) as exc:
                    logger.warning("File missing or unreadable %s: %s", file_path, exc)
                    db.mark_missing(row["id"])

                except Exception as exc:
                    logger.error("Failed to prepare image %s: %s", file_path, exc)
                    db.mark_failed(row["id"])

            if not images_data:
                logger.warning("No images could be prepared in this batch — skipping")
                # Update the progress bar for the skipped batch
                pbar.update(batch_size)
                continue

            # ── Step 2: Build prompt and content parts ───────────
            actual_count = len(images_data)
            prompt = build_standard_prompt(
                actual_count,
                config.whatsapp_categories,
                config.fallback_category,
                config.global_rules
            )
            content_parts = _build_content_parts(prompt, images_data)

            # ── Step 3: Call the Gemini API ──────────────────────
            try:
                logger.info("Sending %d image(s) to Gemini API (waiting for response)...", actual_count)

                def _call_api():
                    # This single synchronous call contains all interleaved images and text for the batch.
                    return retry_with_backoff(
                        fn=lambda: client.models.generate_content(
                            model=config.active_model,
                            contents=types.Content(
                                role="user",
                                parts=content_parts,
                            ),
                            config=types.GenerateContentConfig(
                                response_mime_type="application/json",
                                temperature=0.1,
                            ),
                        ),
                        description=f"Gemini API batch {batch_number}",
                    )

                # We use a ThreadPoolExecutor with 1 worker to execute the synchronous API call in the background.
                # This prevents the main thread from freezing and allows us to draw a live tqdm wait spinner
                # while we wait 5-10 seconds for Google's servers to process the single bulk request.
                with ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(_call_api)
                    
                    # Show an indeterminate spinner in terminal
                    with tqdm(desc=f"API Wait", unit="s", leave=False) as wait_pbar:
                        while not future.done():
                            time.sleep(1)
                            wait_pbar.update(1)
                            
                    response = future.result()

                logger.info("API response received for Batch %s", progress_str)

            except Exception as exc:
                logger.error(
                    "API call failed for batch %d: %s", batch_number, exc,
                    exc_info=True,
                )
                # Revert all images in batch to Pending for retry
                failed_ids = [image_map[lbl]["db_row"]["id"] for lbl in image_map]
                db.revert_to_pending(failed_ids)
                # Update the progress bar for the failed batch
                pbar.update(batch_size)

                if test_mode:
                    logger.info("--test-mode: stopping after 1 batch (API error)")
                    break
                continue

            # ── Step 4: Record token usage ───────────────────────
            if hasattr(response, "usage_metadata") and response.usage_metadata:
                usage = response.usage_metadata
                cost_result = cost_tracker.record_usage(
                    input_tokens=getattr(usage, "prompt_token_count", 0) or 0,
                    output_tokens=getattr(usage, "candidates_token_count", 0) or 0,
                    images_in_request=actual_count,
                )
                logger.info("Batch %d cost: %s", batch_number, cost_result.format_display())

            # ── Step 5: Parse JSON response ──────────────────────
            try:
                response_text = response.text
                results = json.loads(response_text)
                if not isinstance(results, list):
                    raise ValueError(f"Expected JSON array, got: {type(results)}")
                logger.debug("Parsed %d results from API response", len(results))

            except (json.JSONDecodeError, ValueError, AttributeError) as exc:
                logger.error(
                    "Failed to parse API response for batch %d: %s\nRaw: %s",
                    batch_number, exc,
                    getattr(response, "text", "<no text>"),
                )
                failed_ids = [image_map[lbl]["db_row"]["id"] for lbl in image_map]
                db.revert_to_pending(failed_ids)
                # Update the progress bar for the failed batch
                pbar.update(batch_size)

                if test_mode:
                    logger.info("--test-mode: stopping after 1 batch (parse error)")
                    break
                continue

            # ── Step 6: Match results to images ──────────────────
            matched_labels = set()
            batch_processed = 0

            for result_obj in results:
                label = result_obj.get("image", "")
                category = result_obj.get("category", "Uncategorized_Review")

                if label not in image_map:
                    logger.warning("Unknown label in response: '%s' — skipping", label)
                    continue

                matched_labels.add(label)
                img_info = image_map[label]
                row = img_info["db_row"]
                date = img_info["date"]
                jpeg_bytes = img_info["jpeg_bytes"]

                # ── Move the file ────────────────────────────────
                try:
                    move_image(
                        src_path=row["file_path"],
                        category=category,
                        date=date,
                        output_dir=config.output_dir,
                        exif_restore=config.features.restore_exif_date,
                    )
                    db.mark_completed(row["id"], category)
                    batch_processed += 1

                except Exception as exc:
                    logger.error(
                        "Failed to move image %s (%s): %s",
                        label, row["file_path"], exc,
                    )
                    db.mark_failed(row["id"])

            # ── Step 7: Handle mismatches ────────────────────────
            unmatched_labels = set(image_map.keys()) - matched_labels
            if unmatched_labels:
                logger.warning(
                    "MISMATCH: AI returned %d results for %d images. "
                    "Missing labels: %s — reverting to Pending",
                    len(matched_labels), actual_count, unmatched_labels,
                )
                unmatched_ids = [
                    image_map[lbl]["db_row"]["id"]
                    for lbl in unmatched_labels
                ]
                db.revert_to_pending(unmatched_ids)

            total_processed += batch_processed
            logger.info(
                "Batch %s complete: %d/%d processed, %d mismatched",
                progress_str, batch_processed, actual_count, len(unmatched_labels),
            )
            
            # Update the progress bar
            pbar.update(batch_size)

            # ── Test mode: exit after first batch ────────────────
            if test_mode:
                logger.info("--test-mode: stopping after 1 batch")
                break

    except KeyboardInterrupt:
        logger.warning("Processing interrupted by user during batch %s (Ctrl+C)", progress_str)
        # Revert the current batch's pending images if necessary
        # The finally block in main handles the rest
    finally:
        pbar.close()

    logger.info("Standard mode finished: %d images processed total", total_processed)
    return total_processed
