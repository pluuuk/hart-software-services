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
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HSS="${HSS:-$HERE/../..}"
GEN="${GEN:-$HSS/tools/hss-payload-generator/hss-payload-generator}"

KERNEL="${KERNEL:-$HOME/linux/arch/riscv/boot/Image}"
DTB="${DTB:-$HOME/uboot/dts/dt.dtb}"
BOOTARGS="${BOOTARGS:-console=ttyS1,115200 earlycon=uart8250,mmio,0x20100000 ignore_loglevel root=/dev/mmcblk0 rootwait rw}"
OUT="${OUT:-$PWD/kernel-flash.img}"
FLASH_SIZE="${FLASH_SIZE:-32M}"

for f in "$KERNEL" "$DTB"; do
    [ -f "$f" ] || { echo "error: missing input $f" >&2; exit 1; }
done
[ -x "$GEN" ] || { echo "error: build hss-payload-generator first" >&2; exit 1; }

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
# Only the boot hart is started; Linux brings the others up through SBI HSM.
cat > "$WORK/config.yaml" <<EOF
set-name: 'PolarFire-SoC-HSS::DirectKernel'
hart-entry-points: {u54_1: '0x80200000'}
payloads:
  Image: {exec-addr: '0x80200000', owner-hart: u54_1, priv-mode: prv_s, ancilliary-data: linux.dtb, payload-name: "Linux"}
EOF

( cd "$WORK" && "$GEN" -c config.yaml kernel_boot.bin >/dev/null )
"$GEN" -d "$WORK/kernel_boot.bin" | grep -E "Boot image set|page[0-9]|entry|privMode|ancilliary" | head

python3 - "$WORK/kernel_boot.bin" "$OUT" "$FLASH_SIZE" <<'PY'
import sys
payload = open(sys.argv[1], "rb").read()
text = sys.argv[3].strip().lower()
mult = {"k": 1024, "m": 1024 * 1024, "g": 1024 * 1024 * 1024}.get(text[-1], 1)
size = int(text[:-1], 0) * mult if mult > 1 else int(text, 0)
if len(payload) > size:
    sys.exit("payload (%d bytes) does not fit flash image (%d bytes)" % (len(payload), size))
img = bytearray(b"\xff" * size)          # erased NOR state
img[0:len(payload)] = payload
open(sys.argv[2], "wb").write(img)
print("wrote %s: %d-byte payload at offset 0 of %d bytes" % (sys.argv[2], len(payload), size))
PY