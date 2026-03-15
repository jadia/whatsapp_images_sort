# WhatsApp Image Sorter

AI-powered image categorization utility that uses the **Google Gemini API** to sort large volumes of images (e.g., WhatsApp media) into organized folders.

## Features

- 📁 **Dual-mode processing** — Standard (synchronous) and Batch (async, 50% cheaper)
- 🧠 **AI categorization** — Uses Gemini to classify images into configurable categories
- 📊 **Cost tracking** — Pre-processing estimates (self-calibrating in SQLite) and post-processing actual costs in local currency
- 📈 **Progress bars** — Clean, live single-line `tqdm` progress tracking
- 🚀 **Parallel uploads** — `ThreadPoolExecutor` with configurable threads for batch mode uploads
- 🔄 **Retry with back-off** — Automatic exponential back-off on API rate limits (429) and server errors
- 💾 **Resume-safe** — SQLite state database ensures seamless restarts
- 📅 **Smart date extraction** — Regex filename parsing + OS timestamp fallback
- 🏷️ **EXIF restoration** — Optionally inject dates back into image metadata
- 📝 **Extensive logging** — Per-run audit logs + error-specific log file
- 🧪 **Test mode** — Process a single batch for validation
- 🔍 **Dry run** — Scan and estimate without making any changes

## Quick Start

### 1. Clone and install

```bash
git clone git@github.com:jadia/whatsapp_images_sort.git
cd whatsapp_images_sort
pip install -r requirements.txt
```

### 2. Set up your API key

```bash
cp .env.example .env
# Edit .env and add your Gemini API key:
# GEMINI_API_KEY=your-actual-key-here
```

Get your API key from [Google AI Studio](https://aistudio.google.com/apikey).

### 3. Configure

```bash
cp config.json.example config.json
```

Edit `config.json`:

```json
{
  "api_mode": "standard",
  "active_model": "gemini-3-flash-lite",
  "source_dir": "/path/to/your/WhatsApp/Media/Images",
  "output_dir": "/path/to/your/Sorted/Output",
  "whatsapp_categories": [
    "Documents & IDs",
    "Financial & Receipts",
    "People & Social",
    "Memes & Junk",
    "Scenery & Objects"
  ]
}
```

### 4. Run

```bash
# Dry run first (see what would happen)
python main.py --dry-run

# Process one batch to validate
python main.py --test-mode

# Full processing
python main.py
```

## Configuration Reference

| Key | Type | Description |
|-----|------|-------------|
| `api_mode` | `"standard"` \| `"batch"` | Processing mode |
| `active_model` | string | Gemini model name (must be in `pricing`) |
| `batch_chunk_size` | int | Images per batch job (batch mode) |
| `standard_club_size` | int | Images per API call (standard mode) |
| `upload_threads` | int | Parallel upload threads for batch mode (1–50, default: 10) |
| `source_dir` | string | Directory to scan for images |
| `output_dir` | string | Root directory for sorted output |
| `features.restore_exif_date` | bool | Inject date into EXIF metadata |
| `pricing.<model>` | object | `input_per_1m` and `output_per_1m` in USD |
| `currency.symbol` | string | Local currency symbol |
| `currency.usd_exchange_rate` | float | USD to local currency rate |
| `whatsapp_categories` | list | Allowed category names |

## CLI Flags

| Flag | Description |
|------|-------------|
| `--test-mode` | Process exactly one batch and exit |
| `--dry-run` | Scan images, show stats/cost estimate, exit without changes |

## Directory Structure (Output)

```
Sorted/
├── Documents & IDs/
│   ├── 2024/
│   │   ├── IMG-20240115-WA0001.jpg
│   │   └── ...
│   └── Unknown_Date/
├── Financial & Receipts/
├── People & Social/
├── Memes & Junk/
├── Scenery & Objects/
└── Uncategorized_Review/
```

## Modes

### Standard Mode
- Processes images synchronously in batches of `standard_club_size`.
- Sends multiple images per API call for efficiency.
- Handles mismatches: if AI returns fewer results, missing images are re-queued.

### Batch Mode
- Cost-efficient (50% cheaper) but asynchronous.
- **Phase 1:** Pulls max `batch_chunk_size` images and uploads them in parallel (using `upload_threads`). This process loops automatically to submit *all* pending images as multiple independent batch jobs sequentially.
- **Phase 2:** After submission (or if jobs are already running), automatically begins polling the Gemini API with a live, single-line countdown. When jobs succeed, files are moved.
- All API calls have automatic retry with exponential back-off for rate limiting.
- Pressing Ctrl+C during upload will clean up orphaned files from the Gemini File API.
- Automatic cleanup of temporary File API uploads.

## Important Notes

> ⚠️ **Duplicate Images:** Ensure no duplicate images exist in your source directory to avoid unnecessary API costs. Consider using [`rmlint`](https://rmlint.readthedocs.io/) for deduplication before running this tool:
> ```bash
> rmlint /path/to/WhatsApp/Media/Images
> ```

> 💡 **State Recovery:** The SQLite database (`state.db`) tracks all progress. If processing is interrupted, simply re-run the script — it will resume from where it left off.

## Good To Know: Gemini Storage Cleanup

If your batch processing script is killed forcefully or crashes during Phase 1, it may leave behind temporary images in Google's cloud storage. These dangling files silently consume your Gemini File API storage quota (which is typically 20 GB).

To safely inspect and clear out any dangling storage files, run the included manual cleanup utility:

```bash
python scripts/cleanup_gemini_storage.py
```

This will automatically securely authenticate using your `.env` API key, list precisely how many orphaned files exist and their total megabyte size, and prompt you before deleting them all to free up your Google Cloud quota.

## Testing

```bash
pip install pytest pytest-cov
pytest tests/ -v --tb=short
```

## Documentation

- [Architecture](docs/architecture.md) — System design with Mermaid diagrams
- [Troubleshooting](docs/troubleshooting.md) — Common errors and recovery steps
- [Specification](prompt/specification.md) — Original project specification

## License

MIT
