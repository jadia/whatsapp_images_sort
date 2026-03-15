# Architecture

## System Overview

This diagram illustrates the high-level orchestration of the application. The `main.py` entry point handles environment initialization (config, database) and pre-calculates costs using self-calibrating historical data, before routing execution to either the Standard or Batch processing engine based on the `api_mode`. Both engines rely heavily on the Shared Infrastructure layer to ensure state is cleanly tracked and resuming is seamless.

```mermaid
graph TB
    subgraph CLI["CLI Entry Point (main.py)"]
        A[Parse Args] --> B[Load Config]
        B --> C[Init Database]
        C --> D[Scan & Enqueue]
        D --> E{api_mode?}
    end

    subgraph Standard["Standard Mode"]
        E -->|standard| F[Fetch Pending Batch]
        F --> G[Resize Images]
        G --> H[Build Prompt + Parts]
        H --> I[Call Gemini API]
        I --> J[Parse JSON Response]
        J --> K{Mismatch?}
        K -->|No| L[Move Files]
        K -->|Yes| M[Move Matched\nRevert Unmatched]
        L --> N[Update DB]
        M --> N
        N --> F
    end

    subgraph Batch["Batch Mode"]
        E -->|batch| O{Running Jobs?}
        O -->|No| P[Phase 1: Submit]
        O -->|Yes| Q[Phase 2: Resume]

        P --> P1["Resize + Upload\n(ThreadPoolExecutor)"]
        P1 --> P3[Build JSONL]
        P3 --> P4[Submit Batch Job]
        P4 --> P5[Save to DB & EXIT]

        Q --> Q1[Poll Job Status]
        Q1 --> Q2{Status?}
        Q2 -->|Running| Q3[Notify & EXIT]
        Q2 -->|Succeeded| Q4[Parse Output\nMove Files]
        Q2 -->|Failed| Q5[Revert to Pending]
        Q4 --> Q6[Cleanup File API]
        Q5 --> Q6
    end

    subgraph Shared["Shared Infrastructure"]
        DB[(SQLite\nstate.db)]
        LOG[Logger\nconsole + file]
        CFG[Config\nconfig.json + .env]
        COST[Cost Tracker]
        RETRY[Retry w/ Backoff]
    end
```

## Standard Mode Sequence

Standard mode is synchronous and optimized for immediate results. To minimize API round-trips, it groups images into "clubs" (e.g., 10 images at once) and interleaves the image bytes directly into a single massive multimodal prompt. If the API successfully processes all 10 images, they are moved. If the AI hallucinates and returns fewer results (a mismatch), the missing images are gracefully reverted to `Pending` status in the database to be safely retried in the next batch.

```mermaid
sequenceDiagram
    participant User
    participant Main
    participant DB
    participant ImageUtils
    participant Gemini as Gemini API
    participant FileMover

    User->>Main: python main.py
    Main->>DB: get_pending_batch(10)
    DB-->>Main: [10 images]

    loop For each image
        Main->>ImageUtils: resize_image(path)
        ImageUtils-->>Main: jpeg_bytes
        Main->>ImageUtils: extract_date(path)
        ImageUtils-->>Main: datetime
    end

    Main->>Gemini: generate_content(prompt + 10 images)
    Gemini-->>Main: JSON array [10 results]

    alt All 10 matched
        loop For each result
            Main->>FileMover: move_image(src, category, date)
            Main->>DB: mark_completed(id, category)
        end
    else 9 of 10 matched (mismatch)
        Main->>FileMover: move 9 matched images
        Main->>DB: mark_completed(9 images)
        Main->>DB: revert_to_pending([missing 1])
    end

    Main->>User: Session summary
```

## Batch Mode Lifecycle

Batch mode is fully asynchronous and significantly cheaper, designed for bulk processing. It operates in two lifecycle phases to allow the user to close the terminal while Google processes the data in the background.

**Phase 1** resizes and uploads images in parallel using `ThreadPoolExecutor` (configurable via `upload_threads`). Each upload is wrapped in `retry_with_backoff()` to handle rate limits. If the user presses Ctrl+C, all orphaned files are cleaned up from the File API before exiting. After uploads complete, the JSONL manifest is submitted.
**Phase 2** (triggered on a subsequent run of the script) polls the job status. When successful, it pulls the results, categorizes the files, and executes a parallel cleanup sweep of the File API to prevent exhausting the user's storage quota.

```mermaid
sequenceDiagram
    participant User
    participant Main
    participant DB
    participant FileAPI as Gemini File API
    participant BatchAPI as Gemini Batch API

    Note over User,BatchAPI: Phase 1 — Submit
    User->>Main: python main.py (batch mode)
    Main->>DB: get_pending_batch(1000)
    DB-->>Main: [1000 images]

    loop ThreadPoolExecutor (upload_threads)
        Main->>FileAPI: upload(resized_jpeg) [with retry]
        FileAPI-->>Main: file_uri
    end

    Main->>Main: Build JSONL input
    Main->>FileAPI: upload(input.jsonl)
    Main->>BatchAPI: batches.create(src=jsonl)
    BatchAPI-->>Main: job_name
    Main->>DB: create_batch_job(job_name)
    Main->>DB: mark_processing(1000 images)
    Main->>User: "Job submitted! Run again later."
    Note over Main: SCRIPT EXITS

    Note over User,BatchAPI: Phase 2 — Resume
    User->>Main: python main.py (next run)
    Main->>DB: get_running_batch_jobs()
    DB-->>Main: [job_name]
    Main->>BatchAPI: batches.get(job_name)

    alt Still Running
        BatchAPI-->>Main: RUNNING
        Main->>User: "Still running, try later."
    else Succeeded
        BatchAPI-->>Main: SUCCEEDED + output
        Main->>Main: Parse JSONL output
        Main->>Main: Move files, update DB
        Main->>FileAPI: Delete uploaded files (cleanup)
        Main->>User: Session summary
    else Failed
        BatchAPI-->>Main: FAILED
        Main->>DB: revert_to_pending(1000, retry++)
        Main->>FileAPI: Delete uploaded files (cleanup)
        Main->>User: "Job failed, images re-queued."
    end
```

## Database Schema

The SQLite database (`state.db`) is the source of truth for the application's resilience. It uses WAL journal mode to support safe concurrent reads. 
- `ImageQueue` tracks the atomic state of every single file.
- `BatchJobs` manages the async lifecycle of Gemini API jobs.
- `SessionStats` aggregates historical run data for auditing.
- `EstimationStats` (not pictured) stores cumulative token usage per-model to self-calibrate future pre-run cost estimations.

```mermaid
erDiagram
    ImageQueue {
        int id PK
        text file_path UK
        text status
        text category
        int retry_count
        int batch_job_id FK
        text inserted_on
        text updated_on
    }

    BatchJobs {
        int job_id PK
        text api_job_name
        text status
        text created_at
        text updated_on
    }

    SessionStats {
        text session_id PK
        text mode
        int images_processed
        int total_tokens
        real cost_local_currency
        text inserted_on
    }

    EstimationStats {
        text model_name PK
        int total_images_measured
        int total_input_tokens
        int total_output_tokens
        text updated_on
    }

    BatchJobs ||--o{ ImageQueue : "batch_job_id"
```

## File Organization

```
whatsapp_images_sort/
├── main.py                  # CLI entry point & orchestrator
├── config.json              # User configuration
├── .env                     # API key (not committed)
├── state.db                 # SQLite state (auto-created)
├── src/
│   ├── config_manager.py    # Config loading + validation
│   ├── database.py          # SQLite CRUD operations
│   ├── image_utils.py       # Resize, date, EXIF
│   ├── prompt_builder.py    # Gemini prompt construction
│   ├── standard_mode.py     # Sync processing engine
│   ├── batch_mode.py        # Async processing engine (parallel uploads)
│   ├── file_mover.py        # Sorted directory management
│   ├── cost_tracker.py      # Token & cost accounting
│   ├── retry.py             # Exponential back-off retry utility
│   └── logger_setup.py      # Logging configuration
├── logs/                    # Per-run audit logs
├── error.log                # API error log (append)
├── tests/                   # pytest test suite
├── docs/                    # Documentation
└── prompt/                  # Project specification
```
