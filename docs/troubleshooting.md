# Troubleshooting Guide

## Startup Errors

### `CONFIG ERROR: GEMINI_API_KEY is not set`

**Cause:** The `.env` file is missing or doesn't contain a valid API key.

**Fix:**
1. Copy the template: `cp .env.example .env`
2. Edit `.env` and add your API key: `GEMINI_API_KEY=your-key-here`
3. Get a key from [Google AI Studio](https://aistudio.google.com/apikey)

---

### `CONFIG ERROR: 'active_model' ('xyz') must be a key in the 'pricing' dictionary`

**Cause:** The model specified in `active_model` doesn't have a matching entry in the `pricing` block.

**Fix:** Either:
- Add the model to the `pricing` section of `config.json`, or
- Change `active_model` to one that already exists in `pricing`

---

### `CONFIG ERROR: 'source_dir' does not exist`

**Cause:** The directory specified in `config.json` doesn't exist or is misspelled.

**Fix:** Verify the path exists: `ls /your/source/dir`. Update `config.json` with the correct absolute path.

---

### `CONFIG ERROR: 'output_dir' is not writable`

**Cause:** The process doesn't have write permissions to the output directory.

**Fix:** `chmod -R u+w /your/output/dir` or choose a directory you own.

---

## Processing Errors

### `API call failed: 429 Resource exhausted`

**Cause:** Gemini API rate limit exceeded.

**Fix:**
- Wait a few minutes and re-run. The script resumes automatically.
- Reduce `standard_club_size` to send smaller batches.
- Switch to Batch mode (`"api_mode": "batch"`) which has separate quotas.

---

### `Failed to parse API response... Expected JSON array`

**Cause:** The model returned a response that wasn't valid JSON.

**Fix:**
- The affected images are automatically reverted to `Pending` and will be retried.
- If persistent, try a different model or reduce `standard_club_size`.
- Check `error.log` for the raw response text.

---

### `MISMATCH: AI returned N results for M images`

**Cause:** The model skipped one or more images in its response (returned fewer results than images sent).

**Impact:** This is handled automatically:
- Matched images are moved and marked `Completed`.
- Unmatched images revert to `Pending` and will be retried in the next batch.
- No manual intervention needed.

---

### Images not being picked up

**Cause:** The image file extension may not be in the supported list.

**Supported extensions:** `.jpg`, `.jpeg`, `.png`, `.gif`, `.bmp`, `.webp`, `.tiff`, `.tif`, `.heic`, `.heif`

**Fix:** If your images use a different extension, you'll need to rename them or update the `IMAGE_EXTENSIONS` set in `main.py`.

---

## Batch Mode Issues

### `Batch job is still running`

**Normal behavior.** Batch jobs can take up to 24 hours (usually much faster). Simply re-run the script later — it will automatically check the status.

---

### `Batch job FAILED`

**Cause:** The Gemini Batch API encountered an error processing the job.

**Impact:** Handled automatically:
- All images in the batch are reverted to `Pending` with `retry_count` incremented.
- File API uploads are cleaned up.
- Re-run the script to resubmit.

**If it keeps failing:**
- Check `error.log` for details.
- Try with a smaller `batch_chunk_size`.
- Verify your API key has batch API access enabled.

---

### Leftover File API uploads (quota warning)

If the script crashes during batch submission and the File API uploads aren't cleaned up:

**Fix:** Use the Google AI Studio console to manually delete uploaded files, or run this cleanup snippet:

```python
from google import genai
client = genai.Client(api_key="your-key")
for f in client.files.list():
    if f.display_name.startswith("Image_"):
        client.files.delete(name=f.name)
        print(f"Deleted: {f.name}")
```

---

## Database Issues

### `state.db` is corrupted

**Cause:** Rare edge case — usually from a disk failure or forced process kill during a write.

**Fix:**
1. Back up the current file: `cp state.db state.db.bak`
2. Try recovery: `sqlite3 state.db ".recover" | sqlite3 state_recovered.db`
3. If that fails, delete `state.db` and re-run. Already-sorted files won't be re-sorted (they've been moved), but tracking history is lost.

---

### How to reset and start fresh

```bash
# Delete state database and logs
rm state.db error.log
rm -rf logs/ batch_metadata/

# Re-run to re-scan
python main.py --dry-run
```

---

## Log Files

| File | Purpose |
|------|---------|
| `logs/sorter_YYYYMMDD_HHMMSS.log` | Detailed per-run audit log (DEBUG level) |
| `error.log` | API and processing errors only (append) |

Review the per-run log for full traceability of what happened during any run.
