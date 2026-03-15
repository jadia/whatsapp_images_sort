"""
============================================================
image_utils.py — Image Processing Utilities
============================================================
Handles three concerns:

1. **Resizing** — Shrink images to max 384×384 for API upload,
   preserving aspect ratio. Returns JPEG bytes without
   modifying the original file on disk.

2. **Date extraction** — Two-pass strategy:
   a) Regex scan filename for YYYYMMDD pattern (validated).
   b) Fallback: OS file modification time.
   c) If both fail: returns None → mapped to Unknown_Date/.

3. **EXIF restoration** — Inject discovered date into the
   image's EXIF DateTimeOriginal field using piexif before
   saving to the destination folder.
============================================================
"""

import io
import logging
import os
import re
from datetime import datetime, timezone
from typing import Optional

from PIL import Image

logger = logging.getLogger("whatsapp_sorter")

# ── Constants ────────────────────────────────────────────────
MAX_DIMENSION = 384
JPEG_QUALITY = 85

# Regex: match YYYYMMDD anywhere in a string.
# Captures year (19xx or 20xx), month (01-12), day (01-31).
_DATE_PATTERN = re.compile(
    r"(?<!\d)"           # Not preceded by another digit
    r"((?:19|20)\d{2})"  # Year: 1900-2099
    r"(0[1-9]|1[0-2])"  # Month: 01-12
    r"(0[1-9]|[12]\d|3[01])"  # Day: 01-31
    r"(?!\d)"            # Not followed by another digit
)


def resize_image(file_path: str, max_dim: int = MAX_DIMENSION) -> bytes:
    """
    Resize an image to fit within max_dim × max_dim pixels.

    Preserves aspect ratio. Converts to RGB JPEG. Does NOT
    modify the original file — returns JPEG bytes in memory.

    Args:
        file_path: Absolute path to the source image.
        max_dim: Maximum width or height in pixels.

    Returns:
        JPEG-encoded bytes of the resized image.

    Raises:
        FileNotFoundError: If file_path does not exist.
        PIL.UnidentifiedImageError: If the file is not a valid image.
    """
    logger.debug("Resizing image: %s (max_dim=%d)", file_path, max_dim)

    with Image.open(file_path) as img:
        # Convert to RGB (handles RGBA, palette, etc.)
        if img.mode != "RGB":
            img = img.convert("RGB")

        # Only resize if the image is larger than max_dim
        original_size = img.size
        img.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)

        # Encode to JPEG bytes
        buffer = io.BytesIO()
        img.save(buffer, format="JPEG", quality=JPEG_QUALITY)
        jpeg_bytes = buffer.getvalue()

    logger.debug(
        "Resized %s: %s → %dx%d (%d bytes JPEG)",
        file_path,
        original_size,
        img.size[0],
        img.size[1],
        len(jpeg_bytes),
    )
    return jpeg_bytes


def extract_date(file_path: str) -> Optional[datetime]:
    """
    Extract a date from the image file.

    Strategy (in order):
      1. Regex search for YYYYMMDD in the filename.
      2. Fallback: OS file modification time.
      3. If both fail: return None (caller maps to Unknown_Date).

    Args:
        file_path: Absolute path to the image file.

    Returns:
        datetime object if a date was found, None otherwise.
    """
    filename = os.path.basename(file_path)

    # ── Pass 1: Regex scan filename for YYYYMMDD ─────────────
    match = _DATE_PATTERN.search(filename)
    if match:
        year, month, day = int(match.group(1)), int(match.group(2)), int(match.group(3))
        try:
            extracted = datetime(year, month, day)
            logger.debug("Date from filename regex: %s → %s", filename, extracted.date())
            return extracted
        except ValueError:
            # Invalid date combo (e.g., Feb 30) — fall through
            logger.debug("Regex matched but invalid date: %04d%02d%02d", year, month, day)

    # ── Pass 2: OS file modification time ────────────────────
    try:
        mtime = os.path.getmtime(file_path)
        extracted = datetime.fromtimestamp(mtime, tz=timezone.utc)
        logger.debug("Date from OS mtime: %s → %s", filename, extracted.date())
        return extracted
    except (OSError, ValueError) as exc:
        logger.warning("Cannot read mtime for %s: %s", file_path, exc)

    # ── Pass 3: Give up ──────────────────────────────────────
    logger.info("No date found for: %s → will use Unknown_Date", filename)
    return None


def restore_exif_date(
    image_bytes: bytes,
    date: datetime,
    output_path: str,
) -> None:
    """
    Save image bytes with EXIF DateTimeOriginal set to `date`.

    Uses piexif to inject the date into the EXIF Exif IFD.
    Writes the result to output_path.

    Args:
        image_bytes: JPEG-encoded image data.
        date: The date to write into EXIF.
        output_path: Destination file path.
    """
    try:
        import piexif
    except ImportError:
        # piexif not installed — save without EXIF modification
        logger.warning("piexif not installed — saving without EXIF restoration")
        with open(output_path, "wb") as fh:
            fh.write(image_bytes)
        return

    # Format date as EXIF expects: "YYYY:MM:DD HH:MM:SS"
    exif_date_str = date.strftime("%Y:%m:%d %H:%M:%S")

    try:
        # Try to load existing EXIF from the image bytes
        exif_dict = piexif.load(image_bytes)
    except Exception:
        # No valid EXIF — create empty structure
        exif_dict = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}}

    # Set DateTimeOriginal and DateTimeDigitized
    exif_dict["Exif"][piexif.ExifIFD.DateTimeOriginal] = exif_date_str.encode("utf-8")
    exif_dict["Exif"][piexif.ExifIFD.DateTimeDigitized] = exif_date_str.encode("utf-8")
    # Also set the 0th IFD DateTime
    exif_dict["0th"][piexif.ImageIFD.DateTime] = exif_date_str.encode("utf-8")

    # Dump EXIF bytes
    exif_bytes = piexif.dump(exif_dict)

    # Write the image with updated EXIF
    with Image.open(io.BytesIO(image_bytes)) as img:
        img.save(output_path, format="JPEG", quality=JPEG_QUALITY, exif=exif_bytes)

    logger.debug("EXIF date restored → %s (%s)", output_path, exif_date_str)


def save_image_without_exif(image_bytes: bytes, output_path: str) -> None:
    """
    Save image bytes to disk without EXIF modification.

    Args:
        image_bytes: JPEG-encoded image data.
        output_path: Destination file path.
    """
    with open(output_path, "wb") as fh:
        fh.write(image_bytes)
    logger.debug("Saved image (no EXIF) → %s", output_path)
