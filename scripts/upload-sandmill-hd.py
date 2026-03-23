#!/usr/bin/env python3
"""
Upload Sandmill HD chunks to R2 bucket.

Progress: tail /tmp/sandmill-hd-upload.log
Status:   python3 scripts/upload-sandmill-hd.py --status
"""

import json
import os
import subprocess
import sys

MANIFEST = os.path.join(os.path.dirname(__file__), "..", "src", "Data", "Sandmill HD.dsk.json")
BUILD_DIR = os.path.join(os.path.dirname(__file__), "..", "Images", "build")
R2_BUCKET = "sandmill-mac-disk"
LOG_FILE = "/tmp/sandmill-hd-upload.log"
DONE_FILE = "/tmp/sandmill-hd-upload-done.txt"
WRANGLER_CWD = os.path.join(os.path.dirname(__file__), "..")


def load_done():
    if os.path.exists(DONE_FILE):
        with open(DONE_FILE) as f:
            return set(line.strip() for line in f if line.strip())
    return set()


def status():
    with open(MANIFEST) as f:
        d = json.load(f)
    chunks = sorted(set(c for c in d["chunks"] if c))
    done = load_done()
    remaining = len(chunks) - len(done)
    print(f"Total unique chunks : {len(chunks)}")
    print(f"Uploaded (done file): {len(done)}")
    print(f"Remaining           : {remaining}")
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE) as f:
            lines = f.readlines()
        last = [l.rstrip() for l in lines[-5:] if l.strip()]
        print(f"\nLast log entries:")
        for l in last:
            print(f"  {l}")


def main():
    with open(MANIFEST) as f:
        d = json.load(f)

    chunks = sorted(set(c for c in d["chunks"] if c))
    done = load_done()
    remaining = [c for c in chunks if c not in done]

    print(f"Total: {len(chunks)} | Done: {len(done)} | Remaining: {len(remaining)}", flush=True)

    if not remaining:
        print("All chunks already uploaded!")
        return

    with open(LOG_FILE, "a") as log:
        log.write(f"=== Start: {len(remaining)} remaining / {len(chunks)} total ===\n")
        log.flush()

        with open(DONE_FILE, "a") as done_f:
            for i, chunk in enumerate(remaining):
                chunk_file = os.path.join(BUILD_DIR, f"{chunk}.chunk")
                r2_key = f"{R2_BUCKET}/{chunk}.chunk"

                if not os.path.exists(chunk_file):
                    log.write(f"MISSING_LOCAL [{i+1}/{len(remaining)}] {chunk}.chunk\n")
                    log.flush()
                    continue

                result = subprocess.run(
                    ["wrangler", "r2", "object", "put", r2_key, "--file", chunk_file, "--remote"],
                    capture_output=True,
                    text=True,
                    cwd=WRANGLER_CWD,
                )

                uploaded_so_far = len(done) + i + 1
                if result.returncode == 0:
                    done_f.write(f"{chunk}\n")
                    done_f.flush()
                    log.write(f"OK [{uploaded_so_far}/{len(chunks)}] {chunk}.chunk\n")
                    log.flush()
                else:
                    err = result.stderr.strip().replace("\n", " ")
                    log.write(f"FAIL [{i+1}/{len(remaining)}] {chunk}: {err}\n")
                    log.flush()

        log.write(f"=== Done: uploaded {len(remaining)} chunks ===\n")

    print("Upload complete!", flush=True)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--status":
        status()
    else:
        main()
