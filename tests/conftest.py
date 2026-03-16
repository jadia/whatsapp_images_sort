"""
============================================================
conftest.py — Shared Test Fixtures
============================================================
Provides common fixtures for all test modules:

- tmp_dirs: Creates temporary source/output directories
- sample_config: Returns a valid AppConfig for testing
- test_db: Creates a fresh in-memory (or temp file) Database
- sample_images: Creates small test JPEG files
============================================================
"""

import json
import os
import tempfile
from datetime import datetime
from typing import Generator
from unittest.mock import patch

import pytest
from PIL import Image

from src.models.config import AppConfig, CategoryDef, CurrencyConfig, FeaturesConfig, ModelPricing
from src.utils.database import Database


@pytest.fixture
def tmp_dirs(tmp_path):
    """
    Create temporary source and output directories.

    Yields a dict with 'source' and 'output' paths.
    """
    source = tmp_path / "source"
    output = tmp_path / "sorted"
    source.mkdir()
    output.mkdir()
    return {"source": str(source), "output": str(output), "root": str(tmp_path)}


@pytest.fixture
def sample_config(tmp_dirs):
    """
    Return a valid AppConfig for testing.

    Uses temporary directories and a fake API key.
    """
    return AppConfig(
        api_mode="standard",
        active_model="gemini-3-flash-lite",
        batch_chunk_size=100,
        standard_club_size=3,
        upload_threads=10,
        source_dir=tmp_dirs["source"],
        output_dir=tmp_dirs["output"],
        features=FeaturesConfig(restore_exif_date=False),
        pricing={
            "gemini-3-flash-lite": ModelPricing(input_per_1m=0.075, output_per_1m=0.30),
            "gemini-3-flash": ModelPricing(input_per_1m=0.35, output_per_1m=1.05),
        },
        currency=CurrencyConfig(symbol="₹", usd_exchange_rate=83.50),
        fallback_category="Uncategorized_Review",
        global_rules=["Only pick one.", "Be smart."],
        ignored_extensions=[".heic"],
        whatsapp_categories=[
            CategoryDef(name="Documents & IDs", description="All kinds of docs"),
            CategoryDef(name="People & Social", description="Photos of people"),
            CategoryDef(name="Memes & Junk", description="Internet memes and forwards"),
        ],
        gemini_api_key="test-fake-api-key-12345",
    )


@pytest.fixture
def test_db(tmp_dirs):
    """
    Create a fresh Database instance using a temp file.

    Automatically closes on teardown.
    """
    db_path = os.path.join(tmp_dirs["root"], "test_state.db")
    db = Database(db_path=db_path)
    yield db
    db.close()


@pytest.fixture
def sample_images(tmp_dirs):
    """
    Create small test JPEG images in the source directory.

    Returns a list of absolute paths to the created images.
    """
    paths = []
    filenames = [
        "IMG-20240115-WA0001.jpg",
        "IMG-20240220-WA0002.jpg",
        "photo_20230510_123456.jpg",
        "random_image.jpg",        # No date in filename
        "IMG-20241331-WA0005.jpg",  # Invalid date (month 13)
    ]

    source_dir = tmp_dirs["source"]
    for fname in filenames:
        path = os.path.join(source_dir, fname)
        # Create a small 100x100 test image
        img = Image.new("RGB", (100, 100), color=(128, 128, 128))
        img.save(path, format="JPEG")
        paths.append(path)

    return paths


@pytest.fixture
def large_sample_image(tmp_dirs):
    """
    Create a single larger test image (800x600) for resize testing.

    Returns the absolute path.
    """
    path = os.path.join(tmp_dirs["source"], "large_IMG-20240301-WA0001.jpg")
    img = Image.new("RGB", (800, 600), color=(200, 100, 50))
    img.save(path, format="JPEG")
    return path


@pytest.fixture
def config_json_path(tmp_dirs, sample_config):
    """
    Write a valid config.json to a temp directory.

    Returns the path to the config file.
    """
    config_dict = {
        "api_mode": sample_config.api_mode,
        "active_model": sample_config.active_model,
        "batch_chunk_size": sample_config.batch_chunk_size,
        "standard_club_size": sample_config.standard_club_size,
        "source_dir": sample_config.source_dir,
        "output_dir": sample_config.output_dir,
        "features": {"restore_exif_date": sample_config.features.restore_exif_date},
        "pricing": {
            name: {"input_per_1m": p.input_per_1m, "output_per_1m": p.output_per_1m}
            for name, p in sample_config.pricing.items()
        },
        "currency": {
            "symbol": sample_config.currency.symbol,
            "usd_exchange_rate": sample_config.currency.usd_exchange_rate,
        },
        "fallback_category": sample_config.fallback_category,
        "global_rules": sample_config.global_rules,
        "whatsapp_categories": [
            {"name": cat.name, "description": cat.description} 
            for cat in sample_config.whatsapp_categories
        ],
    }

    config_path = os.path.join(tmp_dirs["root"], "config.json")
    with open(config_path, "w") as f:
        json.dump(config_dict, f)

    return config_path
