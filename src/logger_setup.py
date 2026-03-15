"""
============================================================
logger_setup.py — Dual Logging Configuration
============================================================
Sets up two logging outputs:

1. **Console handler** (INFO level): User-friendly progress messages
   displayed in the terminal during execution.

2. **File handler** (DEBUG level): Detailed audit log capturing
   every operation — config load, image processing, API calls,
   file moves, errors, cost calculations. One file per run,
   stored in logs/sorter_YYYYMMDD_HHMMSS.log.

Additionally, an error-specific append-mode file handler writes
to error.log for quick triage of API and processing failures.

Usage:
    from src.logger_setup import setup_logging
    logger = setup_logging()           # call once at startup
    logger.info("User-visible message")
    logger.debug("Audit-only detail")
    logger.error("Goes to console + file + error.log")
============================================================
"""

import logging
import os
from datetime import datetime, timezone


def setup_logging(
    log_dir: str = "logs",
    error_log_path: str = "error.log",
) -> logging.Logger:
    """
    Configure and return the root application logger.

    Creates:
      - logs/sorter_YYYYMMDD_HHMMSS.log  (DEBUG, one per run)
      - error.log                         (ERROR, append mode)
      - console stream                    (INFO)

    Args:
        log_dir: Directory for per-run log files. Created if missing.
        error_log_path: Path to the persistent error log file.

    Returns:
        Configured logging.Logger instance named 'whatsapp_sorter'.
    """
    # ── Create log directory if it doesn't exist ─────────────
    os.makedirs(log_dir, exist_ok=True)

    # ── Build per-run log filename with UTC timestamp ────────
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    run_log_path = os.path.join(log_dir, f"sorter_{timestamp}.log")

    # ── Get (or create) the named logger ─────────────────────
    logger = logging.getLogger("whatsapp_sorter")
    logger.setLevel(logging.DEBUG)  # Capture everything at root

    # Prevent duplicate handlers if setup_logging is called twice
    if logger.handlers:
        return logger

    # ── Formatter: detailed for files, concise for console ───
    file_formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(funcName)s:%(lineno)d | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    console_formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%H:%M:%S",
    )

    # ── 1) Per-run file handler (DEBUG — captures everything) ─
    run_file_handler = logging.FileHandler(run_log_path, mode="w", encoding="utf-8")
    run_file_handler.setLevel(logging.DEBUG)
    run_file_handler.setFormatter(file_formatter)
    logger.addHandler(run_file_handler)

    # ── 2) Error log handler (ERROR — append mode for triage) ─
    error_file_handler = logging.FileHandler(error_log_path, mode="a", encoding="utf-8")
    error_file_handler.setLevel(logging.ERROR)
    error_file_handler.setFormatter(file_formatter)
    logger.addHandler(error_file_handler)

    # ── 3) Console handler (INFO — user-friendly output) ─────
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(console_formatter)
    logger.addHandler(console_handler)

    logger.debug("Logging initialised — run log: %s", run_log_path)
    return logger
