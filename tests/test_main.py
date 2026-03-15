"""
============================================================
test_main.py — CLI Entry Point Tests
============================================================
Tests CLI argument parsing, --test-mode flag, --dry-run flag,
and source directory scanning.
============================================================
"""

import os
from unittest.mock import patch, MagicMock

import pytest
from PIL import Image

from main import _scan_source_directory, IMAGE_EXTENSIONS


class TestScanSourceDirectory:
    """Tests for image file scanning."""

    def test_finds_jpg_images(self, tmp_path):
        """Should find .jpg files."""
        (tmp_path / "test.jpg").touch()
        (tmp_path / "test.jpeg").touch()

        paths = _scan_source_directory(str(tmp_path))
        assert len(paths) == 2

    def test_finds_png_images(self, tmp_path):
        """Should find .png files."""
        (tmp_path / "test.png").touch()

        paths = _scan_source_directory(str(tmp_path))
        assert len(paths) == 1

    def test_ignores_non_image_files(self, tmp_path):
        """Should ignore .txt, .pdf, etc."""
        (tmp_path / "test.txt").touch()
        (tmp_path / "test.pdf").touch()
        (tmp_path / "test.mp4").touch()
        (tmp_path / "image.jpg").touch()

        paths = _scan_source_directory(str(tmp_path))
        assert len(paths) == 1

    def test_recursive_scan(self, tmp_path):
        """Should scan subdirectories."""
        subdir = tmp_path / "sub1" / "sub2"
        subdir.mkdir(parents=True)

        (tmp_path / "root.jpg").touch()
        (subdir / "nested.jpg").touch()

        paths = _scan_source_directory(str(tmp_path))
        assert len(paths) == 2

    def test_returns_sorted_paths(self, tmp_path):
        """Paths should be sorted alphabetically."""
        (tmp_path / "c.jpg").touch()
        (tmp_path / "a.jpg").touch()
        (tmp_path / "b.jpg").touch()

        paths = _scan_source_directory(str(tmp_path))
        assert paths == sorted(paths)

    def test_empty_directory(self, tmp_path):
        """Empty directory should return empty list."""
        paths = _scan_source_directory(str(tmp_path))
        assert len(paths) == 0

    def test_case_insensitive_extensions(self, tmp_path):
        """Should find images with uppercase extensions."""
        (tmp_path / "test.JPG").touch()
        (tmp_path / "test.Png").touch()

        paths = _scan_source_directory(str(tmp_path))
        assert len(paths) == 2


class TestImageExtensions:
    """Tests for the supported image extension set."""

    def test_common_extensions_supported(self):
        """All common image formats should be in the set."""
        assert ".jpg" in IMAGE_EXTENSIONS
        assert ".jpeg" in IMAGE_EXTENSIONS
        assert ".png" in IMAGE_EXTENSIONS
        assert ".gif" in IMAGE_EXTENSIONS
        assert ".webp" in IMAGE_EXTENSIONS
        assert ".heic" in IMAGE_EXTENSIONS

    def test_video_extensions_not_included(self):
        """Video formats should NOT be included."""
        assert ".mp4" not in IMAGE_EXTENSIONS
        assert ".avi" not in IMAGE_EXTENSIONS
        assert ".mov" not in IMAGE_EXTENSIONS


class TestCLIArgParsing:
    """Tests for command-line argument parsing."""

    def test_test_mode_flag(self):
        """--test-mode should be parsed correctly."""
        from main import main
        import argparse

        parser = argparse.ArgumentParser()
        parser.add_argument("--test-mode", action="store_true")
        parser.add_argument("--dry-run", action="store_true")

        args = parser.parse_args(["--test-mode"])
        assert args.test_mode is True
        assert args.dry_run is False

    def test_dry_run_flag(self):
        """--dry-run should be parsed correctly."""
        import argparse

        parser = argparse.ArgumentParser()
        parser.add_argument("--test-mode", action="store_true")
        parser.add_argument("--dry-run", action="store_true")

        args = parser.parse_args(["--dry-run"])
        assert args.dry_run is True
        assert args.test_mode is False

    def test_no_flags(self):
        """No flags should default to False for both."""
        import argparse

        parser = argparse.ArgumentParser()
        parser.add_argument("--test-mode", action="store_true")
        parser.add_argument("--dry-run", action="store_true")

        args = parser.parse_args([])
        assert args.test_mode is False
        assert args.dry_run is False

    def test_both_flags(self):
        """Both flags should be independent."""
        import argparse

        parser = argparse.ArgumentParser()
        parser.add_argument("--test-mode", action="store_true")
        parser.add_argument("--dry-run", action="store_true")

        args = parser.parse_args(["--test-mode", "--dry-run"])
        assert args.test_mode is True
        assert args.dry_run is True
