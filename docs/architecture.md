# Architecture

## System Overview

The WhatsApp Image Sorter is a config-driven CLI utility with two processing modes sharing a common infrastructure layer.

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

        P --> P1[Resize Images]
        P1 --> P2[Upload to File API]
        P2 --> P3[Build JSONL]
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
    end
```

## Standard Mode Sequence

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

    loop Upload each image
        Main->>FileAPI: upload(resized_jpeg)
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
│   ├── batch_mode.py        # Async processing engine
│   ├── file_mover.py        # Sorted directory management
│   ├── cost_tracker.py      # Token & cost accounting
│   └── logger_setup.py      # Logging configuration
├── logs/                    # Per-run audit logs
├── error.log                # API error log (append)
├── tests/                   # pytest test suite
├── docs/                    # Documentation
└── prompt/                  # Project specification
```
