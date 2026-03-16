"""
============================================================
test_standard_mode.py — Standard Mode Integration Tests
============================================================
Tests the full standard processing flow with mocked API.
============================================================
"""

import json
import os
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image

from src.config_manager import AppConfig, CurrencyConfig, FeaturesConfig, ModelPricing, CategoryDef
from src.cost_tracker import CostTracker
from src.database import Database, STATUS_COMPLETED, STATUS_PENDING
from src.standard_mode import run_standard_mode


@pytest.fixture
def std_env(tmp_path):
    """Set up a complete environment for standard mode testing."""
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

    # Config
    config = AppConfig(
        api_mode="standard",
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
            CategoryDef(name="Memes & Junk", description="Test desc"),
        ],
        gemini_api_key="test-key",
    )

    # Database
    db_path = str(tmp_path / "test.db")
    db = Database(db_path=db_path)
    db.enqueue_images(image_paths)

    cost_tracker = CostTracker(config)

    return {
        "config": config,
        "db": db,
        "cost_tracker": cost_tracker,
        "image_paths": image_paths,
        "output": str(output),
    }


def _mock_api_response(results_json, input_tokens=100, output_tokens=20):
    """Create a mock Gemini API response object."""
    mock_response = MagicMock()
    mock_response.text = json.dumps(results_json)

    mock_usage = MagicMock()
    mock_usage.prompt_token_count = input_tokens
    mock_usage.candidates_token_count = output_tokens
    mock_response.usage_metadata = mock_usage

    return mock_response


class TestStandardModeFullFlow:
    """Tests for complete standard mode processing."""

    @patch("src.standard_mode.genai")
    def test_single_batch_processes_correctly(self, mock_genai, std_env):
        """Should process one batch of images and move them."""
        config = std_env["config"]
        db = std_env["db"]

        # Mock API to return classification for 3 images
        api_results = [
            {"image": "Image_1", "category": "People & Social"},
            {"image": "Image_2", "category": "Memes & Junk"},
            {"image": "Image_3", "category": "People & Social"},
        ]
        mock_client = MagicMock()
        mock_client.models.generate_content.return_value = _mock_api_response(api_results)
        mock_genai.Client.return_value = mock_client

        processed = run_standard_mode(
            config=config,
            db=db,
            cost_tracker=std_env["cost_tracker"],
            test_mode=True,  # Only 1 batch
        )

        assert processed == 3
        stats = db.get_queue_stats()
        assert stats.get(STATUS_COMPLETED, 0) == 3
        assert stats.get(STATUS_PENDING, 0) == 2  # 5 total - 3 processed

    @patch("src.standard_mode.genai")
    def test_mismatch_reverts_missing_images(self, mock_genai, std_env):
        """If AI returns fewer results, unmatched images revert to Pending."""
        config = std_env["config"]
        db = std_env["db"]

        # Mock API returns only 2 of 3 images
        api_results = [
            {"image": "Image_1", "category": "People & Social"},
            {"image": "Image_3", "category": "Memes & Junk"},
            # Image_2 is missing!
        ]
        mock_client = MagicMock()
        mock_client.models.generate_content.return_value = _mock_api_response(api_results)
        mock_genai.Client.return_value = mock_client

        processed = run_standard_mode(
            config=config,
            db=db,
            cost_tracker=std_env["cost_tracker"],
            test_mode=True,
        )

        assert processed == 2
        stats = db.get_queue_stats()
        # 2 completed, 1 reverted to pending + 2 never touched = 3 pending
        assert stats.get(STATUS_COMPLETED, 0) == 2
        assert stats.get(STATUS_PENDING, 0) == 3

    @patch("src.standard_mode.genai")
    def test_api_error_reverts_batch(self, mock_genai, std_env):
        """API failure should revert entire batch to Pending."""
        config = std_env["config"]
        db = std_env["db"]

        mock_client = MagicMock()
        mock_client.models.generate_content.side_effect = Exception("API Error 500")
        mock_genai.Client.return_value = mock_client

        processed = run_standard_mode(
            config=config,
            db=db,
            cost_tracker=std_env["cost_tracker"],
            test_mode=True,  # MUST set test_mode to avoid infinite retry loop
        )

        assert processed == 0
        stats = db.get_queue_stats()
        # 3 were attempted and reverted, 2 never touched = 5 still pending
        assert stats.get(STATUS_PENDING, 0) == 5

    @patch("src.standard_mode.genai")
    def test_invalid_json_response_reverts_batch(self, mock_genai, std_env):
        """Invalid JSON response should revert the batch."""
        config = std_env["config"]
        db = std_env["db"]

        mock_response = MagicMock()
        mock_response.text = "This is not valid JSON at all!"
        mock_response.usage_metadata = None

        mock_client = MagicMock()
        mock_client.models.generate_content.return_value = mock_response
        mock_genai.Client.return_value = mock_client

        processed = run_standard_mode(
            config=config,
            db=db,
            cost_tracker=std_env["cost_tracker"],
            test_mode=True,
        )

        assert processed == 0

    def test_dry_run_no_api_calls(self, std_env):
        """Dry run should not call the API or move files."""
        config = std_env["config"]
        db = std_env["db"]

        # No mock needed — should never reach API
        run_standard_mode(
            config=config,
            db=db,
            cost_tracker=std_env["cost_tracker"],
            dry_run=True,
        )

        # All images should still be Pending
        stats = db.get_queue_stats()
        assert stats.get(STATUS_PENDING, 0) == 5

    @patch("src.standard_mode.genai")
    def test_files_moved_to_correct_directories(self, mock_genai, std_env):
        """Processed files should appear in output/Category/Date/ dirs."""
        config = std_env["config"]
        db = std_env["db"]

        api_results = [
            {"image": "Image_1", "category": "People & Social"},
            {"image": "Image_2", "category": "Memes & Junk"},
            {"image": "Image_3", "category": "People & Social"},
        ]
        mock_client = MagicMock()
        mock_client.models.generate_content.return_value = _mock_api_response(api_results)
        mock_genai.Client.return_value = mock_client

        run_standard_mode(
            config=config,
            db=db,
            cost_tracker=std_env["cost_tracker"],
            test_mode=True,
        )

        output = std_env["output"]
        # Check that category directories were created
        assert os.path.isdir(os.path.join(output, "People & Social"))
        assert os.path.isdir(os.path.join(output, "Memes & Junk"))

    def test_empty_queue_exits_cleanly(self, std_env):
        """Should exit cleanly if no pending images."""
        config = std_env["config"]
        db = std_env["db"]

        # Complete all images manually
        pending = db.get_pending_batch(100)
        for row in pending:
            db.mark_completed(row["id"], "Done")

        processed = run_standard_mode(
            config=config,
            db=db,
            cost_tracker=std_env["cost_tracker"],
        )

        assert processed == 0
