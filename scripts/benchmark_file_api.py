import os
import sys
import time
import io
from concurrent.futures import ThreadPoolExecutor, as_completed
from google import genai
from google.genai import types
from PIL import Image

# Import dotenv if available
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

API_KEY = os.getenv("GEMINI_API_KEY")
if not API_KEY:
    print("Error: GEMINI_API_KEY not set.")
    sys.exit(1)

client = genai.Client(api_key=API_KEY)

THREAD_COUNTS = [50, 75, 100]
FILES_PER_TEST = 500

def create_dummy_jpeg() -> bytes:
    img = Image.new("RGB", (100, 100), color="blue")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=50)
    return buf.getvalue()

def test_threads(thread_count: int, jpeg_bytes: bytes):
    print(f"\n--- Testing with {thread_count} threads ---")
    
    # 1. UPLOAD
    upload_start = time.time()
    uploaded_uris = []
    upload_errors = 0
    upload_429s = 0

    def upload_task(i):
        # We need to write bytes to a temp file because Files API upload expects a file path
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            tmp.write(jpeg_bytes)
            tmp_path = tmp.name
        
        try:
            result = client.files.upload(
                file=tmp_path,
                config=types.UploadFileConfig(
                    display_name=f"bench_{thread_count}_{i}.jpg",
                    mime_type="image/jpeg"
                )
            )
            return result.name
        except Exception as e:
            if "429" in str(e) or "quota" in str(e).lower():
                return ("429", str(e))
            return ("error", str(e))
        finally:
            os.unlink(tmp_path)

    print(f"Uploading {FILES_PER_TEST} files...")
    with ThreadPoolExecutor(max_workers=thread_count) as executor:
        futures = [executor.submit(upload_task, i) for i in range(FILES_PER_TEST)]
        for fut in as_completed(futures):
            res = fut.result()
            if isinstance(res, tuple):
                upload_errors += 1
                if res[0] == "429":
                    upload_429s += 1
            else:
                uploaded_uris.append(res)
    
    upload_time = time.time() - upload_start
    upload_rate = len(uploaded_uris) / upload_time if upload_time > 0 else 0
    print(f"Upload Complete: {len(uploaded_uris)} successes, {upload_errors} errors ({upload_429s} rate limits) in {upload_time:.2f}s ({upload_rate:.2f} files/s)")

    # 2. DELETE
    delete_start = time.time()
    delete_errors = 0
    delete_429s = 0

    def delete_task(name):
        try:
            client.files.delete(name=name)
            return True
        except Exception as e:
            if "429" in str(e) or "quota" in str(e).lower():
                return ("429", str(e))
            return ("error", str(e))

    if uploaded_uris:
        print(f"Deleting {len(uploaded_uris)} files...")
        with ThreadPoolExecutor(max_workers=thread_count) as executor:
            futures = [executor.submit(delete_task, uri) for uri in uploaded_uris]
            for fut in as_completed(futures):
                res = fut.result()
                if isinstance(res, tuple):
                    delete_errors += 1
                    if res[0] == "429":
                        delete_429s += 1
    
    delete_time = time.time() - delete_start
    delete_rate = len(uploaded_uris) / delete_time if delete_time > 0 else 0
    print(f"Delete Complete: {len(uploaded_uris) - delete_errors} successes, {delete_errors} errors ({delete_429s} rate limits) in {delete_time:.2f}s ({delete_rate:.2f} files/s)")

    return {
        "threads": thread_count,
        "upload_rate": upload_rate,
        "upload_errors": upload_errors,
        "upload_429s": upload_429s,
        "delete_rate": delete_rate,
        "delete_errors": delete_errors,
        "delete_429s": delete_429s,
    }

def main():
    print("=" * 60)
    print("Gemini File API Thread Benchmark")
    print("=" * 60)
    
    jpeg_bytes = create_dummy_jpeg()
    results = []

    for tc in THREAD_COUNTS:
        res = test_threads(tc, jpeg_bytes)
        results.append(res)
        print("Sleeping for 8 seconds to let rate limits cool down...")
        time.sleep(8)

    print("\n" + "=" * 60)
    print("BENCHMARK RESULTS")
    print("=" * 60)
    print(f"{'Threads':<10} | {'Upload Rate':<15} | {'Upload Err':<12} | {'Delete Rate':<15} | {'Delete Err':<12}")
    print("-" * 75)
    best_threads = 5
    max_rate = 0
    
    for r in results:
        t = r['threads']
        ur = f"{r['upload_rate']:.2f}/s"
        ue = f"{r['upload_errors']} ({r['upload_429s']})"
        dr = f"{r['delete_rate']:.2f}/s"
        de = f"{r['delete_errors']} ({r['delete_429s']})"
        print(f"{t:<10} | {ur:<15} | {ue:<12} | {dr:<15} | {de:<12}")
        
        # Determine sweet spot: max upload rate with 0 rate limit errors
        if r['upload_429s'] == 0 and r['delete_429s'] == 0:
            if r['upload_rate'] > max_rate:
                max_rate = r['upload_rate']
                best_threads = t
    
    print("-" * 75)
    print(f"\nRECOMMENDATION:")
    if max_rate > 0:
        print(f"The sweet spot appears to be **{best_threads} threads**. This achieved the highest")
        print("throughput without triggering any HTTP 429 Rate Limit errors.")
    else:
        print("All thread counts triggered rate limits. We recommend using a low thread count (5-10)")
        print("along with robust exponential backoff retry logic.")

if __name__ == '__main__':
    main()
