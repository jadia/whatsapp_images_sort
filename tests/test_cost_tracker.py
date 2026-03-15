"""
============================================================
test_cost_tracker.py — Cost Tracker Tests
============================================================
Tests cost computation, record_usage with images_in_request,
calibration from DB, and estimation actuals.
============================================================
"""

import pytest

from src.config_manager import AppConfig, CurrencyConfig, FeaturesConfig, ModelPricing
from src.cost_tracker import CostTracker, AVG_INPUT_TOKENS_PER_IMAGE, AVG_OUTPUT_TOKENS_PER_IMAGE


@pytest.fixture
def tracker(sample_config):
    """Return a CostTracker initialised with the sample config."""
    return CostTracker(sample_config)


class TestCostComputation:
    """Tests for basic cost computation."""

    def test_estimate_uses_defaults(self, tracker):
        """estimate_cost should use default per-image averages."""
        result = tracker.estimate_cost(10)
        expected_input = 10 * AVG_INPUT_TOKENS_PER_IMAGE
        expected_output = 10 * AVG_OUTPUT_TOKENS_PER_IMAGE
        assert result.input_tokens == expected_input
        assert result.output_tokens == expected_output

    def test_cost_result_format_display(self, tracker):
        """format_display should produce a readable string."""
        result = tracker.estimate_cost(1)
        display = result.format_display()
        assert "Tokens:" in display
        assert "Cost:" in display
        assert "USD" in display

    def test_zero_images_zero_cost(self, tracker):
        """Estimating for 0 images should produce 0 cost."""
        result = tracker.estimate_cost(0)
        assert result.cost_usd == 0.0
        assert result.total_tokens == 0


class TestRecordUsage:
    """Tests for record_usage with images_in_request."""

    def test_record_usage_single_image(self, tracker):
        """Default images_in_request=1 should increment by 1."""
        tracker.record_usage(input_tokens=1000, output_tokens=50)
        images, inp, out = tracker.get_estimation_actuals()
        assert images == 1
        assert inp == 1000
        assert out == 50

    def test_record_usage_batch_of_10(self, tracker):
        """images_in_request=10 should increment _images_processed by 10."""
        tracker.record_usage(
            input_tokens=10000,
            output_tokens=500,
            images_in_request=10,
        )
        images, inp, out = tracker.get_estimation_actuals()
        assert images == 10
        assert inp == 10000
        assert out == 500

    def test_record_usage_accumulates(self, tracker):
        """Multiple calls should accumulate tokens and image counts."""
        tracker.record_usage(input_tokens=1000, output_tokens=50, images_in_request=5)
        tracker.record_usage(input_tokens=2000, output_tokens=100, images_in_request=3)
        images, inp, out = tracker.get_estimation_actuals()
        assert images == 8
        assert inp == 3000
        assert out == 150

    def test_session_total_matches_accumulated(self, tracker):
        """get_session_total should reflect all recorded usage."""
        tracker.record_usage(input_tokens=500, output_tokens=25, images_in_request=1)
        tracker.record_usage(input_tokens=1500, output_tokens=75, images_in_request=3)
        total = tracker.get_session_total()
        assert total.input_tokens == 2000
        assert total.output_tokens == 100
        assert total.total_tokens == 2100


class TestCalibration:
    """Tests for calibrate_from_db."""

    def test_calibrate_overrides_defaults(self, tracker):
        """Calibration should override default per-image averages."""
        tracker.calibrate_from_db({
            "total_images_measured": 100,
            "total_input_tokens": 100000,
            "total_output_tokens": 3000,
        })
        result = tracker.estimate_cost(10)
        # 100000/100 = 1000 per image, 3000/100 = 30 per image
        assert result.input_tokens == 10000
        assert result.output_tokens == 300

    def test_calibrate_with_none_keeps_defaults(self, tracker):
        """None stats should keep default averages."""
        tracker.calibrate_from_db(None)
        result = tracker.estimate_cost(1)
        assert result.input_tokens == AVG_INPUT_TOKENS_PER_IMAGE
        assert result.output_tokens == AVG_OUTPUT_TOKENS_PER_IMAGE

    def test_calibrate_with_zero_images_keeps_defaults(self, tracker):
        """Zero total_images_measured should keep defaults (avoid division by zero)."""
        tracker.calibrate_from_db({
            "total_images_measured": 0,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
        })
        result = tracker.estimate_cost(1)
        assert result.input_tokens == AVG_INPUT_TOKENS_PER_IMAGE

    def test_total_tokens_property(self, tracker):
        """total_tokens property should track session usage."""
        assert tracker.total_tokens == 0
        tracker.record_usage(input_tokens=100, output_tokens=50)
        assert tracker.total_tokens == 150
