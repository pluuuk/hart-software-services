#!/usr/bin/env bash
#
# Build a QSPI flash image that boots a Linux kernel Image directly, skipping
# U-Boot entirely.
#
# The HSS already contains OpenSBI.  Its regular boot flow hands the payload to
# OpenSBI, which jumps to it in S-mode with $a1 pointing at the payload's
# ancilliary data.  That is exactly the RISC-V Linux boot protocol, so the
# "payload" can simply be the kernel Image with the device tree as its
# ancilliary data - no U-Boot and no separately built OpenSBI firmware needed.
#
# Inputs (override with environment variables):
#   KERNEL     Linux Image      (default ~/linux/arch/riscv/boot/Image)
#   DTB        device tree blob (default ~/uboot/dts/dt.dtb)
#   BOOTARGS   /chosen bootargs written into the DTB
#   OUT        output flash image (default ./kernel-flash.img)
#   FLASH_SIZE QSPI image size  (default 32M; QEMU's n25q256a needs 32 MiB)
#   DTB_ADDR   where to load the DTB, see below (default 0x88000000)
#
# The device tree is *not* placed straight after the kernel Image file.
# The Image ends at _edata, but the kernel's .bss runs from __bss_start to
# _end, well past the end of the file (for this kernel: Image ends 0x804a4c00,
# .bss is 0x804a5000..0x804dd000).  A DTB appended after the Image therefore
# lands inside the kernel's own .bss and clear_bss() wipes it, which makes
# Linux see a corrupt tree: no /chosen (falls back to CONFIG_CMDLINE) and no
# /memory at all (empty memblock, then an access fault in paging_init).
# Put it far away from the kernel's runtime footprint instead.
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HSS="${HSS:-$HERE/../..}"
GEN="${GEN:-$HSS/tools/hss-payload-generator/hss-payload-generator}"

KERNEL_ADDR="${KERNEL_ADDR:-0x80200000}"
KERNEL="${KERNEL:-$HOME/linux/arch/riscv/boot/Image}"
DTB="${DTB:-$HOME/uboot/dts/dt.dtb}"
BOOTARGS="${BOOTARGS:-console=ttyS1,115200 earlycon=uart8250,mmio,0x20100000 ignore_loglevel root=/dev/mmcblk0 rootwait rw}"
OUT="${OUT:-$PWD/kernel-flash.img}"
FLASH_SIZE="${FLASH_SIZE:-32M}"
DTB_ADDR="${DTB_ADDR:-0x88000000}"

for f in "$KERNEL" "$DTB"; do
    [ -f "$f" ] || { echo "error: missing input $f" >&2; exit 1; }
done
[ -x "$GEN" ] || { echo "error: build hss-payload-generator first" >&2; exit 1; }

# Refuse to load the DTB inside the kernel's runtime footprint.  _end is well
# past the end of the Image file because of .bss, and it moves whenever the
# kernel is rebuilt, so derive it from System.map when we can find one.
KEND_PA=0x0
KSRC="${KERNEL%/arch/riscv/boot/Image}"
SYSMAP="$KSRC/System.map"
if [ -f "$SYSMAP" ]; then
    KEND_VA="$(awk '$3 == "_end" { print $1; exit }' "$SYSMAP")"
    if [ -n "$KEND_VA" ]; then
        KEND_PA=$(( 0x$KEND_VA - 0xffffffff80000000 + KERNEL_ADDR ))
    fi
fi
printf 'kernel: entry %s, runtime end (_end) 0x%x, DTB at %s\n' \
    "$KERNEL_ADDR" "$KEND_PA" "$DTB_ADDR"
if [ $(( DTB_ADDR )) -lt "$KEND_PA" ]; then
    echo "error: DTB_ADDR $DTB_ADDR is inside the kernel image [$KERNEL_ADDR, 0x$(printf %x "$KEND_PA"))" >&2
    exit 1
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

cp "$KERNEL" "$WORK/Image"
cp "$DTB" "$WORK/linux.dtb"

if command -v fdtput >/dev/null 2>&1; then
    fdtput -t s "$WORK/linux.dtb" /chosen bootargs "$BOOTARGS"
else
    echo "warning: fdtput not found; DTB bootargs not set" >&2
fi

# The kernel enters in S-mode via the HSS OpenSBI; the DTB is passed as $a1.
# All four U54s are started with the same entry point: OpenSBI puts harts that
# share an entry point into one domain, so the harts end up as one SMP set and
# Linux brings the secondaries up through SBI HSM.  The chunks (kernel + DTB)
# belong to u54_1 only; the others are launched straight into the same domain
# and park in OpenSBI until Linux starts them.
cat > "$WORK/config.yaml" <<EOF
set-name: 'PolarFire-SoC-HSS::DirectKernel'
hart-entry-points: {u54_1: '$KERNEL_ADDR', u54_2: '$KERNEL_ADDR', u54_3: '$KERNEL_ADDR', u54_4: '$KERNEL_ADDR'}
payloads:
  Image: {exec-addr: '$KERNEL_ADDR', owner-hart: u54_1, secondary-hart: u54_2, secondary-hart: u54_3, secondary-hart: u54_4, priv-mode: prv_s, ancilliary-data: linux.dtb, payload-name: "Linux"}
EOF

( cd "$WORK" && "$GEN" -c config.yaml kernel_boot.bin >/dev/null )
"$GEN" -d "$WORK/kernel_boot.bin" | grep -E "Boot image set|page[0-9]|entry|privMode|ancilliary" | head

python3 - "$WORK/kernel_boot.bin" "$OUT" "$FLASH_SIZE" "$DTB_ADDR" "$OUT.raw" <<'PY'
import struct
import sys

payload = bytearray(open(sys.argv[1], "rb").read())
text = sys.argv[3].strip().lower()
mult = {"k": 1024, "m": 1024 * 1024, "g": 1024 * 1024 * 1024}.get(text[-1], 1)
size = int(text[:-1], 0) * mult if mult > 1 else int(text, 0)
if len(payload) > size:
    sys.exit("payload (%d bytes) does not fit flash image (%d bytes)" % (len(payload), size))

# The generator always puts ancilliary data straight after the main blob
# (blob_handler.c), which lands in the kernel's .bss.  Move it: rewrite the
# exec address in its chunk descriptor.  Safe against the header CRC, which
# covers only sizeof(struct HSS_BootImage) i.e. [0, chunkTableOffset).
magic, version = struct.unpack_from("<II", payload, 0)
assert magic == 0xB007C0DE, "bad boot image magic %08x" % magic
chunk_table_off, = struct.unpack_from("<Q", payload, 0x18)
dtb_addr = int(sys.argv[4], 0)
CHUNK = 40  # owner, loadAddr, execAddr, size, crc32 (each 8-byte aligned)
moved = []
off = chunk_table_off
while True:
    owner, load, exec_addr, csize, crc = struct.unpack_from("<QQQQI", payload, off)
    if csize == 0:
        break
    if owner & 0x80 and exec_addr != dtb_addr:  # BOOT_FLAG_ANCILLIARY_DATA
        struct.pack_into("<Q", payload, off + 0x10, dtb_addr)
        moved.append((exec_addr, dtb_addr, csize))
    off += CHUNK
if not moved:
    sys.exit("error: no ancilliary-data chunk found in the payload")
for was, now, csize in moved:
    print("ancilliary data: exec 0x%x -> 0x%x (%d bytes)" % (was, now, csize))

img = bytearray(b"\xff" * size)          # erased NOR state
img[0:len(payload)] = payload
open(sys.argv[2], "wb").write(img)
print("wrote %s: %d-byte payload at offset 0 of %d bytes" % (sys.argv[2], len(payload), size))

# raw boot image (no flash padding) - this is what goes on eMMC/SD at offset 0
with open(sys.argv[5], "wb") as f:
    f.write(payload)
print("wrote %s: %d-byte raw boot image (bootImageLength=0x%x)"
      % (sys.argv[5], len(payload), struct.unpack_from("<Q", payload, 0x5C8)[0]))
PY

# Refuse to ship a payload whose chunks collide (e.g. a DTB inside the kernel's
# .bss - that silently truncates the device tree in Linux).
HSS_CFG="$HSS/.config"
[ -f "$HSS_CFG" ] || HSS_CFG=""
python3 "$HERE/validate-payload.py" "$OUT.raw" "$KSRC" --dtb "$WORK/linux.dtb" ${HSS_CFG:+--hss-config "$HSS_CFG"}
