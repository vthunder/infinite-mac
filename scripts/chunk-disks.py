#!/usr/bin/env python3
"""
Simple disk chunker — no Basilisk II required.
Chunks raw .dsk files into BLAKE2b-named .chunk files and updates JSON manifests.
Usage: python3 scripts/chunk-disks.py [disk names...]
       e.g.: python3 scripts/chunk-disks.py "System 7.5 HD" "Infinite HD"
       (omit .dsk suffix)
"""

import hashlib
import json
import os
import sys

CHUNK_SIZE = 256 * 1024  # 262144 bytes
SALT = b"raw"
ZERO_CHUNK = b"\0" * CHUNK_SIZE

ROOT_DIR = os.path.join(os.path.dirname(__file__), "..")
IMAGES_DIR = os.path.join(ROOT_DIR, "Images")
BUILD_DIR = os.path.join(ROOT_DIR, "Images", "build")
DATA_DIR = os.path.join(ROOT_DIR, "src", "Data")


def chunk_disk(disk_name: str) -> None:
    dsk_path = os.path.join(IMAGES_DIR, f"{disk_name}.dsk")
    if not os.path.exists(dsk_path):
        # Try .dsk.zip? Skip for now.
        print(f"ERROR: {dsk_path} not found", file=sys.stderr)
        sys.exit(1)

    os.makedirs(BUILD_DIR, exist_ok=True)

    print(f"Chunking {disk_name}...")
    with open(dsk_path, "rb") as f:
        image_bytes = f.read()

    disk_size = len(image_bytes)
    chunks = []
    chunk_signatures = set()
    zero_count = 0
    new_chunks = 0

    for i in range(0, disk_size, CHUNK_SIZE):
        chunk = image_bytes[i : i + CHUNK_SIZE]
        pct = min(100.0, (i + CHUNK_SIZE) / disk_size * 100)
        print(f"  {pct:.1f}%\r", end="", flush=True)

        if chunk == ZERO_CHUNK or chunk == b"\0" * len(chunk):
            chunks.append("")
            zero_count += 1
            continue

        sig = hashlib.blake2b(chunk, digest_size=16, salt=SALT).hexdigest()
        chunks.append(sig)

        if sig not in chunk_signatures:
            chunk_signatures.add(sig)
            chunk_path = os.path.join(BUILD_DIR, f"{sig}.chunk")
            if not os.path.exists(chunk_path):
                with open(chunk_path, "wb") as cf:
                    cf.write(chunk)
                new_chunks += 1

    print(f"  Done. {len(chunks)} chunks, {zero_count} zero, {new_chunks} new files written.")

    # Update JSON manifest
    manifest_path = os.path.join(DATA_DIR, f"{disk_name}.dsk.json")
    manifest = {
        "name": disk_name,
        "totalSize": disk_size,
        "chunks": chunks,
        "chunkSize": CHUNK_SIZE,
    }
    with open(manifest_path, "w") as mf:
        json.dump(manifest, mf, separators=(",", ":"))
    print(f"  Manifest written: {manifest_path}")


if __name__ == "__main__":
    disks = sys.argv[1:] if len(sys.argv) > 1 else ["System 7.5 HD", "Infinite HD"]
    for disk in disks:
        chunk_disk(disk)
    print("All done.")
