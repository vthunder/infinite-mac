#!/usr/bin/env python3
"""Download Mac OS 8.0 chunks from infinitemac.org and upload to R2."""

import json
import os
import subprocess
import sys
import tempfile
import urllib.request

MANIFEST = "src/Data/Mac OS 8.0 HD.dsk.json"
SOURCE_BASE = "https://infinitemac.org/Disk/"
R2_BUCKET = "sandmill-mac-disk"

def main():
    with open(MANIFEST) as f:
        d = json.load(f)
    chunks = [c for c in d["chunks"] if c]
    print(f"{len(chunks)} chunks to upload")

    with tempfile.TemporaryDirectory() as tmpdir:
        for i, chunk in enumerate(chunks):
            filename = f"{chunk}.chunk"
            local_path = os.path.join(tmpdir, filename)

            # Download
            url = SOURCE_BASE + filename
            print(f"[{i+1}/{len(chunks)}] Downloading {filename}...", end=" ", flush=True)
            try:
                req = urllib.request.Request(url, headers={
                    "Referer": "https://infinitemac.org/",
                    "User-Agent": "Mozilla/5.0",
                })
                with urllib.request.urlopen(req) as resp, open(local_path, "wb") as f:
                    f.write(resp.read())
                size = os.path.getsize(local_path)
                print(f"{size//1024}KB", end=" ", flush=True)
            except Exception as e:
                print(f"FAILED: {e}")
                continue

            # Upload to R2
            r2_key = f"{R2_BUCKET}/{filename}"
            result = subprocess.run(
                ["wrangler", "r2", "object", "put", r2_key, "--file", local_path],
                capture_output=True, text=True
            )
            if result.returncode == 0:
                print("✓")
            else:
                print(f"UPLOAD FAILED: {result.stderr}")

    print("Done!")

if __name__ == "__main__":
    main()
