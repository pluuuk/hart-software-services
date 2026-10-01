#!/usr/bin/env bash
#
# Build a redundant striped boot image (HSS CONFIG_SERVICE_BOOT_REDUNDANT) for
# QSPI flash or MMC/SD, from a mainline U-Boot build, a Linux kernel Image and
# a device tree blob.  The on-storage layout is identical for both backends.
#
# Inputs (override with environment variables):
#   UBOOT_BIN  path to u-boot.bin            (default ~/uboot/u-boot.bin)
#   KERNEL     path to a Linux Image         (default ~/linux/arch/riscv/boot/Image)
#   DTB        path to a device tree blob    (default ~/uboot/dts/dt.dtb, adapted)
#   OUT        output image                  (default ./rs-flash.img)
#   WRITE_DEV  optional block device to dd the image to (e.g. /dev/sdX)
#   OWNER_HART optional U54 recorded in the chunk headers (default u54_1)
#   AUTO_LAYOUT=1  size the chunks from the images and emit the C layout header
#                  under services/boot/ (requires rebuilding + reprogramming HSS)
#
# Extra arguments are passed to qspi-rs-image.py (e.g. --corrupt kernel:0).
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HSS="${HSS:-$HERE/../..}"
TOOL="$HERE/qspi-rs-image.py"
GEN="${GEN:-$HSS/tools/hss-payload-generator/hss-payload-generator}"

UBOOT_BIN="${UBOOT_BIN:-$HOME/uboot/u-boot.bin}"
KERNEL="${KERNEL:-$HOME/linux/arch/riscv/boot/Image}"
DTB="${DTB:-$HOME/uboot/dts/dt.dtb}"
OUT="${OUT:-$PWD/rs-flash.img}"

for f in "$UBOOT_BIN" "$KERNEL" "$DTB"; do
    [ -f "$f" ] || { echo "error: missing input $f" >&2; exit 1; }
done
[ -x "$GEN" ] || { echo "error: build hss-payload-generator first" >&2; exit 1; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# 1. wrap U-Boot into an HSS boot image payload
cp "$UBOOT_BIN" "$WORK/u-boot.bin"
cat > "$WORK/config.yaml" <<EOF
set-name: 'QSPI-U-Boot-Icicle'
hart-entry-points: {u54_1: '0x80200000', u54_2: '0x80200000', u54_3: '0x80200000', u54_4: '0x80200000'}
payloads:
  u-boot.bin: {exec-addr: '0x80200000', owner-hart: u54_1, secondary-hart: u54_2, secondary-hart: u54_3, secondary-hart: u54_4, priv-mode: prv_s, payload-name: "u-boot"}
EOF
( cd "$WORK" && "$GEN" -c config.yaml uboot_boot.bin >/dev/null )

# 2. adapt the U-Boot derived DTB for Linux (console on MMUART1)
cp "$DTB" "$WORK/linux.dtb"
if command -v fdtput >/dev/null 2>&1; then
    if [ -n "${BOOTARGS:-}" ]; then
        fdtput -t s "$WORK/linux.dtb" /chosen bootargs "$BOOTARGS"
    elif ! fdtget "$WORK/linux.dtb" /chosen bootargs >/dev/null 2>&1; then
        # only add a default if the DTB does not already carry bootargs
        fdtput -t s "$WORK/linux.dtb" /chosen bootargs "console=ttyS1,115200 earlycon"
    fi
fi

# 3. build the redundant striped flash image
LAYOUT_ARGS=()
if [ "${AUTO_LAYOUT:-0}" != "0" ]; then
    LAYOUT_ARGS+=(--auto-layout --emit-header "$HSS/services/boot/hss_boot_rsimage_layout.h")
fi
if [ -n "${OWNER_HART:-}" ]; then
    LAYOUT_ARGS+=(--owner-hart "$OWNER_HART")
fi

python3 "$TOOL" \
    --uboot "$WORK/uboot_boot.bin" \
    --kernel "$KERNEL" \
    --dtb "$WORK/linux.dtb" \
    --output "$OUT" "${LAYOUT_ARGS[@]}" "$@"

if [ "${AUTO_LAYOUT:-0}" != "0" ]; then
    echo
    echo "### layout emitted to $HSS/services/boot/hss_boot_rsimage_layout.h"
    echo "### rebuild + reprogram the HSS so the firmware matches this image"
fi

echo
echo "Write it with the HSS YMODEM 'QSPI Write' (option 4) or 'MMC Write' (option 5)"
echo "utility (tools/ymodem-flash), or from a host:"
echo "  dd if=$OUT of=<dev> bs=1M conv=fsync   # dedicated card, offset 0"

if [ -n "${WRITE_DEV:-}" ]; then
    # A missing USB mass-storage node turns /dev/sdX into a regular file and
    # dd then silently writes a scratch file instead of the card.
    [ -b "$WRITE_DEV" ] || { echo "error: $WRITE_DEV is not a block device" >&2; exit 1; }
    echo
    echo "### writing $OUT to $WRITE_DEV"
    dd if="$OUT" of="$WRITE_DEV" bs=1M conv=fsync status=progress
fi
