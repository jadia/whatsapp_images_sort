"""
============================================================
file_mover.py — Deterministic File Move Logic
============================================================
Moves processed images to their sorted destination:

    output_dir/Category/YYYY-MM-DD/filename.ext

If no date was discovered:

    output_dir/Category/Unknown_Date/filename.ext

Creates intermediate directories as needed. Optionally
writes EXIF-restored bytes instead of copying the original.
============================================================
"""

import logging
import os
import shutil
from datetime import datetime
from typing import Optional

from src.image_utils import restore_exif_date, save_image_without_exif

logger = logging.getLogger("whatsapp_sorter")

# ── Fallback folder name when no date is found ───────────────
UNKNOWN_DATE_FOLDER = "Unknown_Date"


def build_destination_path(
    output_dir: str,
    category: str,
    date: Optional[datetime],
    original_filename: str,
) -> str:
    """
    Build the full destination path for a sorted image.

    Path structure:
        output_dir/Category/YYYY-MM-DD/filename.ext
        output_dir/Category/Unknown_Date/filename.ext

    Args:
        output_dir: Root output directory from config.
        category: The AI-assigned or fallback category.
        date: Extracted date, or None for Unknown_Date.
        original_filename: The original file's basename.

    Returns:
        Absolute path to the destination file.
    """
    # Build the date folder name
    if date is not None:
        date_folder = date.strftime("%Y-%m-%d")
    else:
        date_folder = UNKNOWN_DATE_FOLDER

    # Sanitise category name for filesystem safety
    safe_category = _sanitise_dirname(category)

    dest_path = os.path.join(output_dir, safe_category, date_folder, original_filename)
    return dest_path


def move_image(
    src_path: str,
    category: str,
    date: Optional[datetime],
    output_dir: str,
    exif_restore: bool = False,
    resized_bytes: Optional[bytes] = None,
) -> str:
    """
    Move an image to its sorted destination folder.

    If exif_restore is True and we have both resized_bytes and
    a date, the image is saved with EXIF DateTimeOriginal set.
    Otherwise, the original file is copied to the destination.

    Args:
        src_path: Absolute path to the original source image.
        category: AI-assigned category name.
        date: Extracted date (or None → Unknown_Date).
        output_dir: Root output directory from config.
        exif_restore: Whether to inject EXIF date metadata.
        resized_bytes: Optional resized JPEG bytes (used when
            exif_restore is True).

    Returns:
        Absolute path to the destination file.

    Raises:
        FileNotFoundError: If src_path doesn't exist.
        OSError: If directory creation or file write fails.
    """
    original_filename = os.path.basename(src_path)
    dest_path = build_destination_path(output_dir, category, date, original_filename)

    # Handle filename collision (don't overwrite existing files)
    dest_path = _resolve_collision(dest_path)

    # Create destination directory tree
    dest_dir = os.path.dirname(dest_path)
    os.makedirs(dest_dir, exist_ok=True)

    # Write the file
    if exif_restore and resized_bytes is not None and date is not None:
        # Save with EXIF date restored
        restore_exif_date(resized_bytes, date, dest_path)
        logger.info("Moved (EXIF restored): %s → %s", src_path, dest_path)
    else:
        # Copy original file as-is
        shutil.copy2(src_path, dest_path)
        logger.info("Moved (copy): %s → %s", src_path, dest_path)

    return dest_path


def _sanitise_dirname(name: str) -> str:
    """
    Make a category name safe for use as a directory name.

    Replaces problematic characters while keeping it readable.

    Args:
        name: Raw category name (e.g., "Documents & IDs").

    Returns:
        Filesystem-safe directory name.
    """
    # Replace characters that are problematic on various OSes
    # Keep & and spaces for readability, replace only truly unsafe chars
    unsafe_chars = '<>:"/\\|?*'
    safe = name
    for ch in unsafe_chars:
        safe = safe.replace(ch, "_")
    return safe.strip()


def _resolve_collision(dest_path: str) -> str:
    """
    If dest_path already exists, append a numeric suffix.

    Example: photo.jpg → photo_1.jpg → photo_2.jpg

    Args:
        dest_path: Proposed destination path.

    Returns:
        A path that does not collide with existing files.
    """
    if not os.path.exists(dest_path):
        return dest_path

    base, ext = os.path.splitext(dest_path)
    counter = 1
    while os.path.exists(f"{base}_{counter}{ext}"):
        counter += 1

    new_path = f"{base}_{counter}{ext}"
    logger.debug("Filename collision resolved: %s → %s", dest_path, new_path)
    return new_path
