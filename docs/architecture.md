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
        O -->|No| P[Phase 1: Submit All Pending]
        O -->|Yes| Q[Phase 2: Resume]

        P --> P0[Loop chunks of `batch_chunk_size`]
        P0 --> P1["Resize + Upload\n(ThreadPoolExecutor)"]
        P1 --> P3[Build JSONL]
        P3 --> P4[Submit Batch Job]
        P4 --> P5[Save to DB]
        P5 --> P0
        P0 -->|Queue Empty| Q

        Q --> Q1[Poll Job Status]
        Q1 --> Q2{Status?}
        Q2 -->|Running| Q3[Wait 60s & Re-poll]
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

Standard mode is synchronous and optimized for immediate results. To minimize API round-trips, it groups images into "clubs" (e.g., 250 images at once) and interleaves the image bytes directly into a single massive multimodal prompt. 

During the actual API call, it encapsulates the sync request inside a daemonized `threading.Thread`. This allows the script to draw an indeterminate `tqdm` UI spinner while waiting, and ensures `Ctrl+C` immediately abandons the request. If the AI hallucinates and returns fewer results (a mismatch), the missing images are gracefully reverted to `Pending` status in the database to be safely retried in the next batch.

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

Batch mode is fully asynchronous and significantly cheaper, designed for bulk processing. It automatically applies a 50% discount to all cost estimators. It operates in two lifecycle phases to allow the user to close the terminal while Google processes the data in the background.

**Phase 1** is executed *second* (only if no jobs are already running). It pulls chunks of images (up to `batch_chunk_size` at a time) from the pending queue and resizes/uploads them in parallel using a ThreadPoolExecutor. Each upload is wrapped in `retry_with_backoff()` to handle rate limits. The chunk is packaged into a JSONL manifest and submitted to the Batch API. This loop repeats until the *entire* pending queue has been successfully dispatched.
**Phase 2** is executed *first* on script launch. The script checks database job statuses. While jobs are running, it refuses to submit new batches, displaying a live countdown. When successful, it pulls the results, categorizes the files, and executes a parallel cleanup sweep of the File API to prevent exhausting the user's storage quota. If the user presses Ctrl+C at any point, all orphaned uploads are cleaned up from the File API before exiting.

```mermaid
sequenceDiagram
    participant User
    participant Main
    participant DB
    participant FileAPI as Gemini File API
    participant BatchAPI as Gemini Batch API

    Note over User,BatchAPI: Phase 1 — Submit
    User->>Main: python main.py (batch mode)
    
    loop Until Pending Queue is Empty
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
        Main->>User: "Job submitted!"
    end

    Note over User,BatchAPI: Phase 2 — Resume & Poll
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

## File Tracking & State Management

A core design principle of this project is that the **source directory is treated as strictly read-only**. 

When a batch completes successfully, the application does *not* actually "move" or delete the original files. Instead, `src/file_mover.py` **copies** the files to the output directory. This is why you will still see all original images in your source folder, while the destination folder only contains the processed ones. This non-destructive approach guarantees zero data loss if something goes wrong.

**How does the script know what has been processed?**
It does not rely on comparing the source and destination folders. Instead, it tracks the exact state of every single file using the SQLite Database (`state.db`).
1. **Scanning**: On startup, `main.py` scans the source directory. Next, it silently **auto-prunes** the database, deleting any previously stored table rows referring to images that no longer physically exist on disk (meaning the user manually deleted them while the tool was offline).
2. **Enqueuing**: It checks the DB. If an existing image path isn't in the DB, it inserts it with a `Pending` status.
3. **Processing**: When standard mode or batch mode successfully categorize an image and copy it to the destination, that specific row in the DB is updated to `Completed`. If the file was corrupted or unreadable, it is silently marked as `Missing`.
4. **Resuming**: The next time you run the script, `main.py` queries the database for files that are *still* `Pending`. It completely ignores files marked as `Completed` or `Missing`, which prevents duplicate processing and saves API costs.

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
        text status "Pending | Processing | Completed | Failed | Missing"
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
        text model_name
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
