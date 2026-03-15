"""
============================================================
cleanup_gemini_storage.py — Gemini File API Storage Cleanup
============================================================
A standalone utility to list and delete all files from your
Google Gemini File API storage. Useful for cleaning up
orphaned files after interrupted batch uploads.

Usage:
    python scripts/cleanup_gemini_storage.py
============================================================
"""

import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

# Add the parent directory to the Python path so we can import src modules
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from google import genai
from src.config_manager import load_config
from src.retry import retry_with_backoff
import logging

# Set up simple logging to console
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(message)s")
logger = logging.getLogger(__name__)

# Suppress the verbose HTTP request logging from the Google GenAI SDK
logging.getLogger("httpx").setLevel(logging.WARNING)


def get_size_str(total_bytes: int) -> str:
    """Convert bytes to a human-readable string (KB, MB, GB)."""
    if total_bytes < 1024:
        return f"{total_bytes} B"
    elif total_bytes < 1024 * 1024:
        return f"{total_bytes / 1024:.2f} KB"
    elif total_bytes < 1024 * 1024 * 1024:
        return f"{total_bytes / (1024 * 1024):.2f} MB"
    else:
        return f"{total_bytes / (1024 * 1024 * 1024):.2f} GB"


def run_cleanup():
    logger.info("Loading configuration and API keys...")
    try:
        config = load_config()
    except Exception as e:
        logger.error(f"Failed to load config or missing GEMINI_API_KEY. Ensure your .env is set up correctly.\nError: {e}")
        return

    client = genai.Client(api_key=config.gemini_api_key)

    logger.info("Fetching file list from Gemini Storage...")
    try:
        files_iterator = client.files.list()
        all_files = list(files_iterator)
        total_files = len(all_files)

        if total_files == 0:
            logger.info("Your Gemini File Storage is completely empty! No cleanup needed.")
            return

        total_bytes = sum([int(getattr(f, 'size_bytes', 0) or 0) for f in all_files])
        size_str = get_size_str(total_bytes)

        logger.info(
            f"\n"
            f"╔══════════════════════════════════════════════════╗\n"
            f"║  Gemini Storage Overview                         ║\n"
            f"║  Total files: {total_files:<34} ║\n"
            f"║  Total size: {size_str:<35} ║\n"
            f"╚══════════════════════════════════════════════════╝"
        )

        user_input = input(f"\nDo you want to PERMANENTLY DELETE all {total_files} files? (yes/no): ").strip().lower()

        if user_input != "yes":
            logger.info("Cleanup cancelled. No files were deleted.")
            return

        logger.info("Deleting files with %d parallel threads...", config.upload_threads)
        deleted_count = 0
        lock = threading.Lock()
        from tqdm import tqdm

        pbar = tqdm(total=total_files, desc="Deleting", unit="file")

        def _delete_one(f):
            try:
                retry_with_backoff(
                    fn=lambda: client.files.delete(name=f.name),
                    description=f"Delete {f.name}",
                )
                return True
            except Exception as e:
                logger.error(f"Failed to delete {f.name}: {e}")
                return False

        with ThreadPoolExecutor(max_workers=config.upload_threads) as executor:
            futures = {executor.submit(_delete_one, f): f for f in all_files}
            for future in as_completed(futures):
                if future.result():
                    with lock:
                        deleted_count += 1
                pbar.update(1)

        pbar.close()
        logger.info(f"Cleanup complete! Successfully deleted {deleted_count}/{total_files} files.")

    except Exception as e:
        logger.error(f"An error occurred while interacting with the Gemini API: {e}")

if __name__ == "__main__":
    run_cleanup()
