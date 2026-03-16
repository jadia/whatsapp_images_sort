# System Architecture

## Core Philosophy: The SQLite State Machine
The WhatsApp Image Sorter is fundamentally designed to handle massive volumes of images (e.g., 50,000+ files) over unreliable networks and rate-limited APIs. To achieve this, it never holds the full application state in memory. 

Instead, it relies on a local SQLite database (`state.db`) acting as a **persistent state machine**. The lifecycle of every single image is strictly tracked on disk. If you `Ctrl+C` the script, lose power, or hit a Google API quota limit, the application will cleanly resume exactly where it left off on the next run.

---

## 1. Database Schema (`state.db`)

The SQLite database uses Write-Ahead Logging (`PRAGMA journal_mode=WAL`) to allow concurrent reads and writes, preventing "Database is locked" errors during high-concurrency 100-thread uploads.

### A. The `ImageQueue` Table
This is the source of truth for every image in your configured `source_dir`.

| Column | Type | Description |
| :--- | :--- | :--- |
| `id` | `INTEGER` | **Primary Key**. Used as the unbreakable `img_{id}` label. |
| `file_path` | `TEXT` | **Unique**. Absolute path to the original image on disk. |
| `status` | `TEXT` | `Pending`, `Processing`, `Completed`, `Failed`, or `Missing`. |
| `category` | `TEXT` | The AI-assigned category (e.g., `Documents_Important`). |
| `retry_count`| `INTEGER`| How many times this image has failed. Max is 2. |
| `batch_job_id`|`INTEGER` | Foreign key to `BatchJobs.job_id` if using Batch API. |
| `inserted_on`| `TEXT` | UTC ISO-8601 Timestamp. |
| `updated_on` | `TEXT` | Managed automatically by a SQLite trigger. |

**Sample Data from `ImageQueue`:**
```text
39040 | /home/user/images/WA001.jpg | Completed | Memes_Forwards_Graphics | 0 | 20 | 2026-03-16T17:29:00Z | 2026-03-16T18:16:52Z
39041 | /home/user/images/WA002.jpg | Pending   | NULL                    | 0 | NULL| 2026-03-16T17:29:00Z | 2026-03-16T17:29:00Z
```

### B. Other Tables
- **`BatchJobs`**: Tracks asynchronous Gemini Batch API submissions (`Running`, `Succeeded`, `Failed`). Maps Google's `batches/...` IDs to local `job_id`s.
- **`SessionStats`**: Records telemetry per run (Images processed, Token count, USD/Local currency cost).
- **`EstimationStats`**: Self-calibrating token averager. It keeps a running tally of input/output tokens per model, providing accurate pre-run cost estimates.

---

## 2. Unbreakable ID Mapping

The most critical mechanism in this application is the **1:1 ID Mapping** between your local file and the AI's response. 

Because we send thousands of images to the AI asynchronously, we cannot rely on sequential order (e.g., `Image 1`, `Image 2`). If `Image 2` fails to upload, `Image 3` shifts into its spot, and your `Documents` folder fills up with `Selfies`.

**The Solution:**
We use the immutable `ImageQueue.id` primary key. When building the prompt or the `.jsonl` payload, the script labels the image dynamically as `img_{id}` (e.g., `img_39040`). 
When the Gemini AI returns its categorization JSON, it returns:
`{"image": "img_39040", "category": "Memes_Forwards_Graphics"}`.
The script parses this, directly looks up `id=39040` in the database, and moves the corresponding file. This guarantees 100% accuracy, regardless of network failures, thread races, or API mismatches.

---

## 3. Resiliency & Auto-Pruning

1. **Auto-Pruning:** Upon startup, `main.py` scans the disk. If it finds files in the SQLite database that no longer exist on your drive (because you deleted or moved them manually), it runs a highly efficient `DELETE` query to purge the DB queue and keep it lean.
2. **Missing Files Recovery:** If a file becomes unreadable during processing (e.g., corrupted disk sector), the script marks it as `Missing` instead of infinitely retrying.
3. **Graceful Thread Shutdown:** In Standard mode, synchronous API calls run on a background daemon thread. If you hit `Ctrl+C`, the main thread instantly kills the daemon and exits cleanly. The database state remains uncorrupted, and the interrupted images remain safely marked as `Pending`.

---

## 4. Retry with Exponential Back-off

To handle the inherent volatility of cloud APIs (rate limits, transient network drops, and server-side pressure), the application implements a robust, thread-safe **Exponential Back-off with Jitter** strategy via `src/utils/retry.py`.

### A. Retry Strategy
When an API call (Upload or Inference) fails, the script does not immediately give up. Instead, it follows a deterministic delay pattern:
1. **Base Delay:** 1.0 second (configurable).
2. **Exponential Growth:** The delay doubles with each subsequent attempt (e.g., 1s, 2s, 4s...).
3. **Random Jitter:** A small random float (0.0–1.0s) is added to each delay. This prevents "Thundering Herd" scenarios where 100 concurrent threads all retry at the exact same millisecond, overwhelming the API again.
4. **Max Delay Cap:** 60.0 seconds.

### B. Intelligent Error Classification
The retry mechanism is not "blind." It only retries errors that are considered **Transient**:
- **HTTP 429 (Resource Exhausted):** The system automatically backs off more aggressively when rate limits are hit.
- **HTTP 5xx (Server Errors):** 500, 502, 503, and 504 errors are automatically retried up to 3 times.
- **Network Issues:** Timeouts, DNS resolution failures, and connection resets.

**Immediate Failures (No Retry):**
- **HTTP 400 (Bad Request):** e.g., Invalid API key, malformed prompt, or "Upload already terminated."
- **HTTP 403 (Permission Denied):** e.g., Blocked API key or expired credentials.
- **Pillow Errors:** Unidentified image formats or "Decompression Bomb" safety triggers.

If a file fails all 3 retry attempts, it is moved to the `Unprocessable_Quarantine/` directory and marked as `Failed` in the SQLite database to prevent blocking the rest of the queue.