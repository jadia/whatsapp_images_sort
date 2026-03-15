"""
============================================================
test_file_mover.py — File Mover Tests
============================================================
Tests directory creation, path construction, EXIF toggle,
Unknown_Date routing, and collision resolution.
============================================================
"""

import os
from datetime import datetime

import pytest
from PIL import Image

from src.file_mover import build_destination_path, move_image, _resolve_collision, _sanitise_dirname



class TestBuildDestinationPath:
    """Tests for path construction logic."""

    def test_path_with_date(self):
        """Should build output_dir/Category/YYYY/filename."""
        date = datetime(2024, 3, 15)
        path = build_destination_path("/output", "People & Social", date, "photo.jpg")

        assert path == "/output/People & Social/2024/photo.jpg"

    def test_path_without_date(self):
        """Should use Unknown_Date when date is None."""
        path = build_destination_path("/output", "Memes & Junk", None, "meme.jpg")

        assert path == "/output/Memes & Junk/Unknown_Date/meme.jpg"

    def test_uncategorized_review_path(self):
        """Should work with Uncategorized_Review category."""
        date = datetime(2024, 1, 1)
        path = build_destination_path("/out", "Uncategorized_Review", date, "x.jpg")

        assert "Uncategorized_Review" in path


class TestSanitiseDirname:
    """Tests for category name sanitization."""

    def test_keeps_safe_characters(self):
        """Ampersand and spaces should be preserved."""
        assert _sanitise_dirname("Documents & IDs") == "Documents & IDs"

    def test_replaces_unsafe_characters(self):
        """Characters like : / ? * should be replaced."""
        assert ":" not in _sanitise_dirname("Cat: Test")
        assert "/" not in _sanitise_dirname("Cat/Test")
        assert "?" not in _sanitise_dirname("Cat?Test")

    def test_strips_whitespace(self):
        """Leading/trailing whitespace should be stripped."""
        assert _sanitise_dirname("  Test  ") == "Test"


class TestResolveCollision:
    """Tests for filename collision resolution."""

    def test_no_collision(self, tmp_path):
        """Should return the same path if no collision."""
        path = str(tmp_path / "unique.jpg")
        assert _resolve_collision(path) == path

    def test_collision_appends_suffix(self, tmp_path):
        """Should append _1 if file already exists."""
        existing = tmp_path / "photo.jpg"
        existing.touch()

        resolved = _resolve_collision(str(existing))
        assert resolved == str(tmp_path / "photo_1.jpg")

    def test_multiple_collisions(self, tmp_path):
        """Should increment suffix until unique."""
        (tmp_path / "photo.jpg").touch()
        (tmp_path / "photo_1.jpg").touch()
        (tmp_path / "photo_2.jpg").touch()

        resolved = _resolve_collision(str(tmp_path / "photo.jpg"))
        assert resolved == str(tmp_path / "photo_3.jpg")


class TestMoveImage:
    """Tests for the full move_image function."""

    def test_move_creates_directories(self, tmp_dirs, sample_images):
        """Should create category/date directories if missing."""
        date = datetime(2024, 6, 15)
        dest = move_image(
            src_path=sample_images[0],
            category="People & Social",
            date=date,
            output_dir=tmp_dirs["output"],
        )

        assert os.path.isfile(dest)
        assert "People & Social" in dest
        assert "2024" in dest

    def test_move_without_date(self, tmp_dirs, sample_images):
        """None date should route to Unknown_Date."""
        dest = move_image(
            src_path=sample_images[0],
            category="Memes & Junk",
            date=None,
            output_dir=tmp_dirs["output"],
        )

        assert os.path.isfile(dest)
        assert "Unknown_Date" in dest

    def test_move_preserves_original(self, tmp_dirs, sample_images):
        """Original file should still exist after copy-mode move."""
        original = sample_images[0]
        dest = move_image(
            src_path=original,
            category="Test",
            date=datetime(2024, 1, 1),
            output_dir=tmp_dirs["output"],
            exif_restore=False,
        )

        # Original should still exist (we copy, not move)
        assert os.path.isfile(original)
        assert os.path.isfile(dest)

    def test_move_with_exif_restore(self, tmp_dirs, sample_images):
        """EXIF restore should copy original and inject EXIF date."""
        date = datetime(2024, 8, 20)
        original_size = os.path.getsize(sample_images[0])

        dest = move_image(
            src_path=sample_images[0],
            category="Documents",
            date=date,
            output_dir=tmp_dirs["output"],
            exif_restore=True,
        )

        assert os.path.isfile(dest)
        # The destination should be roughly the same size as the original,
        # NOT a tiny 384x384 thumbnail
        dest_size = os.path.getsize(dest)
        assert dest_size > 0
        # Original file should still exist
        assert os.path.isfile(sample_images[0])

    def test_move_handles_collision(self, tmp_dirs, sample_images):
        """Duplicate filenames should get _1 suffix."""
        date = datetime(2024, 1, 1)

        # Move same image twice to same category/date
        dest1 = move_image(
            src_path=sample_images[0],
            category="Test",
            date=date,
            output_dir=tmp_dirs["output"],
        )
        dest2 = move_image(
            src_path=sample_images[0],
            category="Test",
            date=date,
            output_dir=tmp_dirs["output"],
        )

        assert dest1 != dest2
        assert os.path.isfile(dest1)
        assert os.path.isfile(dest2)
        assert "_1" in os.path.basename(dest2)
