# -*- coding: utf-8 -*-
# -*- coding: utf-8 -*-
"""
============================================================
main.py — CLI Entry Point for WhatsApp Image Sorter
============================================================
Usage:
    python main.py                  # run normally
    python main.py --test-mode      # process 1 batch, then exit
    python main.py --dry-run        # scan & estimate, no changes

Lifecycle:
    1. Set up logging (console + file).
    2. Load & validate config (config.json + .env).
    3. Initialise SQLite database (state.db).
    4. Scan source directory for new images → enqueue.
    5. Show stats: total, pending, estimated cost.
    6. Route to Standard or Batch mode.
    7. Print session summary and exit.
============================================================
"""

import argparse
import logging
import sys
import uuid
from pathlib import Path
from typing import Iterator

from tqdm import tqdm

from src.models.config import load_config
from src.utils.cost_tracker import CostTracker
from src.utils.database import Database
from src.utils.logger_setup import setup_logging

logger = logging.getLogger("whatsapp_sorter")

# Supported image extensions (case-insensitive matching)
IMAGE_EXTENSIONS = frozenset({
    ".jpg", ".jpeg", ".png", ".gif", ".bmp",
    ".webp", ".tiff", ".tif", ".heic", ".heif",
})


def _scan_source_directory(source_dir: str | Path) -> Iterator[Path]:
    """
    Recursively scan the source directory using a Generator.
    """
    source_path = Path(source_dir)
    logger.info("Scanning source directory: %s", source_path)

    for file_path in source_path.rglob("*"):
        if file_path.is_file() and file_path.suffix.lower() in IMAGE_EXTENSIONS:
            yield file_path


def _print_banner() -> None:
    """Print a nice startup banner."""
    print(
        "\n"
        "╔══════════════════════════════════════════════════════╗\n"
        "║              WhatsApp Image Sorter                  ║\n"
        "║         AI-powered image categorization             ║\n"
        "╚══════════════════════════════════════════════════════╝\n"
    )


def _print_dry_run_summary(
    config,
    total_images: int,
    new_images: int,
    queue_stats: dict,
    cost_tracker: CostTracker,
) -> None:
    """
    Print detailed dry-run summary and exit.
    """
    pending = queue_stats.get("Pending", 0)
    completed = queue_stats.get("Completed", 0)
    failed = queue_stats.get("Failed", 0)

    estimate = cost_tracker.estimate_cost(pending)

    print("\n═══ DRY RUN SUMMARY ═══════════════════════════════")
    print(f"  Source directory : {config.source_dir}")
    print(f"  Output directory : {config.output_dir}")
    print(f"  Mode             : {config.api_mode}")
    print(f"  Model            : {config.active_model}")
    print(f"  EXIF restore     : {config.features.restore_exif_date}")
    print()
    print(f"  Total image files found : {total_images:,}")
    print(f"  Newly enqueued          : {new_images:,}")
    print(f"  Already in queue        :")
    print(f"    Pending   : {pending:,}")
    print(f"    Completed : {completed:,}")
    print(f"    Failed    : {failed:,}")
    missing = queue_stats.get("Missing", 0)
    if missing > 0:
        print(f"    Missing   : {missing:,}")
    print()
    print(f"  ── Estimated Cost (for {pending:,} pending images) ──")
    print(f"  {estimate.format_display()}")
    print()
    print(f"  ── Categories ──")
    for i, cat in enumerate(config.whatsapp_categories, 1):
        print(f"    {i}. {cat.name}")
    print(f"    +  {config.fallback_category} (fallback)")
    print()
    print("  No API calls made. No files moved.")
    print("═══════════════════════════════════════════════════\n")


def main() -> None:
    """Main entry point for the WhatsApp Image Sorter."""

    # ── Parse CLI arguments ──────────────────────────────────
    parser = argparse.ArgumentParser(
        description="AI-powered WhatsApp image sorter using Google Gemini",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python main.py                # Run in mode from config\n"
            "  python main.py --test-mode    # Process 1 batch, then exit\n"
            "  python main.py --dry-run      # Scan and estimate only\n"
        ),
    )
    parser.add_argument(
        "--test-mode",
        action="store_true",
        help="Process exactly one batch and exit (good for validation)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan images, show stats and cost estimate, but make no changes",
    )
    parser.add_argument(
        "--prune-queue",
        action="store_true",
        help="Clear the image queue database tracking completely",
    )
    args = parser.parse_args()

    # ── Step 1: Set up logging ───────────────────────────────
    setup_logging()
    _print_banner()

    # ── Step 2: Load & validate config ───────────────────────
    try:
        config = load_config()
    except SystemExit:
        return

    # ── Step 3: Initialise database ──────────────────────────
    with Database() as db:
        logger.info("Database ready")

        if args.prune_queue:
            db.truncate_queue()
            print("\n  Image queue has been successfully cleared.\n")
            return

        # ── Step 4: Scan source directory and enqueue new images ──
        # Consume the generator to sort and get the total count for tqdm
        image_paths = sorted(list(_scan_source_directory(config.source_dir)))
        total_images = len(image_paths)
        
        # Prune Database: Remove DB rows for files that were deleted from disk
        db.prune_missing_files({str(p) for p in image_paths})

        if total_images == 0:
            logger.info("No image files found in %s — nothing to do", config.source_dir)
            print(f"\n  No image files found in {config.source_dir}\n")
            return

        # Enqueue with progress bar
        new_count = 0
        with tqdm(
            total=total_images,
            desc="Enqueuing images",
            unit="img",
            disable=False,
        ) as pbar:
            # Batch enqueue for efficiency
            batch_size = 500
            for i in range(0, total_images, batch_size):
                batch = [str(p) for p in image_paths[i : i + batch_size]]
                inserted = db.enqueue_images(batch)
                new_count += inserted
                pbar.update(len(batch))

        logger.info("Enqueue complete: %d new, %d total", new_count, total_images)

        # ── Step 5: Show queue stats and cost estimate ───────────
        queue_stats = db.get_queue_stats()
        pending_count = queue_stats.get("Pending", 0)
        completed_count = queue_stats.get("Completed", 0)

        # Calibrate tracker with actual historical usage from DB for this specific model
        if config.api_mode == "batch":
            cost_tracker = CostTracker(config, discount_multiplier=0.5)
        else:
            cost_tracker = CostTracker(config)
            
        cost_tracker.calibrate_from_db(db.get_estimation_stats(config.active_model))

        if args.dry_run:
            _print_dry_run_summary(
                config=config,
                total_images=total_images,
                new_images=new_count,
                queue_stats=queue_stats,
                cost_tracker=cost_tracker,
            )
            return

        has_running_batch = False
        if config.api_mode == "batch":
            has_running_batch = len(db.get_running_batch_jobs()) > 0

        # Show brief stats before processing
        if pending_count == 0 and not has_running_batch:
            logger.info("No pending images — all %d images already processed", completed_count)
            print(f"\n  All {completed_count:,} images already processed. Nothing to do.\n")
            return

        estimate = cost_tracker.estimate_cost(pending_count)
        print(f"\n  Pending images: {pending_count:,}")
        print(f"  Estimated cost: {estimate.format_display()}")
        print()

        # ── Step 6: Route to the appropriate mode ────────────────
        session_id = str(uuid.uuid4())
        logger.info("Session %s starting — mode=%s", session_id, config.api_mode)
        
        processed = 0
        try:
            if config.api_mode == "standard":
                from src.core.standard_mode import run_standard_mode

                processed = run_standard_mode(
                    config=config,
                    db=db,
                    cost_tracker=cost_tracker,
                    test_mode=args.test_mode,
                    dry_run=False,
                )
            elif config.api_mode == "batch":
                from src.core.batch_mode import run_batch_mode

                processed = run_batch_mode(
                    config=config,
                    db=db,
                    cost_tracker=cost_tracker,
                    test_mode=args.test_mode,
                    dry_run=False,
                )
            else:
                logger.error("Unknown api_mode: %s", config.api_mode)
                processed = 0

        except KeyboardInterrupt:
            logger.warning("Processing interrupted by user (Ctrl+C)")
        except Exception as exc:
            logger.error("Unhandled error during processing: %s", exc, exc_info=True)

        # ── Step 7: Record session and print summary ─────────────
        session_cost = cost_tracker.get_session_total()
        db.record_session(
            session_id=session_id,
            mode=config.api_mode,
            model=config.active_model,
            images_processed=processed,
            total_tokens=cost_tracker.total_tokens,
            cost_local_currency=session_cost.cost_local,
        )

        # Update global estimation stats with this session's actuals for this model
        actuals = cost_tracker.get_estimation_actuals()
        db.update_estimation_stats(config.active_model, *actuals)

        # Final summary
        print("\n═══ SESSION SUMMARY ═══════════════════════════════")
        print(f"  Session ID : {session_id}")
        print(f"  Mode       : {config.api_mode}")
        print(f"  Model      : {config.active_model}")
        print(f"  Processed  : {processed:,} images")
        print(f"  Cost       : {session_cost.format_display()}")

        # Updated queue state
        final_stats = db.get_queue_stats()
        print(f"  Queue      : {final_stats}")
        print("═══════════════════════════════════════════════════\n")

        logger.info("Session %s complete — %d images processed", session_id, processed)


if __name__ == "__main__":
    main()

