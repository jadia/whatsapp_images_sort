"""
============================================================
datatypes.py — Domain Models & Dataclasses
============================================================
Defines the core data structures used throughout the
application, ensuring strict typing and providing
auto-completion for IDEs.
============================================================
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass(frozen=True)
class ImageRow:
    """
    Represents a single row from the ImageQueue table.
    
    LEARNING FOCUS: Dataclasses
    Dataclasses (Python 3.7+) automatically generate boilerplate
    code like __init__ and __repr__. Setting frozen=True makes
    the objects immutable, preventing accidental changes to
    database records in memory.
    """
    id: int
    file_path: str
    status: str
    retry_count: int
    category: Optional[str] = None
    batch_job_id: Optional[int] = None
    inserted_on: Optional[str] = None
    updated_on: Optional[str] = None


@dataclass(frozen=True)
class BatchJobRow:
    """Represents a single row from the BatchJobs table."""
    job_id: int
    api_job_name: str
    status: str
    created_at: str
    updated_on: str


@dataclass(frozen=True)
class SessionStatsRow:
    """Represents a single row from the SessionStats table."""
    session_id: str
    mode: str
    model_name: str
    images_processed: int
    total_tokens: int
    cost_local_currency: float
    inserted_on: str
