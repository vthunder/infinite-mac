#!/usr/bin/env python3
"""
Build Sandmill HD navigation applets using hand-assembled 68k machine code.

Instead of AppleScript + Standard Additions (which doesn't work reliably on
Mac OS 8.0), each applet directly calls:
  ZeroScrap()          ; _ZeroScrap trap $A9FC
  PutScrap(len,'TEXT',ptr)  ; _PutScrap trap $A9FE
  ExitToShell()        ; _ExitToShell trap $A9F4

No AppleScript, no OSA, no scripting additions required.

The CODE 0 resource from the StopFileSharing template is reused unchanged
(it has the jump table entry pointing to offset 164 in CODE 1). Our CODE 1
has 164 bytes of padding followed by the clipboard code + nav string.
"""

import os
import struct
import subprocess
import sys

APPLETS = [
    {"name": "About Dan",  "nav": "NAV:/"},
    {"name": "Resume",     "nav": "NAV:/resume"},
    {"name": "Projects",   "nav": "NAV:/projects"},
    {"name": "Blog",       "nav": "NAV:/blog"},
    {"name": "Contact",    "nav": "NAV:/contact"},
]

DISK_NAME = "Sandmill HD"
DISK_SIZE_MB = 10
OUT_DISK = "/tmp/sandmill-hd.dsk"
TEMPLATE_BIN = "/tmp/applets/extract/StopFileSharing.bin"
APPLETS_DIR = "/tmp/sandmill-applets-68k"

# Entry point offset in CODE 1.
# CODE 0's jump table entry: offset=0xA4 (164) in CODE 1, segment 1.
# Verified from template binary: bytes 16-23 = 00 A4 3F 3C 00 01 A9 F0
# (MOVE.W #1,-(SP); _LoadSeg). Entry point MUST be at offset 164.
CODE1_ENTRY_OFFSET = 164


# ─── Resource fork helpers (same as build-nav-disk.py) ───────────────────────

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
    res_data_parts = []
    res_data_offsets = {}
    sorted_types = sorted(resources_dict.keys())
    current_offset = 0

    for rtype in sorted_types:
        for entry in resources_dict[rtype]:
            key = (rtype, entry['id'])
            res_data_offsets[key] = current_offset
            blob = struct.pack('>I', len(entry['data'])) + entry['data']
            res_data_parts.append(blob)
            current_offset += len(blob)

    resource_data_blob = b''.join(res_data_parts)
    res_data_len = len(resource_data_blob)

    map_header_size = 28
    type_list_header_size = 2
    type_entry_size = 8
    ref_entry_size = 12
    num_types = len(sorted_types)

    ref_list_start = map_header_size + type_list_header_size + num_types * type_entry_size
    name_list_offset = ref_list_start
    for rtype in sorted_types:
        name_list_offset += len(resources_dict[rtype]) * ref_entry_size

    type_entries = []
    ref_entries = []
    ref_offset_from_type_list = type_list_header_size + num_types * type_entry_size
    current_ref_offset = ref_offset_from_type_list

    for rtype in sorted_types:
        items = resources_dict[rtype]
        num_items = len(items)
        type_entries.append(struct.pack('>4sHH',
            rtype.encode('latin-1'), num_items - 1, current_ref_offset))
        for item in items:
            key = (rtype, item['id'])
            data_off = res_data_offsets[key]
            attrs = item.get('attrs', 0)
            attr_and_data = ((attrs & 0xFF) << 24) | (data_off & 0x00FFFFFF)
            ref_entries.append(struct.pack('>hHI4s',
                item['id'], 0xFFFF, attr_and_data, b'\x00\x00\x00\x00'))
        current_ref_offset += num_items * ref_entry_size

    actual_name_list_offset = map_header_size + (
        type_list_header_size + num_types * type_entry_size +
        sum(len(resources_dict[rt]) * ref_entry_size for rt in sorted_types)
    )

    type_list_blob = (
        struct.pack('>H', num_types - 1)
        + b''.join(type_entries)
        + b''.join(ref_entries)
    )

    map_blob = (
        b'\x00' * 16 + b'\x00' * 4 + b'\x00' * 2 + b'\x00' * 2 +
        struct.pack('>H', map_header_size) +
        struct.pack('>H', actual_name_list_offset) +
        type_list_blob + b'\x00'
    )

    res_data_offset_final = 256
    res_map_offset_final = res_data_offset_final + res_data_len
    padding = res_data_offset_final - 16
    header = (
        struct.pack('>I', res_data_offset_final) +
        struct.pack('>I', res_map_offset_final) +
        struct.pack('>I', res_data_len) +
        struct.pack('>I', len(map_blob))
    )
    result = bytearray(header + b'\x00' * padding + resource_data_blob + map_blob)
    map_start = res_map_offset_final
    struct.pack_into('>I', result, map_start + 0, res_data_offset_final)
    struct.pack_into('>I', result, map_start + 4, res_map_offset_final)
    struct.pack_into('>I', result, map_start + 8, res_data_len)
    struct.pack_into('>I', result, map_start + 12, len(map_blob))
    return bytes(result)


def crc16_macbinary(data):
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
    name_bytes = name.encode('mac_roman')[:63]
    header = bytearray(128)
    header[0] = 0
    header[1] = len(name_bytes)
    header[2:2+len(name_bytes)] = name_bytes
    header[65:69] = file_type.encode('ascii')[:4]
    header[69:73] = creator.encode('ascii')[:4]
    struct.pack_into('>I', header, 83, len(data_data))
    struct.pack_into('>I', header, 87, len(rsrc_data))
    header[122] = 0x81
    header[123] = 0x81
    crc = crc16_macbinary(bytes(header[:124]))
    struct.pack_into('>H', header, 124, crc)

    def pad128(data):
        if not data:
            return b''
        pad = (128 - len(data) % 128) % 128
        return data + b'\x00' * pad

    return bytes(header) + pad128(data_data) + pad128(rsrc_data)


# ─── 68k machine code builder ────────────────────────────────────────────────

def build_code1(nav_string: str) -> bytes:
    """
    Build a CODE 1 resource that sets the Mac clipboard to nav_string and exits.

    Layout:
      [0..163]  : 164 bytes of zeros (padding to reach CODE 0's JT entry point)
      [164..]   : actual 68k code + nav string data

    Machine code (hand-assembled 68k):
      3F 3C 00 00              MOVE.W #0, -(SP)      ; result space for PutScrap
      2F 3C 00 00 00 LL        MOVE.L #len, -(SP)    ; length
      2F 3C 54 45 58 54        MOVE.L #'TEXT', -(SP) ; type
      41 FA 00 06              LEA nav_str(PC), A0   ; src pointer
      2F 08                    MOVE.L A0, -(SP)
      A9 FE                    PutScrap()
      A9 F4                    ExitToShell()
      [nav bytes]              nav string data
    """
    nav_bytes = nav_string.encode('mac_roman')
    nav_len = len(nav_bytes)

    # Build the code bytes
    # Note: PutScrap returns LongInt (4 bytes), so we must push 4 bytes of result
    # space with MOVE.L, not MOVE.W. Using MOVE.W (2 bytes) causes the trap to
    # write 4 bytes into a 2-byte slot, smashing the original stack and freezing
    # Mac OS. ZeroScrap is not needed — PutScrap overwrites the scrap directly.
    code = bytes([
        0x2F, 0x3C, 0x00, 0x00, 0x00, 0x00, # MOVE.L #0, -(SP)  (4-byte result space for PutScrap)
    ])
    code += struct.pack('>2sI', b'\x2F\x3C', nav_len)  # MOVE.L #len, -(SP)
    code += bytes([0x2F, 0x3C, 0x54, 0x45, 0x58, 0x54])  # MOVE.L #'TEXT', -(SP)

    # LEA nav_str(PC), A0
    # After LEA (4 bytes): MOVE.L A0,-(SP) (2) + A9FE (2) + A9F4 (2) = 6 bytes before nav
    code += bytes([0x41, 0xFA, 0x00, 0x06])  # LEA nav_str(PC), A0
    code += bytes([0x2F, 0x08])              # MOVE.L A0, -(SP)
    code += bytes([0xA9, 0xFE])              # PutScrap
    code += bytes([0xA9, 0xF4])              # ExitToShell
    code += nav_bytes

    # Pad to even length
    if len(code) % 2:
        code += b'\x00'

    # Prepend 164 bytes of padding to reach the entry point offset
    return b'\x00' * CODE1_ENTRY_OFFSET + code


def build_applet(name, nav_target, template_resources):
    """Build a 68k Mac applet that sets clipboard directly (no AppleScript)."""
    print(f"  Building 68k code for: {nav_target!r}")

    code1_data = build_code1(nav_target)

    # Reuse all template resources EXCEPT scpt and CODE 1
    new_resources = {}
    for rtype, items in template_resources.items():
        if rtype in ('scpt', 'CODE'):
            continue
        new_resources[rtype] = [dict(item) for item in items]

    # Keep CODE 0 from template (JT entry at offset 164 in CODE 1 — verified)
    for item in template_resources.get('CODE', []):
        if item['id'] == 0:
            new_resources.setdefault('CODE', []).append(dict(item))
            break

    # Add our new CODE 1
    new_resources['CODE'].append({'id': 1, 'data': code1_data, 'attrs': 0})

    rsrc_fork = build_resource_fork(new_resources)
    return build_macbinary(name, 'APPL', 'aplt', rsrc_fork)


def main():
    if not os.path.exists(TEMPLATE_BIN):
        print(f"ERROR: Template not found at {TEMPLATE_BIN}")
        sys.exit(1)

    os.makedirs(APPLETS_DIR, exist_ok=True)

    print("Loading applet template...")
    template_rsrc = parse_macbinary(TEMPLATE_BIN)
    template_resources = parse_resource_fork(template_rsrc)
    print(f"  Template has {sum(len(v) for v in template_resources.values())} resources")

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
        print(f"  CODE 1 size: {len(build_code1(nav))} bytes")

    print(f"\nCreating {DISK_SIZE_MB}MB HFS disk: {DISK_NAME}")
    subprocess.run(['humount', OUT_DISK], capture_output=True)
    if os.path.exists(OUT_DISK):
        os.unlink(OUT_DISK)
    with open(OUT_DISK, 'wb') as f:
        f.write(b'\x00' * (DISK_SIZE_MB * 1024 * 1024))
    result = subprocess.run(['hformat', '-l', DISK_NAME, OUT_DISK],
                            capture_output=True, text=True)
    if result.returncode != 0:
        print(f"hformat failed: {result.stderr}")
        sys.exit(1)

    r = subprocess.run(['hmount', OUT_DISK], capture_output=True, text=True)
    if r.returncode != 0:
        print(f"hmount failed: {r.stderr}")
        sys.exit(1)

    for name, bin_path in applet_files:
        print(f"  Copying {name}...")
        result = subprocess.run(['hcopy', '-m', bin_path, f':{name}'],
                                capture_output=True, text=True)
        if result.returncode != 0:
            print(f"  WARNING: hcopy failed for {name}: {result.stderr}")
        else:
            print(f"  ✓ {name}")

    r = subprocess.run(['hls'], capture_output=True, text=True)
    print(f"\nDisk contents: {r.stdout.strip()}")
    subprocess.run(['humount', OUT_DISK], capture_output=True)
    print(f"\nDone! {OUT_DISK} ({os.path.getsize(OUT_DISK):,} bytes)")


if __name__ == "__main__":
    main()
