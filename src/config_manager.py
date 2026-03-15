"""
============================================================
config_manager.py — Configuration Loading & Validation
============================================================
Loads config.json and .env, validates all fields (sanity
checks), and returns an immutable AppConfig dataclass.

Pre-flight checks (exits with clear error on failure):
  1. active_model exists as a key in pricing
  2. api_mode is "standard" or "batch"
  3. whatsapp_categories is a non-empty list
  4. source_dir exists and is readable
  5. output_dir exists (or is creatable) and is writable
  6. GEMINI_API_KEY is set and non-empty

The .env file is loaded with override=True so that the
.env value for GEMINI_API_KEY always wins over any
pre-existing environment variable.
============================================================
"""

import json
import logging
import os
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from dotenv import load_dotenv

logger = logging.getLogger("whatsapp_sorter")

# ── Valid values ─────────────────────────────────────────────
VALID_API_MODES = {"standard", "batch"}


# ── Immutable configuration container ────────────────────────
@dataclass(frozen=True)
class CurrencyConfig:
    """Currency display and conversion settings."""
    symbol: str
    usd_exchange_rate: float


@dataclass(frozen=True)
class FeaturesConfig:
    """Feature toggle flags."""
    restore_exif_date: bool = True


@dataclass(frozen=True)
class ModelPricing:
    """Per-model pricing in USD per 1 million tokens."""
    input_per_1m: float
    output_per_1m: float


@dataclass(frozen=True)
class AppConfig:
    """
    Complete, validated application configuration.

    All fields are validated at construction time. Once created
    this object is immutable (frozen dataclass).
    """
    api_mode: str
    active_model: str
    batch_chunk_size: int
    standard_club_size: int
    upload_threads: int
    source_dir: str
    output_dir: str
    features: FeaturesConfig
    pricing: Dict[str, ModelPricing]
    currency: CurrencyConfig
    whatsapp_categories: List[str]
    gemini_api_key: str

    # ── Convenience helpers ──────────────────────────────────
    @property
    def active_pricing(self) -> ModelPricing:
        """Return pricing for the currently active model."""
        return self.pricing[self.active_model]


# ── Error helper ─────────────────────────────────────────────
def _fail(message: str) -> None:
    """
    Log a CRITICAL config error and exit immediately.

    This provides a clear, user-friendly error message in the
    terminal and ensures the process stops before any work
    is attempted with an invalid configuration.
    """
    logger.critical("CONFIG ERROR: %s", message)
    sys.exit(1)


# ── Main loader ──────────────────────────────────────────────
def load_config(
    config_path: str = "config.json",
    env_path: Optional[str] = ".env",
) -> AppConfig:
    """
    Load config.json + .env and return a validated AppConfig.

    Args:
        config_path: Path to the JSON configuration file.
        env_path: Path to the .env file (or None to skip).

    Returns:
        Validated, immutable AppConfig instance.

    Exits:
        Calls sys.exit(1) with a clear error if any check fails.
    """
    # ── Step 1: Load .env (override=True so .env always wins) ─
    if env_path and os.path.exists(env_path):
        load_dotenv(env_path, override=True)
        logger.debug("Loaded .env from: %s (override=True)", env_path)
    else:
        logger.debug("No .env file found at: %s — relying on env vars", env_path)

    # ── Step 2: Load config.json ─────────────────────────────
    if not os.path.isfile(config_path):
        _fail(f"Configuration file not found: {config_path}")

    try:
        with open(config_path, "r", encoding="utf-8") as fh:
            raw: dict = json.load(fh)
        logger.debug("Loaded config from: %s", config_path)
    except json.JSONDecodeError as exc:
        _fail(f"Invalid JSON in {config_path}: {exc}")

    # ── Step 3: Extract & validate each field ────────────────

    # 3a. api_mode
    api_mode = raw.get("api_mode", "standard")
    if api_mode not in VALID_API_MODES:
        _fail(
            f"'api_mode' must be one of {VALID_API_MODES}, "
            f"got: '{api_mode}'"
        )

    # 3b. whatsapp_categories
    categories = raw.get("whatsapp_categories", [])
    if not isinstance(categories, list) or len(categories) == 0:
        _fail("'whatsapp_categories' must be a non-empty list.")

    # 3c. pricing
    pricing_raw = raw.get("pricing", {})
    if not isinstance(pricing_raw, dict) or len(pricing_raw) == 0:
        _fail("'pricing' must be a non-empty dictionary of model pricing.")

    pricing: Dict[str, ModelPricing] = {}
    for model_name, price_data in pricing_raw.items():
        try:
            pricing[model_name] = ModelPricing(
                input_per_1m=float(price_data["input_per_1m"]),
                output_per_1m=float(price_data["output_per_1m"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            _fail(
                f"Invalid pricing for model '{model_name}': {exc}. "
                f"Expected keys: 'input_per_1m', 'output_per_1m'."
            )

    # 3d. active_model must exist in pricing
    active_model = raw.get("active_model", "")
    if active_model not in pricing:
        _fail(
            f"'active_model' ('{active_model}') must be a key in "
            f"the 'pricing' dictionary. Available: {list(pricing.keys())}"
        )

    # 3e. source_dir — must exist and be readable
    source_dir = raw.get("source_dir", "")
    if not source_dir:
        _fail("'source_dir' must be specified in config.json.")
    if not os.path.isdir(source_dir):
        _fail(f"'source_dir' does not exist or is not a directory: {source_dir}")
    if not os.access(source_dir, os.R_OK):
        _fail(f"'source_dir' is not readable: {source_dir}")

    # 3f. output_dir — must exist or be creatable, must be writable
    output_dir = raw.get("output_dir", "")
    if not output_dir:
        _fail("'output_dir' must be specified in config.json.")
    # Attempt to create if it doesn't exist
    try:
        os.makedirs(output_dir, exist_ok=True)
    except OSError as exc:
        _fail(f"Cannot create 'output_dir' ({output_dir}): {exc}")
    if not os.access(output_dir, os.W_OK):
        _fail(f"'output_dir' is not writable: {output_dir}")

    # 3g. GEMINI_API_KEY
    gemini_api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not gemini_api_key:
        _fail(
            "GEMINI_API_KEY is not set. Create a .env file with:\n"
            "  GEMINI_API_KEY=your-api-key-here\n"
            "Or set it as an environment variable."
        )

    # 3h. features
    features_raw = raw.get("features", {})
    features = FeaturesConfig(
        restore_exif_date=bool(features_raw.get("restore_exif_date", True)),
    )

    # 3i. currency
    currency_raw = raw.get("currency", {})
    try:
        currency = CurrencyConfig(
            symbol=str(currency_raw.get("symbol", "$")),
            usd_exchange_rate=float(currency_raw.get("usd_exchange_rate", 1.0)),
        )
    except (TypeError, ValueError) as exc:
        _fail(f"Invalid 'currency' config: {exc}")

    # 3j. numeric fields
    batch_chunk_size = int(raw.get("batch_chunk_size", 1000))
    standard_club_size = int(raw.get("standard_club_size", 10))
    upload_threads = int(raw.get("upload_threads", 10))
    if not (1 <= upload_threads <= 50):
        _fail(f"'upload_threads' must be between 1 and 50, got: {upload_threads}")

    # ── Step 4: Build and return the frozen config ───────────
    config = AppConfig(
        api_mode=api_mode,
        active_model=active_model,
        batch_chunk_size=batch_chunk_size,
        standard_club_size=standard_club_size,
        upload_threads=upload_threads,
        source_dir=os.path.abspath(source_dir),
        output_dir=os.path.abspath(output_dir),
        features=features,
        pricing=pricing,
        currency=currency,
        whatsapp_categories=list(categories),
        gemini_api_key=gemini_api_key,
    )

    logger.info(
        "Config loaded — mode=%s, model=%s, source=%s, output=%s",
        config.api_mode,
        config.active_model,
        config.source_dir,
        config.output_dir,
    )
    logger.debug("Full config: %s", config)

    return config
