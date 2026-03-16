"""
============================================================
cost_tracker.py — Token & Cost Accounting
============================================================
Provides:

1. **Pre-processing cost estimation** — Before running, estimate
   expected cost based on image count × average tokens per
   image × model pricing from config.

2. **Post-processing actual cost** — Compute real cost from
   API usage_metadata and display in local currency.
============================================================
"""

import logging
from dataclasses import dataclass
from typing import Optional, Tuple

from src.models.config import AppConfig, ModelPricing

logger = logging.getLogger("whatsapp_sorter")

# ── Average token estimates per image ────────────────────────
# Calibrated from a real 10-image batch with gemini-3-flash-preview:
#   Observed: 11,186 input tokens / 10 images = ~1,119 input/image
#             269 output tokens   / 10 images = ~27   output/image
# Vision tokens for a 384×384 JPEG dominate the input side at
# roughly 1,000–1,100 tokens depending on image complexity.
# The text prompt adds ~50–150 tokens shared across all images.
AVG_INPUT_TOKENS_PER_IMAGE = 1_187  # vision tokens + prompt share
AVG_OUTPUT_TOKENS_PER_IMAGE = 19    # JSON response per image


@dataclass
class CostResult:
    """Container for cost calculation results."""
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cost_usd: float
    cost_local: float
    currency_symbol: str

    def format_display(self) -> str:
        """Format cost as a user-friendly string."""
        return (
            f"Tokens: {self.total_tokens:,} "
            f"(in: {self.input_tokens:,}, out: {self.output_tokens:,}) | "
            f"Cost: ${self.cost_usd:.6f} USD "
            f"({self.currency_symbol}{self.cost_local:.4f})"
        )


class CostTracker:
    """
    Tracks and computes API costs for the current session.

    Accumulates token counts across multiple API calls and
    computes costs using the pricing configuration.

    Supports self-calibrating estimates: if historical data
    is available in the database, per-image token averages
    are computed from actual past usage rather than hardcoded.
    """

    def __init__(self, config: AppConfig, discount_multiplier: float = 1.0):
        """
        Initialise the cost tracker with pricing config.

        Args:
            config: The validated application config.
            discount_multiplier: Scaling factor for final costs (e.g. 0.5 for batch mode).
        """
        self.pricing: ModelPricing = config.active_pricing
        self.exchange_rate: float = config.currency.usd_exchange_rate
        self.currency_symbol: str = config.currency.symbol
        self.discount_multiplier: float = discount_multiplier

        # Per-image token estimates (defaults, overridden by calibrate_from_db)
        self._avg_input = AVG_INPUT_TOKENS_PER_IMAGE
        self._avg_output = AVG_OUTPUT_TOKENS_PER_IMAGE
        self._calibrated = False

        # Accumulators for the session
        self._total_input_tokens: int = 0
        self._total_output_tokens: int = 0
        self._images_processed: int = 0

    def calibrate_from_db(self, estimation_stats: dict | None) -> None:
        """
        Override per-image token estimates with historical actuals.

        Called once at script start with data from EstimationStats.

        Args:
            estimation_stats: Dict from db.get_estimation_stats(), or
                None if no history exists (uses defaults).
        """
        if estimation_stats is None:
            logger.debug("No historical estimation data — using defaults")
            return

        total_images = estimation_stats["total_images_measured"]
        if total_images <= 0:
            return

        self._avg_input = estimation_stats["total_input_tokens"] // total_images
        self._avg_output = estimation_stats["total_output_tokens"] // total_images
        self._calibrated = True
        logger.info(
            "Cost estimates calibrated from %d historical images: "
            "avg_in=%d, avg_out=%d tokens/image",
            total_images, self._avg_input, self._avg_output,
        )

    def estimate_cost(self, num_images: int) -> CostResult:
        """
        Estimate the cost of processing N images.

        Uses average token-per-image estimates. This is a
        pre-processing estimate shown to the user before
        any API calls are made.

        Args:
            num_images: Number of images to process.

        Returns:
            CostResult with estimated values based on either historical
            actuals (if calibrated) or fallback defaults.
        """
        est_input = num_images * self._avg_input
        est_output = num_images * self._avg_output
        return self._compute(est_input, est_output)

    def record_usage(
        self,
        input_tokens: int,
        output_tokens: int,
        images_in_request: int = 1,
    ) -> CostResult:
        """
        Record actual token usage from an API response.

        Adds to the running session totals and returns the
        cost for this individual API call.

        Args:
            input_tokens: Tokens consumed for input.
            output_tokens: Tokens consumed for output.
            images_in_request: Number of images processed in this call.

        Returns:
            CostResult for this API call.
        """
        self._total_input_tokens += input_tokens
        self._total_output_tokens += output_tokens
        self._images_processed += images_in_request

        result = self._compute(input_tokens, output_tokens)
        logger.debug("API call cost: %s", result.format_display())
        return result

    def get_session_total(self) -> CostResult:
        """
        Get the accumulated cost for the entire session.

        Returns:
            CostResult with session totals.
        """
        return self._compute(self._total_input_tokens, self._total_output_tokens)

    def get_estimation_actuals(self) -> Tuple[int, int, int]:
        """
        Get the session's actual numbers for updating estimation averages.

        Returns:
            (images_processed, total_input_tokens, total_output_tokens)
        """
        return (
            self._images_processed,
            self._total_input_tokens,
            self._total_output_tokens,
        )
        return self._compute(self._total_input_tokens, self._total_output_tokens)

    @property
    def total_tokens(self) -> int:
        """Total tokens consumed in this session."""
        return self._total_input_tokens + self._total_output_tokens

    def _compute(self, input_tokens: int, output_tokens: int) -> CostResult:
        """
        Compute cost from token counts using configured pricing.

        Pricing is per 1 million tokens, so we divide by 1M.

        Args:
            input_tokens: Number of input tokens.
            output_tokens: Number of output tokens.

        Returns:
            CostResult with computed values.
        """
        cost_usd = (
            (input_tokens / 1_000_000) * self.pricing.input_per_1m
            + (output_tokens / 1_000_000) * self.pricing.output_per_1m
        ) * self.discount_multiplier
        
        cost_local = cost_usd * self.exchange_rate

        return CostResult(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            cost_usd=cost_usd,
            cost_local=cost_local,
            currency_symbol=self.currency_symbol,
        )
