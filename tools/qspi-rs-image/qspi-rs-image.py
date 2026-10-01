#!/usr/bin/env python3
#
# MPFS HSS Embedded Software - tools/qspi-rs-image
#
# Build a redundant, striped boot image for the storage-independent HSS
# CONFIG_SERVICE_BOOT_REDUNDANT boot flow (QSPI or MMC).
#
# Layout (3 images x 3 copies = 9 chunks), each chunk sized by a constant
# defined in services/boot/hss_boot_rsimage.h and each preceded by its own
# header:
#
#   +---------+---------+---------+---------+---------+---------+---------+---------+---------+
#   | uboot 0 | kernel0 | dtb  0  | uboot 1 | kernel1 | dtb  1  | uboot 2 | kernel2 | dtb  2  |
#   +---------+---------+---------+---------+---------+---------+---------+---------+---------+
#      ^chunk header + payload (chunk header holds image id, copy index,
#       payload size, payload CRC32, owner hart, exec address and its own
#       header CRC32)
#
# The chunk offsets are precalculated constants (HSS_RS_OFF_*) parsed from the
# HSS header, so the firmware never has to read a global header to find a
# chunk.  There is deliberately no single global header: each chunk is fully
# self describing.  With --auto-layout the sizes/offsets are instead derived
# from the images and can be emitted as a generated C header.
#
# SPDX-License-Identifier: MIT
#
import argparse
import os
import re
import struct
import sys
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
RS_HEADER = os.path.join(HERE, "..", "..", "services", "boot", "hss_boot_rsimage.h")

# on-flash chunk header: 8 x u32 + 2 x u64 = 48 bytes
CHUNK_HEADER_FORMAT = "<8I2Q"
CHUNK_HEADER_SIZE = struct.calcsize(CHUNK_HEADER_FORMAT)

IMAGE_UBOOT = 0
IMAGE_KERNEL = 1
IMAGE_DTB = 2
IMAGE_NAMES = {IMAGE_UBOOT: "uboot", IMAGE_KERNEL: "kernel", IMAGE_DTB: "dtb"}
IMAGE_BY_NAME = {v: k for k, v in IMAGE_NAMES.items()}

HART_BY_NAME = {"u54_1": 1, "u54_2": 2, "u54_3": 3, "u54_4": 4}


def parse_hart(text):
    text = text.strip().lower()
    if text in HART_BY_NAME:
        return HART_BY_NAME[text]
    value = int(text, 0)
    if not (1 <= value <= 4):
        raise argparse.ArgumentTypeError("hart must be u54_1..u54_4")
    return value


def align_up(value, align):
    return (value + align - 1) // align * align


def parse_size(text):
    text = text.strip().lower()
    mult = 1
    if text.endswith("k"):
        mult, text = 1024, text[:-1]
    elif text.endswith("m"):
        mult, text = 1024 * 1024, text[:-1]
    elif text.endswith("g"):
        mult, text = 1024 * 1024 * 1024, text[:-1]
    return int(text, 0) * mult


def crc32(data):
    return zlib.crc32(data) & 0xFFFFFFFF


def read_file(path):
    with open(path, "rb") as f:
        return f.read()


def parse_defines(path):
    """Evaluate the HSS_RS_* #defines from the C header, in order."""
    defines = {}
    pattern = re.compile(r"\s*#\s*define\s+(HSS_RS_\w+)\s+(.+?)\s*$")
    with open(path) as f:
        # join backslash line continuations before parsing
        text = re.sub(r"\\\n", " ", f.read())
    for line in text.splitlines():
        m = pattern.match(line)
        if not m:
            continue
        name, expr = m.group(1), m.group(2)
        expr = expr.split("/*")[0].split("//")[0].strip()
        # strip C integer suffixes (0x10000u -> 0x10000)
        expr = re.sub(r"([0-9a-fA-FxX])([uUlL]+)\b", r"\1", expr)
        try:
            defines[name] = eval(expr, {"__builtins__": {}}, defines)
        except Exception:
            pass
    return defines


def load_layout(header_path):
    d = parse_defines(header_path)
    # A generated layout header (same directory) takes precedence over defaults,
    # matching what services/boot/hss_boot_rsimage.h includes at build time.
    generated = os.path.join(os.path.dirname(os.path.abspath(header_path)),
                             "hss_boot_rsimage_layout.h")
    if os.path.exists(generated):
        d.update(parse_defines(generated))
    required = [
        "HSS_RS_CHUNK_MAGIC", "HSS_RS_VERSION",
        "HSS_RS_NUM_IMAGES", "HSS_RS_NUM_COPIES",
        "HSS_RS_BOOT_BLOCK",
        "HSS_RS_CHUNK_UBOOT_SIZE", "HSS_RS_CHUNK_KERNEL_SIZE", "HSS_RS_CHUNK_DTB_SIZE",
        "HSS_RS_OFF_UBOOT_0", "HSS_RS_OFF_KERNEL_0", "HSS_RS_OFF_DTB_0",
        "HSS_RS_OFF_UBOOT_1", "HSS_RS_OFF_KERNEL_1", "HSS_RS_OFF_DTB_1",
        "HSS_RS_OFF_UBOOT_2", "HSS_RS_OFF_KERNEL_2", "HSS_RS_OFF_DTB_2",
    ]
    missing = [k for k in required if k not in d]
    if missing:
        raise SystemExit("error: %s: missing defines: %s" % (header_path, ", ".join(missing)))

    capacities = {
        IMAGE_UBOOT: d["HSS_RS_CHUNK_UBOOT_SIZE"],
        IMAGE_KERNEL: d["HSS_RS_CHUNK_KERNEL_SIZE"],
        IMAGE_DTB: d["HSS_RS_CHUNK_DTB_SIZE"],
    }
    offsets = {}
    for img, name in ((IMAGE_UBOOT, "UBOOT"), (IMAGE_KERNEL, "KERNEL"), (IMAGE_DTB, "DTB")):
        for copy in range(d["HSS_RS_NUM_COPIES"]):
            offsets[(img, copy)] = d["HSS_RS_OFF_%s_%d" % (name, copy)]

    return d, offsets, capacities


def build_chunk_header(layout, image_id, copy_index, payload, exec_addr, offset, owner_hart):
    magic = layout["HSS_RS_CHUNK_MAGIC"]
    version = layout["HSS_RS_VERSION"]
    blob = struct.pack(
        CHUNK_HEADER_FORMAT,
        magic, version, image_id, copy_index,
        len(payload), crc32(payload), 0, owner_hart,  # headerCrc patched below
        exec_addr, offset)
    header_crc = crc32(blob)
    # headerCrc lives at byte offset 24 (7th u32)
    blob = blob[:24] + struct.pack("<I", header_crc) + blob[28:]
    return blob


def compute_layout(images, boot_block, align, slack, num_copies, magic, version):
    """Derive block-aligned chunk sizes and striped offsets from the images."""
    if align > boot_block:
        raise SystemExit("error: --chunk-align 0x%x exceeds boot block 0x%x"
                         % (align, boot_block))
    capacities = {}
    for image_id, data, _ in images:
        capacities[image_id] = align_up(len(data) + CHUNK_HEADER_SIZE + slack, align)

    stride = sum(capacities[i] for i in (IMAGE_UBOOT, IMAGE_KERNEL, IMAGE_DTB))

    first = {}
    running = boot_block
    for image_id in (IMAGE_UBOOT, IMAGE_KERNEL, IMAGE_DTB):
        first[image_id] = running
        running += capacities[image_id]

    offsets = {}
    for image_id in (IMAGE_UBOOT, IMAGE_KERNEL, IMAGE_DTB):
        for copy in range(num_copies):
            offsets[(image_id, copy)] = first[image_id] + copy * stride

    layout = {
        "HSS_RS_CHUNK_MAGIC": magic,
        "HSS_RS_VERSION": version,
        "HSS_RS_NUM_IMAGES": 3,
        "HSS_RS_NUM_COPIES": num_copies,
        "HSS_RS_BOOT_BLOCK": boot_block,
        "HSS_RS_CHUNK_UBOOT_SIZE": capacities[IMAGE_UBOOT],
        "HSS_RS_CHUNK_KERNEL_SIZE": capacities[IMAGE_KERNEL],
        "HSS_RS_CHUNK_DTB_SIZE": capacities[IMAGE_DTB],
        "HSS_RS_COPY_STRIDE": stride,
    }
    for image_id, name in ((IMAGE_UBOOT, "UBOOT"), (IMAGE_KERNEL, "KERNEL"), (IMAGE_DTB, "DTB")):
        for copy in range(num_copies):
            layout["HSS_RS_OFF_%s_%d" % (name, copy)] = offsets[(image_id, copy)]

    return layout, offsets, capacities


def emit_layout_header(path, layout, capacities):
    """Write the C layout header consumed by services/boot/hss_boot_rsimage.h."""
    copies = layout["HSS_RS_NUM_COPIES"]
    out = []
    out.append("/* Auto-generated by tools/qspi-rs-image --auto-layout --emit-header.")
    out.append(" * Do not edit; regenerate from the images instead. */")
    out.append("#ifndef HSS_BOOT_RSIMAGE_LAYOUT_H")
    out.append("#define HSS_BOOT_RSIMAGE_LAYOUT_H")
    out.append("")
    out.append("#define HSS_RS_BOOT_BLOCK        (0x%xu)" % layout["HSS_RS_BOOT_BLOCK"])
    out.append("#define HSS_RS_CHUNK_UBOOT_SIZE  (0x%xu)" % capacities[IMAGE_UBOOT])
    out.append("#define HSS_RS_CHUNK_KERNEL_SIZE (0x%xu)" % capacities[IMAGE_KERNEL])
    out.append("#define HSS_RS_CHUNK_DTB_SIZE    (0x%xu)" % capacities[IMAGE_DTB])
    out.append("")
    out.append("#define HSS_RS_COPY_STRIDE (0x%xu)" % layout["HSS_RS_COPY_STRIDE"])
    out.append("")
    for image_id, name in ((IMAGE_UBOOT, "UBOOT"), (IMAGE_KERNEL, "KERNEL"), (IMAGE_DTB, "DTB")):
        for copy in range(copies):
            out.append("#define HSS_RS_OFF_%s_%d (0x%xu)"
                       % (name, copy, layout["HSS_RS_OFF_%s_%d" % (name, copy)]))
        out.append("")
    out.append("#endif /* HSS_BOOT_RSIMAGE_LAYOUT_H */")
    with open(path, "w") as f:
        f.write("\n".join(out))
    print("emitted layout header %s" % path)


def self_test():
    """Minimal check that the auto layout is contiguous, aligned and non-overlapping."""
    images = [(IMAGE_UBOOT, bytes(700000), 0), (IMAGE_KERNEL, bytes(2500000), 0),
              (IMAGE_DTB, bytes(23000), 0)]
    layout, offsets, capacities = compute_layout(images, 0x10000, 0x1000, 0x0, 3, 0x52534348, 2)
    stride = layout["HSS_RS_COPY_STRIDE"]
    ordered = [IMAGE_UBOOT, IMAGE_KERNEL, IMAGE_DTB]
    for copy in range(3):
        prev_end = layout["HSS_RS_OFF_UBOOT_%d" % copy]
        for image_id in ordered:
            off = offsets[(image_id, copy)]
            assert off % 0x1000 == 0, "offset not aligned"
            assert off == prev_end, "chunk not contiguous"
            assert capacities[image_id] >= len(bytes(0)) + CHUNK_HEADER_SIZE
            prev_end = off + capacities[image_id]
    assert offsets[(IMAGE_UBOOT, 1)] == offsets[(IMAGE_UBOOT, 0)] + stride
    assert offsets[(IMAGE_DTB, 2)] + capacities[IMAGE_DTB] <= 16 * 1024 * 1024
    print("self-test OK")
    return 0


def main():
    ap = argparse.ArgumentParser(description="Build a redundant striped QSPI image")
    ap.add_argument("--uboot", help="HSS boot image (hss-payload-generator output)")
    ap.add_argument("--kernel", help="Linux kernel Image")
    ap.add_argument("--dtb", help="Linux device tree blob")
    ap.add_argument("--output", help="output flash image")
    ap.add_argument("--header", default=RS_HEADER, help="path to hss_boot_rsimage.h")
    ap.add_argument("--flash-size", default="32M", help="total flash size (default 32M)")
    ap.add_argument("--uboot-load", type=lambda s: int(s, 0), default=0x103FC00000,
                    help="DDR address for the uboot HSS boot image")
    ap.add_argument("--kernel-load", type=lambda s: int(s, 0), default=0x1050000000,
                    help="DDR address to stage the kernel to")
    ap.add_argument("--dtb-load", type=lambda s: int(s, 0), default=0x1070000000,
                    help="DDR address to stage the dtb to")
    ap.add_argument("--owner-hart", type=parse_hart, default=HART_BY_NAME["u54_1"],
                    help="U54 that must have PMP write permission for the staged "
                         "images (default u54_1)")
    ap.add_argument("--erase-value", type=lambda s: int(s, 0), default=0xFF,
                    help="fill byte for unused flash (default 0xFF)")
    ap.add_argument("--auto-layout", action="store_true",
                    help="derive chunk sizes/offsets from the images instead of "
                         "the header constants")
    ap.add_argument("--chunk-align", type=lambda s: int(s, 0), default=0x1000,
                    help="alignment for auto-layout chunk sizes (default 4 KiB)")
    ap.add_argument("--slack", type=lambda s: int(s, 0), default=0,
                    help="extra headroom added to each chunk in auto-layout mode")
    ap.add_argument("--emit-header", metavar="PATH",
                    help="write a generated C layout header (hss_boot_rsimage_layout.h)")
    ap.add_argument("--self-test", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--corrupt", action="append", default=[], metavar="IMAGE:COPY",
                    help="flip a byte in a copy's payload (e.g. --corrupt kernel:0)")
    ap.add_argument("--corrupt-header", action="append", default=[], metavar="IMAGE:COPY",
                    help="flip a byte in a copy's chunk header (e.g. --corrupt-header dtb:0)")
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    for required in ("uboot", "kernel", "dtb", "output"):
        if not getattr(args, required):
            ap.error("--%s is required" % required)

    header_layout, header_offsets, header_capacities = load_layout(args.header)
    flash_size = parse_size(args.flash_size)

    images = [
        (IMAGE_UBOOT,  read_file(args.uboot),  args.uboot_load),
        (IMAGE_KERNEL, read_file(args.kernel), args.kernel_load),
        (IMAGE_DTB,    read_file(args.dtb),    args.dtb_load),
    ]

    if args.auto_layout:
        layout, offsets, capacities = compute_layout(
            images, header_layout["HSS_RS_BOOT_BLOCK"], args.chunk_align, args.slack,
            header_layout["HSS_RS_NUM_COPIES"],
            header_layout["HSS_RS_CHUNK_MAGIC"], header_layout["HSS_RS_VERSION"])
    else:
        layout, offsets, capacities = header_layout, header_offsets, header_capacities

    if args.emit_header:
        emit_layout_header(args.emit_header, layout, capacities)

    # validate every image fits inside its constant chunk capacity
    for image_id, data, _ in images:
        capacity = capacities[image_id] - CHUNK_HEADER_SIZE
        if len(data) == 0:
            ap.error("image %s is empty" % IMAGE_NAMES[image_id])
        if len(data) > capacity:
            ap.error("image %s is 0x%x bytes but chunk capacity is only 0x%x"
                     % (IMAGE_NAMES[image_id], len(data), capacity))

    def parse_corrupt(specs):
        result = {}
        for spec in specs:
            try:
                name, copy = spec.split(":")
                image_id = IMAGE_BY_NAME[name.strip().lower()]
                copy = int(copy, 0)
            except (ValueError, KeyError):
                ap.error("bad corruption value %r (expected IMAGE:COPY)" % spec)
            if copy >= layout["HSS_RS_NUM_COPIES"]:
                ap.error("copy %d out of range" % copy)
            result.setdefault(image_id, set()).add(copy)
        return result

    corrupt_payload = parse_corrupt(args.corrupt)
    corrupt_header = parse_corrupt(args.corrupt_header)

    used = max(offsets[(i, c)] + capacities[i]
               for (i, c) in offsets)
    if used > flash_size:
        ap.error("layout needs 0x%x bytes but flash is only 0x%x bytes"
                 % (used, flash_size))

    print("Redundant striped QSPI image (per-chunk headers, constant offsets)")
    print("  header       : %s" % os.path.relpath(args.header))
    print("  chunk magic  : 0x%08x  version %d"
          % (layout["HSS_RS_CHUNK_MAGIC"], layout["HSS_RS_VERSION"]))
    print("  flash size   : 0x%x (%d bytes)" % (flash_size, flash_size))
    print("  chunk sizes  : uboot 0x%x, kernel 0x%x, dtb 0x%x, header 0x%x"
          % (capacities[IMAGE_UBOOT], capacities[IMAGE_KERNEL],
             capacities[IMAGE_DTB], CHUNK_HEADER_SIZE))
    print("  layout end   : 0x%x" % used)
    for image_id, data, _ in images:
        print("  %-6s size=0x%-8x crc=0x%08x"
              % (IMAGE_NAMES[image_id], len(data), crc32(data)))

    flash = bytearray([args.erase_value]) * flash_size

    print("  chunks:")
    # iterate in flash order: [uboot0 kernel0 dtb0][uboot1 ...]
    data_by_image = {i: d for i, d, _ in images}
    load_by_image = {i: l for i, _, l in images}
    for copy in range(layout["HSS_RS_NUM_COPIES"]):
        for image_id in (IMAGE_UBOOT, IMAGE_KERNEL, IMAGE_DTB):
            offset = offsets[(image_id, copy)]
            payload = bytearray(data_by_image[image_id])

            header = bytearray(build_chunk_header(
                layout, image_id, copy, payload, load_by_image[image_id], offset,
                args.owner_hart))

            notes = []
            if image_id in corrupt_header and copy in corrupt_header[image_id]:
                header[0] ^= 0xFF  # break the chunk magic
                notes.append("HEADER CORRUPTED")
            if image_id in corrupt_payload and copy in corrupt_payload[image_id]:
                payload[0] ^= 0xFF
                notes.append("PAYLOAD CORRUPTED")

            flash[offset:offset + len(header)] = header
            flash[offset + len(header):offset + len(header) + len(payload)] = payload

            print("    %-6s copy %d @ 0x%08x hdr@0x%08x payload@0x%08x  %s"
                  % (IMAGE_NAMES[image_id], copy, offset, offset,
                     offset + CHUNK_HEADER_SIZE,
                     " ".join(notes) if notes else "ok"))

    with open(args.output, "wb") as f:
        f.write(flash)

    print("wrote %s (%d bytes)" % (args.output, len(flash)))


if __name__ == "__main__":
    sys.exit(main())
