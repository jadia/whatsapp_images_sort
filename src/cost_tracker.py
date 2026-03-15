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
from typing import Optional

from src.config_manager import AppConfig, ModelPricing

logger = logging.getLogger("whatsapp_sorter")

# ── Average token estimates per image ────────────────────────
# These are rough estimates based on 384×384 JPEG images.
# Gemini typically uses ~258 tokens for a 384×384 image.
# Text prompt overhead is ~100-150 tokens per request.
AVG_INPUT_TOKENS_PER_IMAGE = 300   # image tokens + prompt share
AVG_OUTPUT_TOKENS_PER_IMAGE = 30   # JSON response per image


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
    """

    def __init__(self, config: AppConfig):
        """
        Initialise the cost tracker with pricing config.

        Args:
            config: The validated application config.
        """
        self.pricing: ModelPricing = config.active_pricing
        self.exchange_rate: float = config.currency.usd_exchange_rate
        self.currency_symbol: str = config.currency.symbol

        # Accumulators for the session
        self._total_input_tokens: int = 0
        self._total_output_tokens: int = 0

    def estimate_cost(self, num_images: int) -> CostResult:
        """
        Estimate the cost of processing N images.

        Uses average token-per-image estimates. This is a
        pre-processing estimate shown to the user before
        any API calls are made.

        Args:
            num_images: Number of images to process.

        Returns:
            CostResult with estimated values.
        """
        est_input = num_images * AVG_INPUT_TOKENS_PER_IMAGE
        est_output = num_images * AVG_OUTPUT_TOKENS_PER_IMAGE
        return self._compute(est_input, est_output)

    def record_usage(
        self,
        input_tokens: int,
        output_tokens: int,
    ) -> CostResult:
        """
        Record actual token usage from an API response.

        Adds to the running session totals and returns the
        cost for this individual API call.

        Args:
            input_tokens: Tokens consumed for input.
            output_tokens: Tokens consumed for output.

        Returns:
            CostResult for this API call.
        """
        self._total_input_tokens += input_tokens
        self._total_output_tokens += output_tokens

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
        )
        cost_local = cost_usd * self.exchange_rate

        return CostResult(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            cost_usd=cost_usd,
            cost_local=cost_local,
            currency_symbol=self.currency_symbol,
        )
