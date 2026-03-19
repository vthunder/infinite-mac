#!/usr/bin/env python3
"""
Build a custom "Sandmill HD" disk image with AppleScript navigation applets.

Each applet sets the Mac clipboard to "NAV:/path" when double-clicked.
The JS embedding layer intercepts this and navigates the parent page.

Usage:
    python3 scripts/build-nav-disk.py

Outputs:
    /tmp/sandmill-hd.dsk  — HFS disk image ready for chunking + R2 upload

Prerequisites:
    brew install hfsutils  (hmount, hformat, hcopy, hls)
    osacompile             (included with macOS / Xcode CLI tools)
    /tmp/mac-os-8.dsk      (run reassemble-mac8.py first)
"""

import os
import struct
import subprocess
import sys
import tempfile

# ─── Navigation applets to create ────────────────────────────────────────────
APPLETS = [
    {"name": "About Dan",      "nav": "NAV:/"},
    {"name": "Resume",         "nav": "NAV:/resume"},
    {"name": "Projects",       "nav": "NAV:/projects"},
    {"name": "Blog",           "nav": "NAV:/blog"},
    {"name": "Contact",        "nav": "NAV:/contact"},
]

DISK_NAME = "Sandmill HD"
DISK_SIZE_MB = 10  # Small disk, just the applets
OUT_DISK = "/tmp/sandmill-hd.dsk"
TEMPLATE_BIN = "/tmp/applets/extract/StopFileSharing.bin"

# ─── Resource fork helpers ────────────────────────────────────────────────────

def parse_macbinary(path):
    with open(path, 'rb') as f:
        data = f.read()
    header = data[:128]
    data_fork_len = struct.unpack_from('>I', header, 83)[0]
    rsrc_fork_len = struct.unpack_from('>I', header, 87)[0]
    data_start = 128
    data_end = data_start + ((data_fork_len + 127) & ~127)
    rsrc_start = data_end
    return data[rsrc_start:rsrc_start + rsrc_fork_len]


def parse_resource_fork(data):
    res_data_offset = struct.unpack_from('>I', data, 0)[0]
    res_map_offset = struct.unpack_from('>I', data, 4)[0]
    map_data = data[res_map_offset:]
    type_list_offset = struct.unpack_from('>H', map_data, 24)[0]
    num_types = struct.unpack_from('>H', data, res_map_offset + type_list_offset)[0]
    resources = {}
    type_list_start = res_map_offset + type_list_offset + 2
    for i in range(num_types + 1):
        entry_offset = type_list_start + i * 8
        res_type = data[entry_offset:entry_offset+4].decode('latin-1')
        num_resources_m1 = struct.unpack_from('>H', data, entry_offset+4)[0]
        ref_list_offset = struct.unpack_from('>H', data, entry_offset+6)[0]
        resources[res_type] = []
        for j in range(num_resources_m1 + 1):
            ref_offset = res_map_offset + type_list_offset + ref_list_offset + j * 12
            res_id = struct.unpack_from('>h', data, ref_offset)[0]
            attr_and_data = struct.unpack_from('>I', data, ref_offset+4)[0]
            attributes = (attr_and_data >> 24) & 0xFF
            data_offset_field = attr_and_data & 0x00FFFFFF
            actual_data_offset = res_data_offset + data_offset_field
            res_data_len_entry = struct.unpack_from('>I', data, actual_data_offset)[0]
            res_data = data[actual_data_offset+4:actual_data_offset+4+res_data_len_entry]
            resources[res_type].append({'id': res_id, 'data': res_data, 'attrs': attributes})
    return resources


def build_resource_fork(resources_dict):
    """Build a Classic Mac OS resource fork binary from a dict of resources."""
    # resources_dict: { 'TYPE': [{'id': int, 'data': bytes, 'attrs': int}, ...] }

    # Step 1: Collect all resource data blobs and compute offsets
    res_data_parts = []
    res_data_offsets = {}  # (type, id) -> offset from start of resource data area

    sorted_types = sorted(resources_dict.keys())
    current_offset = 0

    for rtype in sorted_types:
        for entry in resources_dict[rtype]:
            key = (rtype, entry['id'])
            res_data_offsets[key] = current_offset
            # Resource data: 4-byte length + data
            blob = struct.pack('>I', len(entry['data'])) + entry['data']
            res_data_parts.append(blob)
            current_offset += len(blob)

    resource_data_blob = b''.join(res_data_parts)
    res_data_len = len(resource_data_blob)

    # Step 2: Build the resource map
    # Type list offset from start of map: 28 bytes (map header)
    map_header_size = 28
    type_list_header_size = 2  # num_types - 1
    type_entry_size = 8
    ref_entry_size = 12

    num_types = len(sorted_types)

    # Calculate reference list offsets
    # Ref lists come after all type entries
    # type_list_offset (from map start) = map_header_size
    # ref lists start at: map_header_size + type_list_header_size + num_types * type_entry_size
    ref_list_start = map_header_size + type_list_header_size + num_types * type_entry_size

    # Build name list (empty — we don't use names)
    name_list_offset = ref_list_start
    for rtype in sorted_types:
        name_list_offset += len(resources_dict[rtype]) * ref_entry_size

    # Build type list entries and ref list entries
    type_entries = []
    ref_entries = []

    ref_offset_from_type_list = type_list_header_size + num_types * type_entry_size

    current_ref_offset = ref_offset_from_type_list
    for rtype in sorted_types:
        items = resources_dict[rtype]
        num_items = len(items)
        type_entries.append(struct.pack('>4sHH',
            rtype.encode('latin-1'),
            num_items - 1,  # count - 1
            current_ref_offset,  # offset from type list start
        ))
        for item in items:
            key = (rtype, item['id'])
            data_off = res_data_offsets[key]
            attrs = item.get('attrs', 0)
            attr_and_data = ((attrs & 0xFF) << 24) | (data_off & 0x00FFFFFF)
            ref_entries.append(struct.pack('>hHI4s',
                item['id'],
                0xFFFF,  # name offset = no name
                attr_and_data,
                b'\x00\x00\x00\x00',  # handle placeholder (reserved)
            ))
        current_ref_offset += num_items * ref_entry_size

    # Assemble the map
    type_list_blob = (
        struct.pack('>H', num_types - 1)  # num types - 1
        + b''.join(type_entries)
        + b''.join(ref_entries)
    )

    # name_list_offset from map start
    actual_name_list_offset = map_header_size + len(type_list_blob)

    map_blob = (
        b'\x00' * 16 +  # copy of header (filled later)
        b'\x00' * 4 +   # next handle to use
        b'\x00' * 2 +   # file reference number
        b'\x00' * 2 +   # resource fork attributes
        struct.pack('>H', map_header_size) +           # offset to type list from map start
        struct.pack('>H', actual_name_list_offset) +   # offset to name list from map start
        type_list_blob
        + b'\x00'  # empty name list (just a null terminator)
    )

    # Step 3: Assemble the full resource fork
    res_data_offset_final = 256  # Standard: resource fork header = 256 bytes preamble
    # Actually, the standard is: header (16 bytes) + resource data + resource map
    # With padding to align. Let's use the simple non-padded form:
    res_data_offset_final = 256
    res_map_offset_final = res_data_offset_final + res_data_len

    # Pad resource data to start at 256
    padding = res_data_offset_final - 16  # header is 16 bytes

    header = (
        struct.pack('>I', res_data_offset_final) +
        struct.pack('>I', res_map_offset_final) +
        struct.pack('>I', res_data_len) +
        struct.pack('>I', len(map_blob))
    )  # 16 bytes

    # The fork = header + padding (to fill 256 bytes) + resource data + resource map
    result = (
        header
        + b'\x00' * padding
        + resource_data_blob
        + map_blob
    )

    # Patch the resource map's copy of the header
    map_start = res_map_offset_final
    result = bytearray(result)
    struct.pack_into('>I', result, map_start + 0, res_data_offset_final)
    struct.pack_into('>I', result, map_start + 4, res_map_offset_final)
    struct.pack_into('>I', result, map_start + 8, res_data_len)
    struct.pack_into('>I', result, map_start + 12, len(map_blob))

    return bytes(result)


def crc16_macbinary(data):
    """CRC16/CCITT as used by MacBinary II."""
    crc = 0
    for byte in data:
        for _ in range(8):
            if (crc ^ (byte << 8)) & 0x8000:
                crc = (crc << 1) ^ 0x1021
            else:
                crc = crc << 1
            byte <<= 1
            crc &= 0xFFFF
    return crc


def build_macbinary(name, file_type, creator, rsrc_data, data_data=b''):
    """Build a MacBinary II file."""
    name_bytes = name.encode('mac_roman')[:63]
    header = bytearray(128)
    header[0] = 0  # must be 0
    header[1] = len(name_bytes)
    header[2:2+len(name_bytes)] = name_bytes
    header[65:69] = file_type.encode('ascii')[:4]
    header[69:73] = creator.encode('ascii')[:4]
    struct.pack_into('>I', header, 83, len(data_data))
    struct.pack_into('>I', header, 87, len(rsrc_data))
    # MacBinary II: version at 122, min version at 123, CRC at 124-125
    header[122] = 0x81  # MacBinary II version
    header[123] = 0x81  # minimum version to read
    crc = crc16_macbinary(bytes(header[:124]))
    struct.pack_into('>H', header, 124, crc)

    def pad128(data):
        if not data:
            return b''
        pad = (128 - len(data) % 128) % 128
        return data + b'\x00' * pad

    return (
        bytes(header)
        + pad128(data_data)
        + pad128(rsrc_data)
    )


def compile_applescript(script_text):
    """Compile AppleScript source to bytecode using osacompile -x."""
    with tempfile.NamedTemporaryFile(suffix='.scpt', delete=False) as f:
        scpt_path = f.name
    src_path = scpt_path + '.src.applescript'
    with open(src_path, 'w') as f:
        f.write(script_text)
    result = subprocess.run(
        ['osacompile', '-x', '-o', scpt_path, src_path],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(f"osacompile failed: {result.stderr}")
    with open(scpt_path, 'rb') as f:
        bytecode = f.read()
    os.unlink(scpt_path)
    os.unlink(src_path)
    return bytecode


def build_applet(name, nav_target, template_resources):
    """Build a Classic Mac OS applet from template CODE resources + new scpt."""
    script = f'tell application "Finder"\nset the clipboard to "{nav_target}"\nend tell'
    print(f"  Compiling: {script!r}")
    bytecode = compile_applescript(script)

    # Start with boilerplate from template (everything except scpt 128)
    new_resources = {}
    for rtype, items in template_resources.items():
        if rtype == 'scpt':
            continue  # We'll replace this
        new_resources[rtype] = [dict(item) for item in items]

    # Add compiled script as scpt 128
    new_resources['scpt'] = [{'id': 128, 'data': bytecode, 'attrs': 0}]

    rsrc_fork = build_resource_fork(new_resources)
    return build_macbinary(name, 'APPL', 'aplt', rsrc_fork)


APPLETS_DIR = "/tmp/sandmill-applets"


def main():
    # Check prerequisites
    if not os.path.exists(TEMPLATE_BIN):
        print(f"ERROR: Template not found at {TEMPLATE_BIN}")
        print("Run the Mac OS 8 disk reassembly and extraction first.")
        sys.exit(1)

    os.makedirs(APPLETS_DIR, exist_ok=True)

    print("Loading applet template...")
    template_rsrc = parse_macbinary(TEMPLATE_BIN)
    template_resources = parse_resource_fork(template_rsrc)
    print(f"  Template has {sum(len(v) for v in template_resources.values())} resources")

    # Build each applet (saved to persistent dir)
    applet_files = []
    for applet_def in APPLETS:
        name = applet_def["name"]
        nav = applet_def["nav"]
        print(f"Building applet: {name} → {nav}")
        macbin = build_applet(name, nav, template_resources)
        out_path = os.path.join(APPLETS_DIR, f"{name}.bin")
        with open(out_path, 'wb') as f:
            f.write(macbin)
        applet_files.append((name, out_path))

    # Create HFS disk image
    print(f"\nCreating {DISK_SIZE_MB}MB HFS disk: {DISK_NAME}")
    # Unmount any existing HFS volumes to avoid conflicts
    subprocess.run(['humount', OUT_DISK], capture_output=True)
    if os.path.exists(OUT_DISK):
        os.unlink(OUT_DISK)
    # Create empty file
    with open(OUT_DISK, 'wb') as f:
        f.write(b'\x00' * (DISK_SIZE_MB * 1024 * 1024))
    # Format as HFS
    result = subprocess.run(
        ['hformat', '-l', DISK_NAME, OUT_DISK],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f"hformat failed: {result.stderr}")
        sys.exit(1)

    # Mount and copy applets
    r = subprocess.run(['hmount', OUT_DISK], capture_output=True, text=True)
    if r.returncode != 0:
        print(f"hmount failed: {r.stderr}")
        sys.exit(1)

    for name, bin_path in applet_files:
        print(f"  Copying {name}...")
        result = subprocess.run(
            ['hcopy', '-m', bin_path, f':{name}'],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            print(f"  WARNING: hcopy failed for {name}: {result.stderr}")
        else:
            print(f"  ✓ {name}")

    # Verify
    r = subprocess.run(['hls'], capture_output=True, text=True)
    print(f"\nDisk contents: {r.stdout.strip()}")
    subprocess.run(['humount', OUT_DISK], capture_output=True)

    print(f"\nDone! Disk image: {OUT_DISK}")
    print(f"Size: {os.path.getsize(OUT_DISK):,} bytes")
    print("\nNext steps:")
    print("  1. Run chunk-and-upload.py to chunk the disk and upload to R2")
    print("  2. Update wrangler.jsonc to include the Sandmill HD manifest")
    print("  3. Update the embed URL to include disk=Sandmill+HD")


if __name__ == "__main__":
    main()
