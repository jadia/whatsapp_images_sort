"""
============================================================
database.py — SQLite State Management
============================================================
Manages state.db with three tables:

  ImageQueue   — tracks every image through the pipeline
  BatchJobs    — tracks async Batch API job lifecycle
  SessionStats — records per-run statistics for auditing

Key design decisions:
  - WAL journal mode for concurrent read access.
  - Audit columns (inserted_on, updated_on) on all tables.
  - updated_on is auto-managed via AFTER UPDATE triggers.
  - file_path has a UNIQUE constraint to prevent re-enqueuing.
  - All timestamps are UTC ISO-8601.
============================================================
"""

import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("whatsapp_sorter")

# ── Status constants ─────────────────────────────────────────
STATUS_PENDING = "Pending"
STATUS_PROCESSING = "Processing"
STATUS_COMPLETED = "Completed"
STATUS_FAILED = "Failed"

BATCH_RUNNING = "Running"
BATCH_SUCCEEDED = "Succeeded"
BATCH_FAILED = "Failed"

# ── SQL Statements ───────────────────────────────────────────
_CREATE_TABLES = """
-- ============================================================
-- ImageQueue: one row per image file to process
-- ============================================================
CREATE TABLE IF NOT EXISTS ImageQueue (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path     TEXT    NOT NULL UNIQUE,
    status        TEXT    NOT NULL DEFAULT 'Pending',
    category      TEXT,
    retry_count   INTEGER NOT NULL DEFAULT 0,
    batch_job_id  INTEGER,
    inserted_on   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_on    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    FOREIGN KEY (batch_job_id) REFERENCES BatchJobs(job_id)
);

-- ============================================================
-- BatchJobs: one row per Batch API submission
-- ============================================================
CREATE TABLE IF NOT EXISTS BatchJobs (
    job_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    api_job_name  TEXT    NOT NULL,
    status        TEXT    NOT NULL DEFAULT 'Running',
    created_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_on    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- ============================================================
-- SessionStats: one row per run for auditing
-- ============================================================
CREATE TABLE IF NOT EXISTS SessionStats (
    session_id          TEXT    PRIMARY KEY,
    mode                TEXT    NOT NULL,
    model_name          TEXT,
    images_processed    INTEGER NOT NULL DEFAULT 0,
    total_tokens        INTEGER NOT NULL DEFAULT 0,
    cost_local_currency REAL    NOT NULL DEFAULT 0.0,
    inserted_on         TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- ============================================================
-- EstimationStats: cumulative token averages for cost estimates
-- Self-calibrating: updated after each session with actuals.
-- Separated by model_name because different models have different
-- tokenizers and vision costs.
-- ============================================================
CREATE TABLE IF NOT EXISTS EstimationStats (
    model_name             TEXT PRIMARY KEY,
    total_images_measured  INTEGER NOT NULL DEFAULT 0,
    total_input_tokens     INTEGER NOT NULL DEFAULT 0,
    total_output_tokens    INTEGER NOT NULL DEFAULT 0,
    updated_on             TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);



-- ============================================================
-- Auto-update triggers for updated_on columns
-- ============================================================
CREATE TRIGGER IF NOT EXISTS trg_image_queue_updated_on
    AFTER UPDATE ON ImageQueue
    FOR EACH ROW
BEGIN
    UPDATE ImageQueue
    SET updated_on = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
    WHERE id = OLD.id;
END;

CREATE TRIGGER IF NOT EXISTS trg_batch_jobs_updated_on
    AFTER UPDATE ON BatchJobs
    FOR EACH ROW
BEGIN
    UPDATE BatchJobs
    SET updated_on = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
    WHERE job_id = OLD.job_id;
END;
"""


class Database:
    """
    SQLite database manager for the image sorting pipeline.

    Handles all state persistence: image queue management,
    batch job tracking, and session statistics.
    """

    def __init__(self, db_path: str = "state.db"):
        """
        Open (or create) the SQLite database and initialise schema.

        Args:
            db_path: Path to the SQLite database file.
        """
        self.db_path = db_path
        logger.debug("Opening database: %s", db_path)

        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row  # Dict-like row access

        # Enable WAL mode for concurrent reads
        self.conn.execute("PRAGMA journal_mode=WAL")
        # Enable foreign key enforcement
        self.conn.execute("PRAGMA foreign_keys=ON")

        # Create tables and triggers
        self.conn.executescript(_CREATE_TABLES)
        self.conn.commit()
        logger.info("Database initialised: %s", db_path)

    # ── ImageQueue Operations ────────────────────────────────

    def enqueue_images(self, file_paths: List[str]) -> int:
        """
        Add new image paths to the queue with status 'Pending'.

        Skips paths that already exist in the queue (UNIQUE
        constraint on file_path). This is safe to call on
        restarts — existing images are not re-enqueued.

        Args:
            file_paths: List of absolute file paths to enqueue.

        Returns:
            Number of newly enqueued images.
        """
        inserted = 0
        for path in file_paths:
            try:
                self.conn.execute(
                    "INSERT INTO ImageQueue (file_path) VALUES (?)",
                    (path,),
                )
                inserted += 1
            except sqlite3.IntegrityError:
                # Already exists — skip silently
                logger.debug("Already in queue, skipping: %s", path)
        self.conn.commit()
        logger.info("Enqueued %d new images (skipped %d existing)", inserted, len(file_paths) - inserted)
        return inserted

    def get_pending_batch(self, limit: int) -> List[Dict]:
        """
        Fetch up to `limit` Pending images from the queue.

        Returns oldest-first (by id) to ensure FIFO processing.

        Args:
            limit: Maximum number of images to fetch.

        Returns:
            List of dicts with keys: id, file_path, status,
            retry_count.
        """
        cursor = self.conn.execute(
            "SELECT id, file_path, status, retry_count "
            "FROM ImageQueue "
            "WHERE status = ? "
            "ORDER BY id ASC "
            "LIMIT ?",
            (STATUS_PENDING, limit),
        )
        rows = [dict(row) for row in cursor.fetchall()]
        logger.debug("Fetched %d pending images (limit=%d)", len(rows), limit)
        return rows

    def mark_processing(self, image_ids: List[int], batch_job_id: Optional[int] = None) -> None:
        """
        Transition images to 'Processing' status.

        Optionally links them to a batch job ID for tracking.

        Args:
            image_ids: List of ImageQueue.id values.
            batch_job_id: Optional BatchJobs.job_id to link.
        """
        if not image_ids:
            return
        placeholders = ",".join("?" * len(image_ids))
        self.conn.execute(
            f"UPDATE ImageQueue SET status = ?, batch_job_id = ? "
            f"WHERE id IN ({placeholders})",
            [STATUS_PROCESSING, batch_job_id] + image_ids,
        )
        self.conn.commit()
        logger.debug("Marked %d images as Processing (batch_job_id=%s)", len(image_ids), batch_job_id)

    def mark_completed(self, image_id: int, category: str) -> None:
        """
        Mark a single image as Completed with its assigned category.

        Args:
            image_id: ImageQueue.id value.
            category: The AI-assigned category name.
        """
        self.conn.execute(
            "UPDATE ImageQueue SET status = ?, category = ? WHERE id = ?",
            (STATUS_COMPLETED, category, image_id),
        )
        self.conn.commit()
        logger.debug("Image %d completed → category: %s", image_id, category)

    def mark_completed_batch(self, results: List[Tuple[int, str]]) -> None:
        """
        Mark multiple images as Completed in a single transaction.

        Args:
            results: List of (image_id, category) tuples.
        """
        self.conn.executemany(
            "UPDATE ImageQueue SET status = ?, category = ? WHERE id = ?",
            [(STATUS_COMPLETED, cat, img_id) for img_id, cat in results],
        )
        self.conn.commit()
        logger.debug("Batch-completed %d images", len(results))

    def mark_failed(self, image_id: int) -> None:
        """
        Mark a single image as Failed and increment retry_count.

        Args:
            image_id: ImageQueue.id value.
        """
        self.conn.execute(
            "UPDATE ImageQueue SET status = ?, retry_count = retry_count + 1 WHERE id = ?",
            (STATUS_FAILED, image_id),
        )
        self.conn.commit()
        logger.debug("Image %d marked as Failed", image_id)

    def revert_to_pending(self, image_ids: List[int]) -> None:
        """
        Revert images back to 'Pending' so they can be retried.

        Used when mismatch is detected (AI returns fewer results
        than images sent) or when a batch job fails.

        Args:
            image_ids: List of ImageQueue.id values to revert.
        """
        if not image_ids:
            return
        placeholders = ",".join("?" * len(image_ids))
        self.conn.execute(
            f"UPDATE ImageQueue SET status = ?, batch_job_id = NULL "
            f"WHERE id IN ({placeholders})",
            [STATUS_PENDING] + image_ids,
        )
        self.conn.commit()
        logger.info("Reverted %d images to Pending", len(image_ids))

    def revert_to_pending_with_retry(self, image_ids: List[int]) -> None:
        """
        Revert images to Pending AND increment their retry_count.

        Used when a batch job fails entirely.

        Args:
            image_ids: List of ImageQueue.id values.
        """
        if not image_ids:
            return
        placeholders = ",".join("?" * len(image_ids))
        self.conn.execute(
            f"UPDATE ImageQueue SET status = ?, batch_job_id = NULL, "
            f"retry_count = retry_count + 1 "
            f"WHERE id IN ({placeholders})",
            [STATUS_PENDING] + image_ids,
        )
        self.conn.commit()
        logger.info("Reverted %d images to Pending (retry incremented)", len(image_ids))

    def get_images_by_batch_job(self, batch_job_id: int) -> List[Dict]:
        """
        Get all images associated with a specific batch job.

        Args:
            batch_job_id: The BatchJobs.job_id.

        Returns:
            List of image dicts.
        """
        cursor = self.conn.execute(
            "SELECT id, file_path, status, retry_count "
            "FROM ImageQueue WHERE batch_job_id = ?",
            (batch_job_id,),
        )
        return [dict(row) for row in cursor.fetchall()]

    def get_queue_stats(self) -> Dict[str, int]:
        """
        Return counts of images by status.

        Returns:
            Dict like {'Pending': 42, 'Completed': 100, ...}.
        """
        cursor = self.conn.execute(
            "SELECT status, COUNT(*) as cnt "
            "FROM ImageQueue GROUP BY status"
        )
        return {row["status"]: row["cnt"] for row in cursor.fetchall()}

    def get_total_count(self) -> int:
        """Return total number of images in the queue."""
        cursor = self.conn.execute("SELECT COUNT(*) as cnt FROM ImageQueue")
        return cursor.fetchone()["cnt"]

    # ── BatchJobs Operations ─────────────────────────────────

    def create_batch_job(self, api_job_name: str) -> int:
        """
        Record a new batch job submission.

        Args:
            api_job_name: The Gemini API job name string.

        Returns:
            The auto-generated job_id.
        """
        cursor = self.conn.execute(
            "INSERT INTO BatchJobs (api_job_name) VALUES (?)",
            (api_job_name,),
        )
        self.conn.commit()
        job_id = cursor.lastrowid
        logger.info("Created batch job: id=%d, api_name=%s", job_id, api_job_name)
        return job_id

    def get_running_batch_jobs(self) -> List[Dict]:
        """
        Fetch all batch jobs with status 'Running'.

        Returns:
            List of batch job dicts.
        """
        cursor = self.conn.execute(
            "SELECT job_id, api_job_name, status, created_at "
            "FROM BatchJobs WHERE status = ?",
            (BATCH_RUNNING,),
        )
        return [dict(row) for row in cursor.fetchall()]

    def update_batch_job_status(self, job_id: int, status: str) -> None:
        """
        Update the status of a batch job.

        Args:
            job_id: The BatchJobs.job_id.
            status: New status (Succeeded / Failed).
        """
        self.conn.execute(
            "UPDATE BatchJobs SET status = ? WHERE job_id = ?",
            (status, job_id),
        )
        self.conn.commit()
        logger.info("Batch job %d status → %s", job_id, status)

    # ── SessionStats Operations ──────────────────────────────

    def record_session(
        self,
        session_id: str,
        mode: str,
        model: str,
        images_processed: int,
        total_tokens: int,
        cost_local_currency: float,
    ) -> None:
        """
        Record statistics for the current run.

        Args:
            session_id: UUID string identifying this run.
            mode: 'standard' or 'batch'.
            model: Name of the Gemini model used.
            images_processed: Total images successfully processed.
            total_tokens: Total API tokens consumed.
            cost_local_currency: Total cost in local currency.
        """
        self.conn.execute(
            "INSERT OR REPLACE INTO SessionStats "
            "(session_id, mode, model_name, images_processed, total_tokens, cost_local_currency) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, mode, model, images_processed, total_tokens, cost_local_currency),
        )
        self.conn.commit()
        logger.info(
            "Session recorded: %s — %d images, %d tokens, cost=%.4f",
            session_id, images_processed, total_tokens, cost_local_currency,
        )

    # ── EstimationStats Operations ────────────────────────────

    def get_estimation_stats(self, model_name: str) -> Optional[Dict]:
        """
        Get the cumulative estimation stats for a specific model.

        Args:
            model_name: The name of the API model (e.g. gemini-3-flash-preview)

        Returns:
            Dict with total_images_measured, total_input_tokens,
            total_output_tokens, or None if no data yet.
        """
        cursor = self.conn.execute(
            "SELECT total_images_measured, total_input_tokens, total_output_tokens "
            "FROM EstimationStats WHERE model_name = ?",
            (model_name,)
        )
        row = cursor.fetchone()
        if row and row["total_images_measured"] > 0:
            return dict(row)
        return None

    def update_estimation_stats(
        self,
        model_name: str,
        images_in_session: int,
        input_tokens_in_session: int,
        output_tokens_in_session: int,
    ) -> None:
        """
        Add this session's actuals to the cumulative estimation stats.

        Args:
            model_name: The name of the API model used.
            images_in_session: Number of images processed this session.
            input_tokens_in_session: Total input tokens this session.
            output_tokens_in_session: Total output tokens this session.
        """
        if images_in_session <= 0:
            return
        
        # Fetch old stats for logging
        old_stats = self.get_estimation_stats(model_name)
        
        # Upsert: insert if model doesn't exist, otherwise add to existing tallies
        self.conn.execute(
            """
            INSERT INTO EstimationStats (model_name, total_images_measured, total_input_tokens, total_output_tokens)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(model_name) DO UPDATE SET
                total_images_measured = total_images_measured + excluded.total_images_measured,
                total_input_tokens = total_input_tokens + excluded.total_input_tokens,
                total_output_tokens = total_output_tokens + excluded.total_output_tokens,
                updated_on = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
            """,
            (model_name, images_in_session, input_tokens_in_session, output_tokens_in_session),
        )
        self.conn.commit()
        
        # Fetch new stats for logging
        new_stats = self.get_estimation_stats(model_name)
        if new_stats:
            new_avg_in = new_stats["total_input_tokens"] / new_stats["total_images_measured"]
            new_avg_out = new_stats["total_output_tokens"] / new_stats["total_images_measured"]
            
            if old_stats:
                old_avg_in = old_stats["total_input_tokens"] / old_stats["total_images_measured"]
                old_avg_out = old_stats["total_output_tokens"] / old_stats["total_images_measured"]
                logger.info(
                    "Cost estimates updated for %s:\n"
                    "  Previous : %.1f in, %.1f out tokens/image\n"
                    "  New      : %.1f in, %.1f out tokens/image",
                    model_name, old_avg_in, old_avg_out, new_avg_in, new_avg_out
                )
            else:
                logger.info(
                    "Cost estimates initialized for %s:\n"
                    "  New      : %.1f in, %.1f out tokens/image",
                    model_name, new_avg_in, new_avg_out
                )

    # ── Lifecycle ────────────────────────────────────────────

    def close(self) -> None:
        """Close the database connection."""
        self.conn.close()
        logger.debug("Database connection closed")
