# WhatsApp Image Sorter

AI-powered image categorization utility that uses the **Google Gemini API** to sort large volumes of images (e.g., WhatsApp media) into organized folders.

**Supported Python Version:** 3.12+

## Features

- 📁 **Dual-mode processing** — Standard (instant synchronous processing) and Batch (async queue, **50% offline discount**). Designed explicitly for the Google Gemini API.
- 🧠 **AI categorization** — Uses Gemini's natively multimodal vision to classify images into configurable categories.
- 📊 **Cost tracking** — Pre-processing estimates (self-calibrating in SQLite) and post-processing actual costs in local currency, natively accounting for the Batch 50% discount.
- 📈 **Progress bars** — Clean, live single-line `tqdm` progress tracking with dynamic API ETA spinners.
- 🚀 **Performance Architecture** — Blitz through massive backlogs! Native `ThreadPoolExecutor` leverages Google's File API to concurrently upload up to **100 threads at once**.
- 🔄 **Retry with back-off** — Automatic exponential back-off on API rate limits (429) and server errors.
- 💾 **Resume-safe** — SQLite state database ensures seamless restarts with auto-pruning.
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
  "fallback_category": "Uncategorized_Review",
  "whatsapp_categories": [
    {
      "name": "Documents & IDs",
      "description": "Document-like or proof-like images"
    },
    {
      "name": "People & Social",
      "description": "Real personal photos of people"
    }
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
| `upload_threads` | int | Parallel upload and cleanup threads (1–150, default: 100) |
| `source_dir` | string | Directory to scan for images |
| `output_dir` | string | Root directory for sorted output |
| `features.restore_exif_date` | bool | Inject date into EXIF metadata |
| `pricing.<model>` | object | `input_per_1m` and `output_per_1m` in USD |
| `currency.symbol` | string | Local currency symbol |
| `currency.usd_exchange_rate` | float | USD to local currency rate |
| `fallback_category` | string | Used when the AI is unsure (e.g., "Uncategorized_Review") |
| `whatsapp_categories` | list | List of config objects with `name` and `description` |

## CLI Flags

| Flag | Description |
|------|-------------|
| `--test-mode` | Process exactly one batch and exit |
| `--dry-run` | Scan images, show stats/cost estimate, exit without changes |
| `--prune-queue` | Wipe the entire tracking queue inside the SQLite DB |

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

## Why WhatsApp?

This project was built primarily to sort WhatsApp media. WhatsApp automatically receives immense amounts of "junk" daily: forwarded morning quotes, receipts, ID cards, memes, and random screenshots. 

Because WhatsApp aggressively compresses images, the file sizes are exceptionally small and API-friendly to upload. Furthermore, WhatsApp filenames follow a predictable date structure (`IMG-20240115-WA0001.jpg`), making them perfect for auto-sorting into nested `Category/Year` folders once AI evaluates them.

*(Note: This application **only** supports Google Gemini. It utilizes the official Google Generative AI SDK, maximizing cost-effectiveness by leveraging Google's extremely generous async Batch API discounts.)*

## Standard vs. Batch Mode

Choosing the right mode in `config.json` depends entirely on your queue size and patience:

| Feature | Standard Mode | Batch Mode |
|---------|---------------|------------|
| **Best For** | Small runs (< 50 images) | Massive backlogs (1,000+ images) |
| **Speed** | Instant / Synchronous | Asynchronous (Delay of 15+ mins) |
| **Cost** | Full API Price | **50% Discount** |
| **Data Sent** | Inline Base64 Data | Concurrent File API Uploads |

### Standard Mode
- Processes short batches synchronously.
- Instantly returns results. 
- You pay the full API token price.

### Batch Mode (Recommended for Backlogs)
- Cost-efficient (**50% cheaper**).
- **Phase 1 (Submit):** Parses thousands of images and leverages up to **100 concurrent threads** to upload images straight into the Google Cloud File API at lightning speed.
- **Phase 2 (Poll):** Instead of keeping a synchronous connection open, the application steps back and automatically polls Google until their servers have processed your entire backlog.
- Automatic cleanup of File API uploads when completed or cancelled.

## Realistic Cost Analysis (gemini-3.1-flash-lite-preview)

Using Gemini's Batch API makes classifying thousands of images impressively cheap. 

The costs shown below are projections modeled using **real token usage averages** pulled from a live run of 16,638 images (averaging **1,522 input tokens** and **21 output tokens** per 384x384 image footprint):

| Queue Size | Projected Tokens | Standard Cost | Batch Cost (50% Off) |
|------------|------------------|---------------|----------------------|
| **1,000 images** | ~1.5 Million | ~$0.41 USD | **~$0.21 USD (₹17.20 INR)** |
| **5,000 images** | ~7.7 Million | ~$2.06 USD | **~$1.03 USD (₹86.00 INR)** |
| **16,638 images** | ~25.7 Million | ~$6.86 USD | **~$3.43 USD (₹286.41 INR)** |
| **20,000 images** | ~30.9 Million | ~$8.24 USD | **~$4.12 USD (₹344.02 INR)** |

*Note: The SQLite database self-calibrates to your personal usage. If you run `--dry-run`, the cost printed uses your actual historical data.*

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

## 🧠 How it Works under the Hood

WhatsApp Image Sorter is designed to be **bulletproof against interruptions**. You can `Ctrl+C` the script, lose your internet connection, or hit an API rate limit, and the tool will seamlessly resume exactly where it left off.

1. **The SQLite State Machine**: When you start the script, it scans your target directory and logs every single image into a local database (`state.db`) as `Pending`.
2. **Strict ID Mapping**: Every image is assigned a permanent Database ID. When we ask the AI to categorize an image, we tag the image with this ID (e.g., `img_14022`). When the AI responds, we use that ID to map the answer back to your local file, completely eliminating the risk of files getting sorted into the wrong folders.
3. **Local Pre-Processing**: Before anything touches the internet, `Pillow` resizes your images to a maximum of 384x384 pixels in memory. This drastically reduces your API token usage and speeds up upload times by 90%.
4. **Asynchronous Batching**: In Batch Mode, the app uploads your files to Google's temporary storage using 100 parallel threads. It hands Google a "Job", marks your local files as `Processing`, and goes to sleep. When you run the script later, it downloads the results, sorts your files, and cleans up Google's servers to save your quota.

For a deeper dive into the system's execution paths and state management, check out [docs/architecture.md](docs/architecture.md) and [docs/project_flow.md](docs/project_flow.md).
