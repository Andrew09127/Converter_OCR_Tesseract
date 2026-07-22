import os
import sys
import time
import requests

url, out = sys.argv[1], sys.argv[2]

for attempt in range(1, 21):
    existing = os.path.getsize(out) if os.path.exists(out) else 0
    headers = {"Range": f"bytes={existing}-"} if existing else {}
    try:
        with requests.get(url, stream=True, timeout=60, headers=headers) as r:
            if r.status_code not in (200, 206):
                print(f"\nHTTP {r.status_code}, retry in 5s")
                time.sleep(5)
                continue
            mode = "ab" if existing else "wb"
            total = existing + int(r.headers.get("content-length", 0))
            done = existing
            with open(out, mode) as f:
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    f.write(chunk)
                    done += len(chunk)
                    print(f"\r{done/1e6:.0f}/{total/1e6:.0f} MB (попытка {attempt})", end="", flush=True)
        break
    except (requests.exceptions.ChunkedEncodingError, requests.exceptions.ConnectionError) as exc:
        print(f"\nОбрыв: {exc} -- докачиваю с {os.path.getsize(out) if os.path.exists(out) else 0} байт")
        time.sleep(3)
else:
    print("\nНе удалось докачать за 20 попыток")
    sys.exit(1)

print("\nDONE", out)
