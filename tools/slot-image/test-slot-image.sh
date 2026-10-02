#!/usr/bin/env bash
#
# Runnable check for the redundant slot layout wrapper.
#
#   bash tools/slot-image/test-slot-image.sh
#
# Verifies the spec's region offsets for both the production and QEMU maps and
# that --image places a valid 48-byte copy header (header CRC round-trip) at
# every region.
#
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

python3 -c "open('$work/blob.bin','wb').write(b'A' * 4096)"

echo "== self-test: production =="
python3 "$here/make-slot-image.py" --self-test

echo "== self-test: qemu =="
python3 "$here/make-slot-image.py" --self-test --qemu

echo "== emit header + image (qemu map) =="
python3 "$here/make-slot-image.py" --qemu --slot 0:"$work/blob.bin" \
    --slot 1:"$work/blob.bin" \
    --emit-header "$work/hss_slot_layout.h" --image "$work/image.bin" --self-test

python3 - "$work/image.bin" "$work/hss_slot_layout.h" <<'PY'
import binascii, struct, sys

image = open(sys.argv[1], "rb").read()
build = open(sys.argv[2], "rb").read()
assert b"#define HSS_SLOT_BASE     0x00010000u" in build, build

BASE, C, N, M = 0x10000, 0x500000, 2, 3
STRIDE = N * C
expected = {
    (0, 0): 0x0010000, (1, 0): 0x0510000,
    (0, 1): 0x0A10000, (1, 1): 0x0F10000,
    (0, 2): 0x1410000, (1, 2): 0x1910000,
}

for (slot, copy), off in expected.items():
    assert off == BASE + copy * STRIDE + slot * C
    magic, version, sid, cid, size, pcrc, hcrc, owner, cver, res0, foff = \
        struct.unpack("<10IQ", image[off:off + 48])
    assert magic == 0x534C4F54, hex(magic)
    assert version == 3 and sid == slot and cid == copy and foff == off
    assert size == 4096
    shadow = image[off:off + 48][:24] + b"\x00\x00\x00\x00" + image[off:off + 48][28:]
    assert binascii.crc32(shadow) & 0xFFFFFFFF == hcrc, "header CRC"
    blob = image[off + 48:off + 48 + size]
    assert binascii.crc32(blob) & 0xFFFFFFFF == pcrc, "payload CRC"
    assert blob == b"A" * 4096

print("PASS: all 6 regions valid")
PY
