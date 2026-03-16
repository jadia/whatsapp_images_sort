"""
============================================================
test_image_utils.py — Image Utility Tests
============================================================
Tests resize, date extraction, and EXIF restoration.
============================================================
"""

import os
from datetime import datetime, timezone
from io import BytesIO
from unittest.mock import patch

import pytest
from PIL import Image

from src.utils.image_utils import extract_date, resize_image, restore_exif_date


class TestResizeImage:
    """Tests for image resizing."""

    def test_large_image_is_resized(self, large_sample_image):
        """800×600 image should be resized to fit within 384×384."""
        result = resize_image(large_sample_image, max_dim=384)

        # Result should be valid JPEG bytes
        assert isinstance(result, bytes)
        assert len(result) > 0

        # Open and check dimensions
        img = Image.open(BytesIO(result))
        assert img.size[0] <= 384
        assert img.size[1] <= 384

    def test_aspect_ratio_preserved(self, large_sample_image):
        """Aspect ratio should be preserved after resize."""
        result = resize_image(large_sample_image, max_dim=384)
        img = Image.open(BytesIO(result))

        # Original was 800×600 (4:3 ratio)
        # After fit to 384, should be 384×288 (4:3)
        w, h = img.size
        assert abs(w / h - 800 / 600) < 0.02  # Allow small rounding error

    def test_small_image_not_upscaled(self, sample_images):
        """A 100×100 image should not be enlarged."""
        result = resize_image(sample_images[0], max_dim=384)
        img = Image.open(BytesIO(result))

        # Should stay at 100×100 or smaller (thumbnail won't upscale)
        assert img.size[0] <= 100
        assert img.size[1] <= 100

    def test_output_is_jpeg(self, large_sample_image):
        """Output should always be JPEG format."""
        result = resize_image(large_sample_image)

        # JPEG files start with FF D8 FF
        assert result[:2] == b"\xff\xd8"

    def test_rgba_image_converted_to_rgb(self, tmp_dirs):
        """RGBA (PNG with alpha) should be converted to RGB."""
        path = os.path.join(tmp_dirs["source"], "rgba_test.png")
        img = Image.new("RGBA", (200, 200), color=(255, 0, 0, 128))
        img.save(path, format="PNG")

        result = resize_image(path)
        img_out = Image.open(BytesIO(result))
        assert img_out.mode == "RGB"

    def test_nonexistent_file_raises(self):
        """Resizing a non-existent file should raise."""
        with pytest.raises(FileNotFoundError):
            resize_image("/nonexistent/image.jpg")


class TestExtractDate:
    """Tests for date extraction from filenames and OS metadata."""

    def test_standard_whatsapp_format(self, sample_images):
        """IMG-20240115-WA0001.jpg should extract 2024-01-15."""
        date = extract_date(sample_images[0])
        assert date is not None
        assert date.year == 2024
        assert date.month == 1
        assert date.day == 15

    def test_different_whatsapp_format(self, sample_images):
        """IMG-20240220-WA0002.jpg should extract 2024-02-20."""
        date = extract_date(sample_images[1])
        assert date is not None
        assert date.year == 2024
        assert date.month == 2
        assert date.day == 20

    def test_underscore_format(self, sample_images):
        """photo_20230510_123456.jpg should extract 2023-05-10."""
        date = extract_date(sample_images[2])
        assert date is not None
        assert date.year == 2023
        assert date.month == 5
        assert date.day == 10

    def test_no_date_in_filename_uses_mtime(self, sample_images):
        """random_image.jpg (no date) should fall back to OS mtime."""
        date = extract_date(sample_images[3])
        assert date is not None
        # Should be close to now (file was just created)
        assert date.year >= 2024

    def test_invalid_date_in_filename_uses_mtime(self, sample_images):
        """IMG-20241331-WA0005.jpg (month 13) should fall back to mtime."""
        date = extract_date(sample_images[4])
        assert date is not None
        # Month 13 is invalid, regex won't match → falls back to mtime

    def test_various_filename_patterns(self, tmp_dirs):
        """Test various YYYYMMDD patterns in filenames."""
        test_cases = [
            ("Screenshot_20210815_152030.png", 2021, 8, 15),
            ("WA-20220301-image.jpg", 2022, 3, 1),
            ("photo_20231201_extra.jpg", 2023, 12, 1),  # Date followed by underscore
        ]

        source_dir = tmp_dirs["source"]
        for fname, exp_y, exp_m, exp_d in test_cases:
            path = os.path.join(source_dir, fname)
            img = Image.new("RGB", (10, 10), color="red")
            img.save(path, format="JPEG" if fname.endswith(".jpg") else "PNG")

            date = extract_date(path)
            assert date is not None, f"Failed to extract date from {fname}"
            assert date.year == exp_y, f"{fname}: year {date.year} != {exp_y}"
            assert date.month == exp_m, f"{fname}: month {date.month} != {exp_m}"
            assert date.day == exp_d, f"{fname}: day {date.day} != {exp_d}"


class TestRestoreExifDate:
    """Tests for EXIF date restoration."""

    def test_exif_date_written(self, tmp_dirs, large_sample_image):
        """Should write DateTimeOriginal into EXIF."""
        jpeg_bytes = resize_image(large_sample_image)
        output_path = os.path.join(tmp_dirs["output"], "exif_test.jpg")
        date = datetime(2024, 6, 15, 10, 30, 0)

        restore_exif_date(jpeg_bytes, date, output_path)

        # Verify the file was created
        assert os.path.isfile(output_path)

        # Read back EXIF
        try:
            import piexif
            exif = piexif.load(output_path)
            date_original = exif["Exif"].get(piexif.ExifIFD.DateTimeOriginal, b"")
            assert b"2024:06:15" in date_original
        except ImportError:
            # piexif not installed — just verify file was created
            pass

    def test_exif_without_piexif(self, tmp_dirs, large_sample_image):
        """Without piexif, should still save the file."""
        jpeg_bytes = resize_image(large_sample_image)
        output_path = os.path.join(tmp_dirs["output"], "no_piexif_test.jpg")
        date = datetime(2024, 1, 1)

        # Mock piexif import to fail
        import builtins
        original_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "piexif":
                raise ImportError("Mocked")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=mock_import):
            restore_exif_date(jpeg_bytes, date, output_path)

        assert os.path.isfile(output_path)
        assert os.path.getsize(output_path) > 0
