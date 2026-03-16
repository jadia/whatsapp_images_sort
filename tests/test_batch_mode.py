"""
============================================================
test_batch_mode.py — Batch Mode Lifecycle Tests
============================================================
Tests submit phase, resume/poll, success handling, failure
handling, and File API cleanup with mocked APIs.
============================================================
"""

import json
import os
from unittest.mock import MagicMock, patch, call

import pytest
from PIL import Image

from src.config_manager import AppConfig, CurrencyConfig, FeaturesConfig, ModelPricing, CategoryDef
from src.cost_tracker import CostTracker
from src.database import (
    BATCH_FAILED,
    BATCH_RUNNING,
    BATCH_SUCCEEDED,
    STATUS_COMPLETED,
    STATUS_PENDING,
    STATUS_PROCESSING,
    Database,
)
from src.batch_mode import run_batch_mode, _save_batch_metadata, _load_batch_metadata


@pytest.fixture
def batch_env(tmp_path):
    """Set up a complete environment for batch mode testing."""
    source = tmp_path / "source"
    output = tmp_path / "sorted"
    source.mkdir()
    output.mkdir()

    # Create test images
    image_paths = []
    for i in range(5):
        path = source / f"IMG-2024010{i+1}-WA000{i+1}.jpg"
        img = Image.new("RGB", (100, 100), color=(i * 50, 100, 200))
        img.save(str(path), format="JPEG")
        image_paths.append(str(path))

    config = AppConfig(
        api_mode="batch",
        active_model="gemini-3-flash-lite",
        batch_chunk_size=100,
        standard_club_size=3,
        upload_threads=10,
        source_dir=str(source),
        output_dir=str(output),
        features=FeaturesConfig(restore_exif_date=False),
        pricing={
            "gemini-3-flash-lite": ModelPricing(input_per_1m=0.075, output_per_1m=0.30),
        },
        currency=CurrencyConfig(symbol="₹", usd_exchange_rate=83.50),
        fallback_category="Uncategorized_Review",
        global_rules=["Only pick one.", "Be smart."],
        ignored_extensions=[".heic"],
        whatsapp_categories=[
            CategoryDef(name="People & Social", description="Test desc"),
            CategoryDef(name="Memes & Junk", description="Test desc")
        ],
        gemini_api_key="test-key",
    )

    db_path = str(tmp_path / "test.db")
    db = Database(db_path=db_path)
    db.enqueue_images(image_paths)

    cost_tracker = CostTracker(config)

    # Change to tmp_path so metadata files are created there
    original_cwd = os.getcwd()
    os.chdir(str(tmp_path))

    yield {
        "config": config,
        "db": db,
        "cost_tracker": cost_tracker,
        "image_paths": image_paths,
        "output": str(output),
        "tmp_path": tmp_path,
    }

    os.chdir(original_cwd)
    db.close()


class TestBatchMetadata:
    """Tests for batch metadata persistence."""

    def test_save_and_load_metadata(self, tmp_path):
        """Metadata should round-trip through save/load."""
        original_cwd = os.getcwd()
        os.chdir(str(tmp_path))

        uploaded = [
            {
                "label": "Image_1",
                "file_api_name": "files/abc123",
                "file_uri": "gs://bucket/abc123",
                "db_row": {"file_path": "/img/test.jpg"},
            }
        ]

        _save_batch_metadata(42, uploaded)
        loaded = _load_batch_metadata(42)

        assert loaded is not None
        assert len(loaded) == 1
        assert loaded[0]["label"] == "Image_1"
        assert loaded[0]["file_api_name"] == "files/abc123"

        os.chdir(original_cwd)

    def test_load_nonexistent_returns_none(self, tmp_path):
        """Loading metadata for a non-existent job should return None."""
        original_cwd = os.getcwd()
        os.chdir(str(tmp_path))

        result = _load_batch_metadata(99999)
        assert result is None

        os.chdir(original_cwd)


class TestBatchSubmit:
    """Tests for Phase 1 (Submit)."""

    def test_dry_run_no_uploads(self, batch_env):
        """Dry run should not upload anything."""
        processed = run_batch_mode(
            config=batch_env["config"],
            db=batch_env["db"],
            cost_tracker=batch_env["cost_tracker"],
            dry_run=True,
        )

        assert processed == 0
        stats = batch_env["db"].get_queue_stats()
        assert stats.get(STATUS_PENDING, 0) == 5

    def test_empty_queue_no_submission(self, batch_env):
        """No pending images should result in no submission."""
        db = batch_env["db"]

        # Complete all images
        for row in db.get_pending_batch(100):
            db.mark_completed(row["id"], "Done")

        processed = run_batch_mode(
            config=batch_env["config"],
            db=db,
            cost_tracker=batch_env["cost_tracker"],
        )

        assert processed == 0

    @patch("src.batch_mode._submit_batch_job")
    def test_multi_submit_loop(self, mock_submit, batch_env):
        """Phase 1 should loop and submit batches until the queue is exhausted."""
        # Setup: Mock _submit_batch_job to return True 3 times, then False.
        # This simulates a queue that requires 3 batch jobs to empty.
        mock_submit.side_effect = [True, True, True, False]
        
        # Mock get_running_batch_jobs to return empty for the Phase 2 check,
        # so run_batch_mode simply returns 0 after Phase 1 finishes.
        with patch.object(batch_env["db"], "get_running_batch_jobs", return_value=[]):
            processed = run_batch_mode(
                config=batch_env["config"],
                db=batch_env["db"],
                cost_tracker=batch_env["cost_tracker"],
            )

        assert processed == 0
        assert mock_submit.call_count == 4

    @patch("src.batch_mode._submit_batch_job")
    def test_test_mode_limits_loop(self, mock_submit, batch_env):
        """Test mode should break the submit loop after exactly one call."""
        mock_submit.return_value = True
        
        with patch.object(batch_env["db"], "get_running_batch_jobs", return_value=[]):
            processed = run_batch_mode(
                config=batch_env["config"],
                db=batch_env["db"],
                cost_tracker=batch_env["cost_tracker"],
                test_mode=True,
            )

        assert mock_submit.call_count == 1


class TestBatchSubmitIntegration:
    """Integration tests for _submit_batch_job without mocking the entire function."""

    @patch("src.batch_mode.genai")
    def test_submit_batch_job_integration(self, mock_genai, batch_env):
        """Should resize images, call upload, build jsonl, and submit job successfully."""
        from src.batch_mode import _submit_batch_job

        mock_client = MagicMock()
        mock_genai.Client.return_value = mock_client

        # Mock file uploads
        mock_file = MagicMock()
        mock_file.name = "files/test_file"
        mock_file.uri = "gs://bucket/test_file"
        mock_client.files.upload.return_value = mock_file

        # Mock batch job creation
        mock_batch = MagicMock()
        mock_batch.name = "batches/test_job_123"
        mock_client.batches.create.return_value = mock_batch

        db = batch_env["db"]
        
        # Call it directly
        submitted = _submit_batch_job(
            client=mock_client,
            config=batch_env["config"],
            db=db,
            test_mode=False,
            dry_run=False,
        )

        assert submitted is True
        
        # Verify db was updated
        running = db.get_running_batch_jobs()
        assert len(running) == 1
        assert running[0]["api_job_name"] == "batches/test_job_123"

        # Verify API calls were made (5 images + 1 jsonl = 6 uploads)
        assert mock_client.files.upload.call_count == 6
        assert mock_client.batches.create.call_count == 1


class TestBatchResume:
    """Tests for Phase 2 (Resume & Poll)."""

    def test_running_job_polls_until_keyboard_interrupt(self, batch_env):
        """Should poll if job is running and exit gracefully on Ctrl+C."""
        db = batch_env["db"]

        # Simulate a running job in the database
        job_id = db.create_batch_job("batches/test-running")
        pending = db.get_pending_batch(5)
        db.mark_processing([r["id"] for r in pending], batch_job_id=job_id)

        # Mock the Gemini client and time.sleep
        with patch("src.batch_mode.genai") as mock_genai, patch("src.batch_mode.time.sleep", side_effect=KeyboardInterrupt):
            mock_client = MagicMock()
            mock_genai.Client.return_value = mock_client

            # Mock batch job status as RUNNING
            mock_batch = MagicMock()
            mock_batch.state.name = "JOB_STATE_RUNNING"
            mock_client.batches.get.return_value = mock_batch

            with pytest.raises(KeyboardInterrupt):
                run_batch_mode(
                    config=batch_env["config"],
                    db=db,
                    cost_tracker=batch_env["cost_tracker"],
                )
        # Job should still be Running in DB
        running = db.get_running_batch_jobs()
        assert len(running) == 1

    @patch("src.batch_mode._submit_batch_job")
    def test_failed_job_reverts_images(self, mock_submit, batch_env):
        """Failed batch job should revert images to Pending."""
        mock_submit.return_value = False
        db = batch_env["db"]

        job_id = db.create_batch_job("batches/test-failed")
        pending = db.get_pending_batch(5)
        image_ids = [r["id"] for r in pending]
        db.mark_processing(image_ids, batch_job_id=job_id)

        with patch("src.batch_mode.genai") as mock_genai:
            mock_client = MagicMock()
            mock_genai.Client.return_value = mock_client

            mock_batch = MagicMock()
            mock_batch.state.name = "JOB_STATE_FAILED"
            mock_client.batches.get.return_value = mock_batch

            processed = run_batch_mode(
                config=batch_env["config"],
                db=db,
                cost_tracker=batch_env["cost_tracker"],
            )

        assert processed == 0
        # All images should be back to Pending
        stats = db.get_queue_stats()
        assert stats.get(STATUS_PENDING, 0) == 5

        # Batch job should be marked as Failed
        running = db.get_running_batch_jobs()
        assert len(running) == 0

    @patch("src.batch_mode.genai")
    @patch("src.batch_mode.move_image")
    def test_succeeded_job_downloads_and_parses(self, mock_move, mock_genai, batch_env):
        """Succeeded batch job should download response, parse it, update db, and move files."""
        db = batch_env["db"]

        job_id = db.create_batch_job("batches/test-success")
        pending = db.get_pending_batch(2)
        image_ids = [r["id"] for r in pending]
        db.mark_processing(image_ids, batch_job_id=job_id)

        # Save metadata so the resume process knows about the files
        from src.batch_mode import _save_batch_metadata
        uploaded = [
            {"label": "Image_1", "file_api_name": "files/a", "file_uri": "gs://a", "db_row": pending[0]},
            {"label": "Image_2", "file_api_name": "files/b", "file_uri": "gs://b", "db_row": pending[1]},
        ]
        _save_batch_metadata(job_id, uploaded)

        mock_client = MagicMock()
        mock_genai.Client.return_value = mock_client

        mock_batch = MagicMock()
        mock_batch.state.name = "JOB_STATE_SUCCEEDED"
        mock_batch.dest = MagicMock()
        mock_batch.dest.file_name = "files/output_job.jsonl"
        mock_client.batches.get.return_value = mock_batch

        # Mock download to provide a valid fake JSONL
        import json
        fake_jsonl = "\n".join([
            json.dumps({"key": "Image_1", "response": {"candidates": [{"content": {"parts": [{"text": '```json\n{"image": "Image_1", "category": "Cat A"}\n```'}]}}]}}),
            json.dumps({"key": "Image_2", "response": {"candidates": [{"content": {"parts": [{"text": '```json\n{"image": "Image_2", "category": "Cat B"}\n```'}]}}]}})
        ])
        mock_client.files.download.return_value = fake_jsonl.encode("utf-8")

        # Mock _submit_batch_job to prevent it from queueing the remaining 3 items
        with patch("src.batch_mode._submit_batch_job", return_value=False):
            processed = run_batch_mode(
                config=batch_env["config"],
                db=db,
                cost_tracker=batch_env["cost_tracker"],
            )

        assert processed == 2
        
        # DB checks: the 2 processing files are now Completed
        running = db.get_running_batch_jobs()
        assert len(running) == 0
        
        assert mock_move.call_count == 2
        assert mock_client.files.delete.call_count == 2


class TestFileAPICleanup:
    """Tests for File API cleanup."""

    def test_cleanup_deletes_all_files(self, batch_env):
        """Cleanup should attempt to delete all uploaded files."""
        from src.batch_mode import _cleanup_file_api

        mock_client = MagicMock()
        file_names = ["files/abc", "files/def", "files/ghi"]

        _cleanup_file_api(mock_client, batch_env["config"], file_names)

        assert mock_client.files.delete.call_count == 3

    def test_cleanup_handles_delete_errors(self, batch_env):
        """Failed deletes should be logged but not raise."""
        from src.batch_mode import _cleanup_file_api

        mock_client = MagicMock()
        mock_client.files.delete.side_effect = Exception("Quota exceeded")

        # Should not raise
        _cleanup_file_api(mock_client, batch_env["config"], ["files/abc"])

    def test_cleanup_with_none_client(self, batch_env):
        """Should handle None client gracefully."""
        from src.batch_mode import _cleanup_file_api

        # Should not raise
        _cleanup_file_api(None, batch_env["config"], ["files/abc"])
