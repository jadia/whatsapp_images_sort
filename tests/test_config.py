"""
============================================================
test_config.py — Config Manager Tests
============================================================
Tests config loading, validation, and all sanity checks.
============================================================
"""

import json
import os
from unittest.mock import patch

import pytest

from src.config_manager import load_config, AppConfig


class TestConfigLoading:
    """Tests for successful config loading."""

    def test_valid_config_loads_successfully(self, config_json_path, tmp_dirs):
        """A well-formed config.json with valid .env should load."""
        with patch.dict(os.environ, {"GEMINI_API_KEY": "test-key-123"}):
            config = load_config(config_path=config_json_path, env_path=None)

        assert isinstance(config, AppConfig)
        assert config.api_mode == "standard"
        assert config.active_model == "gemini-3-flash-lite"
        assert config.source_dir == os.path.abspath(tmp_dirs["source"])
        assert config.gemini_api_key == "test-key-123"

    def test_config_returns_frozen_dataclass(self, config_json_path):
        """Config should be immutable (frozen dataclass)."""
        with patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}):
            config = load_config(config_path=config_json_path, env_path=None)

        with pytest.raises(AttributeError):
            config.api_mode = "batch"  # type: ignore

    def test_active_pricing_property(self, config_json_path):
        """active_pricing should return pricing for the active model."""
        with patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}):
            config = load_config(config_path=config_json_path, env_path=None)

        pricing = config.active_pricing
        assert pricing.input_per_1m == 0.075
        assert pricing.output_per_1m == 0.30


class TestConfigValidation:
    """Tests for config sanity checks."""

    def _write_config(self, tmp_path, config_dict):
        """Helper to write a config.json and return its path."""
        path = os.path.join(str(tmp_path), "config.json")
        with open(path, "w") as f:
            json.dump(config_dict, f)
        return path

    def _base_config(self, tmp_path):
        """Return a base valid config dict."""
        source = os.path.join(str(tmp_path), "src_imgs")
        output = os.path.join(str(tmp_path), "out_imgs")
        os.makedirs(source, exist_ok=True)
        return {
            "api_mode": "standard",
            "active_model": "gemini-3-flash-lite",
            "source_dir": source,
            "output_dir": output,
            "pricing": {
                "gemini-3-flash-lite": {"input_per_1m": 0.075, "output_per_1m": 0.30},
            },
            "whatsapp_categories": ["Test Category"],
        }

    def test_invalid_api_mode_exits(self, tmp_path):
        """api_mode not in {standard, batch} should sys.exit."""
        cfg = self._base_config(tmp_path)
        cfg["api_mode"] = "turbo"
        path = self._write_config(tmp_path, cfg)

        with patch.dict(os.environ, {"GEMINI_API_KEY": "key"}):
            with pytest.raises(SystemExit):
                load_config(config_path=path, env_path=None)

    def test_active_model_not_in_pricing_exits(self, tmp_path):
        """active_model not found in pricing dict should sys.exit."""
        cfg = self._base_config(tmp_path)
        cfg["active_model"] = "nonexistent-model"
        path = self._write_config(tmp_path, cfg)

        with patch.dict(os.environ, {"GEMINI_API_KEY": "key"}):
            with pytest.raises(SystemExit):
                load_config(config_path=path, env_path=None)

    def test_empty_categories_exits(self, tmp_path):
        """Empty whatsapp_categories list should sys.exit."""
        cfg = self._base_config(tmp_path)
        cfg["whatsapp_categories"] = []
        path = self._write_config(tmp_path, cfg)

        with patch.dict(os.environ, {"GEMINI_API_KEY": "key"}):
            with pytest.raises(SystemExit):
                load_config(config_path=path, env_path=None)

    def test_missing_source_dir_exits(self, tmp_path):
        """Non-existent source_dir should sys.exit."""
        cfg = self._base_config(tmp_path)
        cfg["source_dir"] = "/nonexistent/path/to/images"
        path = self._write_config(tmp_path, cfg)

        with patch.dict(os.environ, {"GEMINI_API_KEY": "key"}):
            with pytest.raises(SystemExit):
                load_config(config_path=path, env_path=None)

    def test_missing_api_key_exits(self, tmp_path):
        """Missing GEMINI_API_KEY should sys.exit."""
        cfg = self._base_config(tmp_path)
        path = self._write_config(tmp_path, cfg)

        # Ensure GEMINI_API_KEY is not in env
        env = os.environ.copy()
        env.pop("GEMINI_API_KEY", None)
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(SystemExit):
                load_config(config_path=path, env_path=None)

    def test_missing_config_file_exits(self):
        """Non-existent config.json path should sys.exit."""
        with patch.dict(os.environ, {"GEMINI_API_KEY": "key"}):
            with pytest.raises(SystemExit):
                load_config(config_path="/nonexistent/config.json", env_path=None)

    def test_invalid_json_exits(self, tmp_path):
        """Malformed JSON in config file should sys.exit."""
        path = os.path.join(str(tmp_path), "config.json")
        with open(path, "w") as f:
            f.write("{invalid json content")

        with patch.dict(os.environ, {"GEMINI_API_KEY": "key"}):
            with pytest.raises(SystemExit):
                load_config(config_path=path, env_path=None)

    def test_output_dir_created_if_missing(self, tmp_path):
        """output_dir should be auto-created if it doesn't exist."""
        cfg = self._base_config(tmp_path)
        cfg["output_dir"] = os.path.join(str(tmp_path), "new_output_dir")
        path = self._write_config(tmp_path, cfg)

        with patch.dict(os.environ, {"GEMINI_API_KEY": "key"}):
            config = load_config(config_path=path, env_path=None)

        assert os.path.isdir(config.output_dir)

    def test_api_mode_defaults_to_standard(self, tmp_path):
        """Missing api_mode should default to 'standard'."""
        cfg = self._base_config(tmp_path)
        del cfg["api_mode"]
        path = self._write_config(tmp_path, cfg)

        with patch.dict(os.environ, {"GEMINI_API_KEY": "key"}):
            config = load_config(config_path=path, env_path=None)

        assert config.api_mode == "standard"
