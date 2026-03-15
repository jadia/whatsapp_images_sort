"""
============================================================
test_database.py — Database CRUD Tests
============================================================
Tests table creation, enqueue/dedup, status transitions,
audit timestamps, batch job CRUD, and session stats.
============================================================
"""

import time

import pytest

from src.database import (
    BATCH_FAILED,
    BATCH_RUNNING,
    BATCH_SUCCEEDED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_PROCESSING,
    Database,
)


class TestTableCreation:
    """Tests for database initialisation."""

    def test_tables_exist(self, test_db):
        """All three tables should be created on init."""
        cursor = test_db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        tables = {row["name"] for row in cursor.fetchall()}
        assert "ImageQueue" in tables
        assert "BatchJobs" in tables
        assert "SessionStats" in tables

    def test_wal_mode_enabled(self, test_db):
        """WAL journal mode should be active."""
        cursor = test_db.conn.execute("PRAGMA journal_mode")
        mode = cursor.fetchone()[0]
        assert mode.lower() == "wal"


class TestImageQueue:
    """Tests for ImageQueue operations."""

    def test_enqueue_images(self, test_db):
        """Should insert new images with Pending status."""
        paths = ["/img/a.jpg", "/img/b.jpg", "/img/c.jpg"]
        inserted = test_db.enqueue_images(paths)

        assert inserted == 3
        assert test_db.get_total_count() == 3

    def test_enqueue_dedup(self, test_db):
        """Re-enqueuing the same path should skip it (UNIQUE constraint)."""
        paths = ["/img/a.jpg", "/img/b.jpg"]
        test_db.enqueue_images(paths)

        # Re-enqueue with one new, one existing
        inserted = test_db.enqueue_images(["/img/a.jpg", "/img/d.jpg"])

        assert inserted == 1  # Only d.jpg is new
        assert test_db.get_total_count() == 3

    def test_get_pending_batch(self, test_db):
        """Should return only Pending images, limited and ordered."""
        test_db.enqueue_images(["/img/1.jpg", "/img/2.jpg", "/img/3.jpg"])

        batch = test_db.get_pending_batch(limit=2)
        assert len(batch) == 2
        assert batch[0]["file_path"] == "/img/1.jpg"
        assert batch[1]["file_path"] == "/img/2.jpg"

    def test_mark_completed(self, test_db):
        """Should update status to Completed with category."""
        test_db.enqueue_images(["/img/test.jpg"])
        batch = test_db.get_pending_batch(1)
        img_id = batch[0]["id"]

        test_db.mark_completed(img_id, "People & Social")

        # Should not appear in Pending anymore
        remaining = test_db.get_pending_batch(10)
        assert len(remaining) == 0

        # Check the actual record
        cursor = test_db.conn.execute(
            "SELECT status, category FROM ImageQueue WHERE id = ?", (img_id,)
        )
        row = cursor.fetchone()
        assert row["status"] == STATUS_COMPLETED
        assert row["category"] == "People & Social"

    def test_mark_completed_batch(self, test_db):
        """Should mark multiple images as Completed."""
        test_db.enqueue_images(["/img/a.jpg", "/img/b.jpg"])
        batch = test_db.get_pending_batch(10)

        results = [(batch[0]["id"], "Memes"), (batch[1]["id"], "Documents")]
        test_db.mark_completed_batch(results)

        remaining = test_db.get_pending_batch(10)
        assert len(remaining) == 0

    def test_mark_failed(self, test_db):
        """Should mark as Failed and increment retry_count."""
        test_db.enqueue_images(["/img/fail.jpg"])
        batch = test_db.get_pending_batch(1)
        img_id = batch[0]["id"]

        test_db.mark_failed(img_id)
        test_db.mark_failed(img_id)  # Fail twice

        cursor = test_db.conn.execute(
            "SELECT status, retry_count FROM ImageQueue WHERE id = ?", (img_id,)
        )
        row = cursor.fetchone()
        assert row["status"] == STATUS_FAILED
        assert row["retry_count"] == 2

    def test_revert_to_pending(self, test_db):
        """Should revert images back to Pending."""
        test_db.enqueue_images(["/img/a.jpg", "/img/b.jpg"])
        batch = test_db.get_pending_batch(10)
        ids = [row["id"] for row in batch]

        # Mark as Processing first
        test_db.mark_processing(ids)
        pending = test_db.get_pending_batch(10)
        assert len(pending) == 0

        # Revert
        test_db.revert_to_pending(ids)
        pending = test_db.get_pending_batch(10)
        assert len(pending) == 2

    def test_revert_to_pending_with_retry(self, test_db):
        """Should revert to Pending and increment retry_count."""
        test_db.enqueue_images(["/img/retry.jpg"])
        batch = test_db.get_pending_batch(1)
        img_id = batch[0]["id"]

        test_db.mark_processing([img_id])
        test_db.revert_to_pending_with_retry([img_id])

        cursor = test_db.conn.execute(
            "SELECT status, retry_count FROM ImageQueue WHERE id = ?", (img_id,)
        )
        row = cursor.fetchone()
        assert row["status"] == STATUS_PENDING
        assert row["retry_count"] == 1

    def test_audit_timestamps_set_on_insert(self, test_db):
        """inserted_on and updated_on should be set on insert."""
        test_db.enqueue_images(["/img/audit.jpg"])

        cursor = test_db.conn.execute(
            "SELECT inserted_on, updated_on FROM ImageQueue WHERE file_path = ?",
            ("/img/audit.jpg",),
        )
        row = cursor.fetchone()
        assert row["inserted_on"] is not None
        assert row["updated_on"] is not None

    def test_audit_updated_on_changes_on_update(self, test_db):
        """updated_on should change after an update (via trigger)."""
        test_db.enqueue_images(["/img/audit2.jpg"])
        batch = test_db.get_pending_batch(1)
        img_id = batch[0]["id"]

        cursor = test_db.conn.execute(
            "SELECT updated_on FROM ImageQueue WHERE id = ?", (img_id,)
        )
        original_ts = cursor.fetchone()["updated_on"]

        # Small delay to ensure timestamp differs
        time.sleep(1.1)
        test_db.mark_completed(img_id, "Test")

        cursor = test_db.conn.execute(
            "SELECT updated_on FROM ImageQueue WHERE id = ?", (img_id,)
        )
        new_ts = cursor.fetchone()["updated_on"]
        assert new_ts != original_ts

    def test_get_queue_stats(self, test_db):
        """Should return correct counts by status."""
        test_db.enqueue_images(["/img/a.jpg", "/img/b.jpg", "/img/c.jpg"])
        batch = test_db.get_pending_batch(10)

        test_db.mark_completed(batch[0]["id"], "Cat1")
        test_db.mark_failed(batch[1]["id"])

        stats = test_db.get_queue_stats()
        assert stats[STATUS_PENDING] == 1
        assert stats[STATUS_COMPLETED] == 1
        assert stats[STATUS_FAILED] == 1


class TestBatchJobs:
    """Tests for BatchJobs operations."""

    def test_create_batch_job(self, test_db):
        """Should insert a new batch job and return its ID."""
        job_id = test_db.create_batch_job("batches/abc123")
        assert job_id is not None
        assert isinstance(job_id, int)

    def test_get_running_batch_jobs(self, test_db):
        """Should return only Running jobs."""
        test_db.create_batch_job("batches/running1")
        job_id2 = test_db.create_batch_job("batches/done1")
        test_db.update_batch_job_status(job_id2, BATCH_SUCCEEDED)

        running = test_db.get_running_batch_jobs()
        assert len(running) == 1
        assert running[0]["api_job_name"] == "batches/running1"

    def test_update_batch_job_status(self, test_db):
        """Should update the status of a batch job."""
        job_id = test_db.create_batch_job("batches/test")
        test_db.update_batch_job_status(job_id, BATCH_FAILED)

        cursor = test_db.conn.execute(
            "SELECT status FROM BatchJobs WHERE job_id = ?", (job_id,)
        )
        assert cursor.fetchone()["status"] == BATCH_FAILED

    def test_get_images_by_batch_job(self, test_db):
        """Should return images linked to a specific batch job."""
        job_id = test_db.create_batch_job("batches/link_test")
        test_db.enqueue_images(["/img/b1.jpg", "/img/b2.jpg", "/img/b3.jpg"])
        batch = test_db.get_pending_batch(10)
        ids = [row["id"] for row in batch]

        test_db.mark_processing(ids, batch_job_id=job_id)

        linked = test_db.get_images_by_batch_job(job_id)
        assert len(linked) == 3
        assert all(img["status"] == STATUS_PROCESSING for img in linked)


class TestSessionStats:
    """Tests for SessionStats operations."""

    def test_record_session(self, test_db):
        """Should insert a session stats record."""
        test_db.record_session(
            session_id="test-uuid-123",
            mode="standard",
            images_processed=42,
            total_tokens=5000,
            cost_local_currency=1.23,
        )

        cursor = test_db.conn.execute(
            "SELECT * FROM SessionStats WHERE session_id = ?",
            ("test-uuid-123",),
        )
        row = cursor.fetchone()
        assert row["mode"] == "standard"
        assert row["images_processed"] == 42
        assert row["total_tokens"] == 5000
        assert row["inserted_on"] is not None
