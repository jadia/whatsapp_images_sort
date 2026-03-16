# WhatsApp Image Sorter — Project Specification

> This document captures the complete specification that drives the development of this project.
> All implementation decisions trace back to the requirements defined here.

---

## 1. Configuration & Startup Validation

The application is strictly driven by a `config.json` file.

- **Default Mode:** The `api_mode` should default to `"standard"`.
- **Model Selection:** The active model is defined explicitly via `active_model`, separate from the `pricing` block.
- **Directory Paths:** `source_dir` and `output_dir` are defined in `config.json`. No CLI overrides — config is the single source of truth.
- **API Key:** Stored in `.env` as `GEMINI_API_KEY`. Loaded via `python-dotenv` with `override=True` so that `.env` always wins over existing env vars. Never stored in `config.json`.
- **Sanity Check:** On startup, the script MUST validate the config:
  1. Ensure `active_model` exists as a key inside the `pricing` dictionary.
  2. Ensure `api_mode` is either `"standard"` or `"batch"`.
  3. Ensure `whatsapp_categories` is a populated list.
  4. Ensure `source_dir` exists on disk and is readable.
  5. Ensure `output_dir` exists (or is creatable) and is writable.
  6. Ensure `GEMINI_API_KEY` is set and non-empty.
  If sanity checks fail, exit gracefully with a clear terminal error.

**Reference `config.json`:**

```json
{
  "api_mode": "standard",
  "active_model": "gemini-3-flash-lite",
  "batch_chunk_size": 1000,
  "standard_club_size": 10,
  "upload_threads": 10,
  "source_dir": "/path/to/WhatsApp/Media",
  "output_dir": "/path/to/Sorted",
  "features": {
    "restore_exif_date": true
  },
  "pricing": {
    "gemini-3-flash-lite": { "input_per_1m": 0.075, "output_per_1m": 0.30 },
    "gemini-3-flash": { "input_per_1m": 0.35, "output_per_1m": 1.05 }
  },
  "currency": {
    "symbol": "₹",
    "usd_exchange_rate": 83.50
  },
  "fallback_category": "Uncategorized_Review",
  "global_rules": [
    "Choose exactly ONE category.",
    "Classify by the main purpose of the image.",
    "If the image is ambiguous or low-confidence, use Uncategorized_Review."
  ],
  "whatsapp_categories": [
    {
      "name": "Documents_Important",
      "description": "Document-like or proof-like images"
    },
    {
      "name": "People_Portraits",
      "description": "Real personal photos of people"
    }
  ]
}
```

- **`upload_threads`** (default: `40`, range: `1–100`): Number of parallel threads for uploading/deleting images to/from the Gemini File API. Higher values speed up Phase 1 submissions and Phase 2 cleanup proportionally.

---

## 1b. Retry & Rate Limiting

All API calls (uploads, deletions, `generate_content`, `batches.create`) are wrapped in `retry_with_backoff()` with:
- **Max retries:** 3
- **Back-off:** Exponential (1s → 2s → 4s) with random jitter, capped at 60s
- **Retryable errors:** HTTP 429 (ResourceExhausted), 500, 503, 504, plus `ConnectionError`, `TimeoutError`, `OSError`
- **Non-retryable errors:** `ValueError`, `TypeError`, `KeyboardInterrupt` — these propagate immediately

---

## 2. Database Schema (SQLite)

Use SQLite (`state.db`) for state management to ensure seamless resumes and error recovery.

- **Table 1: `ImageQueue`** → `id` (PK), `file_path` (UNIQUE), `status` (Pending, Processing, Completed, Failed, Missing), `category` (Nullable), `retry_count`, `batch_job_id` (Nullable FK), `inserted_on` (UTC ISO auto), `updated_on` (UTC ISO auto-trigger).
- **Table 2: `BatchJobs`** → `job_id` (PK), `api_job_name`, `status` (Running, Succeeded, Failed), `created_at` (UTC ISO), `updated_on` (UTC ISO auto-trigger).
- **Table 3: `SessionStats`** → `session_id` (PK), `mode`, `images_processed`, `total_tokens`, `cost_local_currency`, `inserted_on` (UTC ISO auto).
- **Table 4: `EstimationStats`** → `model_name` (PK), `total_images_measured`, `total_input_tokens`, `total_output_tokens`, `updated_on` (UTC ISO auto-trigger). Tracks self-calibrating token averages per model.

All tables include audit columns (`inserted_on`, `updated_on`) with `updated_on` managed by SQLite `AFTER UPDATE` triggers.

---

## 3. Core Image Processing & Edge Cases

- **Resizing:** Before uploading to the API (in either mode), use `Pillow` to resize images locally to a maximum of 384×384 pixels to save tokens and bandwidth. Preserve aspect ratio. Do NOT modify the original file.
- **Date Extraction (Regex):** Do NOT hardcode WhatsApp filename prefixes. Use Regex to search for `YYYYMMDD` in the filename. Validate month (01–12) and day (01–31).
- **Date Edge Cases:** If no date is found in the filename, attempt to read the OS file modification time. If that also fails, route the file to `output_dir/Category/Unknown_Date/`.
- **Missing Files Handling:** If `FileNotFoundError` or `PermissionError` occurs when attempting to read an image file block, do not crash. Safely catch the exception, mark that file explicitly as `Missing` in the SQLite datastore (so it is not infinitely retried), and proceed. 
- **Auto-Pruning:** Immediately after scanning the source directory on startup, the application sweeps the `state.db` database and drops any rows referencing files that were manually moved or deleted from disk behind the tool's back.
- **EXIF Restoration:** If `features.restore_exif_date` is `true`, inject the discovered date back into the image's EXIF metadata (using `piexif`) before saving it to the destination folder.

---

## 4. Standard API Mode (Synchronous)

- **Clubbing Logic:** Pull up to `standard_club_size` (e.g., 10) `Pending` images from the DB.
- **Payload Generation:** Interleave text (`Image_1:`, `Image_2:`) with base64 image data in the Gemini `parts` array.
- **Dynamic Prompting:** The text prompt must dynamically state the exact number of images in the batch (to handle the final batch which might have fewer than 10 images). Enforces the configured `fallback_category` as the fallback classification.
- **Processing:** Call the API. Parse the JSON. Move the files. Update the DB to `Completed`.
- **Mismatch Edge Case:** If the AI returns 9 JSON objects for 10 images, move the 9 successful ones. Revert the missing 1 image in the SQLite DB back to `Pending` so it is picked up in the next run.
- **Error Handling:** All API calls wrapped in `try/except`. Errors logged to audit log file and `error.log`.

---

## 5. Batch API Mode (Asynchronous)

This mode requires the script to operate in a "Submit, Exit, and Resume" lifecycle.

- **Phase 1 (Submit):**
  - Loop while there are images in the `Pending` queue:
    - Pull up to `batch_chunk_size` images. Resize them.
    - Upload them to Google's temporary storage using the **Gemini File API** (`client.files.upload()`). Store the returned URIs.
    - Create a `.jsonl` file mapping local file paths to File API URIs.
    - Submit the Job to the Gemini Batch API (`client.batches.create()`).
    - Save the `api_job_name` to the `BatchJobs` table, mark the images as `Processing` in `ImageQueue`.
  - Once the queue is fully submitted across multiple batch jobs, transition to Phase 2.

- **Phase 2 (Resume & Poll):**
  - Upon next launch, the script checks `BatchJobs` for `Running` jobs.
  - It polls the Gemini API. If still running, it notifies the user and exits.
  - If `SUCCEEDED`: Download the output `.jsonl` (ensure to use `client.files.download(file=)` and decode the output from `bytes` to string!). Parse results, move files, and mark as `Completed`.
  - **Crucial Cleanup:** Make API calls to delete the temporary images from the Gemini File API to free up user quota.
  - **Batch Mismatch Edge Case:** Compare input IDs to output IDs. Any skipped images go back to `Pending`. If the whole Batch job `FAILED`, mark all associated images as `Pending`, increment their `retry_count`, and delete the File API uploads.

---

## 6. Uncategorized & Fallbacks

- The prompt to the AI must strictly enforce that if an image does not fit the `whatsapp_categories`, it must return the `fallback_category` (e.g. `"Uncategorized_Review"`). These files are then moved to `output_dir/Uncategorized_Review/`.
- All API calls are wrapped in `try/except` blocks. API errors logged to audit log file and appended to `error.log`.

---

## 7. Logging & Audit

- **Dual logging:** Rich console output (`INFO` level) for the user + detailed file logging (`DEBUG` level) for audit.
- **Progress Bars:** Use `tqdm` for live, single-line progress updates (e.g., during File API uploads or chunk processing) to avoid flooding the terminal with repetitive logs.
- **Per-run log file:** `logs/sorter_YYYYMMDD_HHMMSS.log` — one per run, never overwritten.
- **Error log:** `error.log` (append mode) for quick triage of API errors.
- Logs capture: config loaded, each image processed, API request/response metadata, file moves, errors, cost calculations.

---

## 8. CLI Flags

- `--test-mode` — Processes exactly one small batch and exits. Useful for validation.
- `--dry-run` — Simulates the entire pipeline without calling the API or moving files. Prints useful stats: total images found, already processed count, to-be-processed count, Missing files count, estimated cost, categories list.
- `--prune-queue` — Cleans the database tracking by wiping out the `ImageQueue` table, starting tracking entirely fresh without having to wipe the overall statistical history of API usage.

No `--source-dir` or `--output-dir` flags — directories come exclusively from `config.json`.

---

## 9. Cost Estimation

- **Pre-processing:** Before running, estimate and print expected cost based on image count × average tokens per image × pricing from config. Display in local currency using `currency.symbol` and `currency.usd_exchange_rate`.
- **Post-processing:** Compute actual cost from API `usage_metadata` (input/output token counts).

---

## 10. Documentation & Testing

- `README.md` — Setup, quick start, config reference, CLI flags, FAQ. Includes notice to ensure no duplicate images (suggest `rmlint`).
- `docs/architecture.md` — Mermaid diagrams for system flow, standard mode, batch lifecycle, DB ER diagram.
- `docs/troubleshooting.md` — Common errors and recovery.
- **Tests:** Comprehensive `pytest` suite with mocked API calls covering: config validation, database CRUD, image utils, prompt builder, standard mode flow, batch mode lifecycle, file mover, CLI flags.

---

## 11. Dependencies

- `google-genai` — Google GenAI SDK
- `Pillow` — Image resizing
- `piexif` — EXIF metadata manipulation
- `python-dotenv` — `.env` file loading
- `tqdm` — Progress bar
- `pytest` — Testing (dev dependency)
