#!/usr/bin/env python3
"""Validate the memory layout of an HSS boot image before it is booted.

Built to prevent the class of bug found in QEMU testing: the device tree was
placed at the end of the kernel *Image file*, which is `_edata` - but the
kernel's `.bss` runs from `__bss_start` to `_end`, past the end of the file.
`clear_bss` then zeroed the DTB, Linux saw a truncated tree (no /chosen, no
/memory) and died in paging_init.

Checks performed on the chunk table of a generated boot image:

  1. no two chunk exec ranges overlap;
  2. no chunk (other than the kernel itself) overlaps the kernel's *runtime*
     footprint [entry, _end), which is larger than the Image file;
  3. every chunk lies inside a /memory region of the device tree;
  4. no chunk overlaps a /reserved-memory node marked "no-map";
  5. the ancilliary DTB has room for OpenSBI to expand it in place (~1 KiB)
     and does not run past the end of its memory region;
  6. bootImageLength fits CONFIG_SERVICE_BOOT_MAX_IMAGE_SIZE from the HSS
     .config, and every chunk is 8-byte aligned (DTB: warn if not 2 MiB
     aligned, which is what the kernel's early FDT fixmap mapping prefers).

Exit status 0 = layout OK, 1 = violation (with a readable explanation).
"""

import os
import re
import struct
import subprocess
import sys

# OpenSBI's fdt_open_into() calls grow the blob in place; measured 1056 bytes
# for fdt_cpu_fixup + fdt_fixups + fdt_reserved_memory_nomap_fixup, round up.
OPENSBI_FDT_SLACK = 4096
CHUNK_DESC_SIZE = 40
ANCILLIARY_FLAG = 0x80
BOOT_IMAGE_MAGIC = 0xB007C0DE


def fail(msg):
    print("payload layout ERROR: %s" % msg, file=sys.stderr)
    return 1


def parse_chunks(data):
    magic, version = struct.unpack_from("<II", data, 0)
    if magic != BOOT_IMAGE_MAGIC:
        raise SystemExit("not an HSS boot image (magic %08x)" % magic)
    chunk_table_off, = struct.unpack_from("<Q", data, 0x18)
    boot_image_length, = struct.unpack_from("<Q", data, 0x5C8)
    chunks = []
    off = chunk_table_off
    while True:
        owner, load, exec_addr, size, crc = struct.unpack_from("<QQQQI", data, off)
        if size == 0:
            break
        chunks.append({"owner": owner, "load": load, "exec": exec_addr, "size": size,
                       "ancilliary": bool(owner & ANCILLIARY_FLAG)})
        off += CHUNK_DESC_SIZE
    return chunks, boot_image_length


def kernel_end(ksrc, kernel_addr):
    """Return the kernel's runtime end (_end) as a physical address, or None."""
    sysmap = os.path.join(ksrc, "System.map") if ksrc else None
    if not sysmap or not os.path.isfile(sysmap):
        return None
    for line in open(sysmap):
        parts = line.split()
        if len(parts) >= 3 and parts[2] == "_end":
            return int(parts[0], 16) - 0xFFFFFFFF80000000 + kernel_addr
    return None


def dt_memory_regions(dtb_path):
    """[(base, size)] from the DTB's /memory nodes, plus [(base,size)] no-map."""
    def fdtget(args):
        return subprocess.run(["fdtget"] + args, capture_output=True, text=True).stdout.split()

    nodes = fdtget(["-l", dtb_path, "/"])
    mem, nomap = [], []
    for n in nodes:
        if not n.startswith("memory@"):
            continue
        vals = [int(v) for v in fdtget(["-t", "u", dtb_path, "/" + n, "reg"])]
        for i in range(0, len(vals) - 3, 4):
            mem.append((vals[i] << 32 | vals[i + 1], vals[i + 2] << 32 | vals[i + 3]))
    try:
        rnodes = fdtget(["-l", dtb_path, "/reserved-memory"]) or []
    except Exception:
        rnodes = []
    for n in rnodes:
        path = "/reserved-memory/" + n
        props = subprocess.run(["fdtget", "-p", dtb_path, path],
                               capture_output=True, text=True).stdout
        if "no-map" not in props:
            continue
        vals = [int(v) for v in fdtget(["-t", "u", dtb_path, path, "reg"])]
        for i in range(0, len(vals) - 3, 4):
            nomap.append((vals[i] << 32 | vals[i + 1], vals[i + 2] << 32 | vals[i + 3]))
    return mem, nomap


def in_regions(addr, size, regions):
    for base, rsize in regions:
        if addr >= base and addr + size <= base + rsize:
            return True
    return False


def overlaps(a, asize, b, bsize):
    return a < b + bsize and b < a + asize


def main():
    if len(sys.argv) < 3:
        print("usage: validate-payload.py <boot-image> <kernel-tree-or--> "
              "[--dtb FILE] [--hss-config FILE]", file=sys.stderr)
        return 2
    image, ksrc = sys.argv[1], sys.argv[2]
    dtb = hss_config = None
    for i, a in enumerate(sys.argv):
        if a == "--dtb" and i + 1 < len(sys.argv):
            dtb = sys.argv[i + 1]
        if a == "--hss-config" and i + 1 < len(sys.argv):
            hss_config = sys.argv[i + 1]

    data = open(image, "rb").read()
    chunks, boot_image_length = parse_chunks(data)
    kernel = next((c for c in chunks if not c["ancilliary"] and c["size"] == max(
        x["size"] for x in chunks if not x["ancilliary"])), chunks[0])
    errors = []

    # 1. chunk vs chunk overlap
    for i in range(len(chunks)):
        for j in range(i + 1, len(chunks)):
            a, b = chunks[i], chunks[j]
            if overlaps(a["exec"], a["size"], b["exec"], b["size"]):
                errors.append("chunks overlap: 0x%x+0x%x and 0x%x+0x%x"
                              % (a["exec"], a["size"], b["exec"], b["size"]))

    # 2. kernel runtime footprint ([entry, _end) - larger than the Image file)
    kend = kernel_end(ksrc if ksrc != "-" else None, kernel["exec"])
    if kend:
        for c in chunks:
            if c is kernel:
                continue
            if overlaps(c["exec"], c["size"], kernel["exec"], kend - kernel["exec"]):
                errors.append(
                    "chunk at 0x%x (0x%x bytes) is inside the kernel runtime footprint "
                    "[0x%x, _end=0x%x) - the kernel's .bss is zeroed by clear_bss(), which "
                    "destroys anything placed there (this is the truncated-DTB bug)"
                    % (c["exec"], c["size"], kernel["exec"], kend))
        print("kernel runtime footprint: [0x%x, 0x%x)" % (kernel["exec"], kend))

    # 3./4. DRAM membership, reserved (no-map) regions, DTB expansion slack
    if dtb and os.path.isfile(dtb):
        mem, nomap = dt_memory_regions(dtb)
        if not mem:
            errors.append("device tree %s has no /memory nodes" % dtb)
        for c in chunks:
            room = c["size"] + (OPENSBI_FDT_SLACK if c["ancilliary"] else 0)
            if mem and not in_regions(c["exec"], room, mem):
                errors.append("chunk at 0x%x (0x%x bytes%s) is not fully inside a /memory "
                              "region" % (c["exec"], c["size"],
                                          " + OpenSBI %d byte slack" % OPENSBI_FDT_SLACK
                                          if c["ancilliary"] else ""))
            for base, rsize in nomap:
                if overlaps(c["exec"], c["size"], base, rsize):
                    errors.append("chunk at 0x%x (0x%x bytes) overlaps the no-map reserved "
                                  "region 0x%x+0x%x (HSS/OpenSBI staging)" %
                                  (c["exec"], c["size"], base, rsize))
        if mem:
            print("device tree /memory regions: %s" % ", ".join(
                "0x%x+0x%x" % r for r in mem))

    # 5. alignment
    for c in chunks:
        if c["exec"] % 8:
            errors.append("chunk at 0x%x is not 8-byte aligned" % c["exec"])
        if c["ancilliary"] and c["exec"] % (2 * 1024 * 1024):
            print("note: ancilliary data at 0x%x is not 2 MiB aligned (kernel early FDT "
                  "fixmap maps it in 2 MiB steps)" % c["exec"])

    # 6. HSS image size limit
    if hss_config and os.path.isfile(hss_config):
        m = re.search(r"^CONFIG_SERVICE_BOOT_MAX_IMAGE_SIZE=(\w+)",
                      open(hss_config).read(), re.M)
        if m:
            limit = int(m.group(1), 0)
            print("HSS boot image limit: 0x%x (%d bytes)" % (limit, limit))
            if boot_image_length > limit:
                errors.append("bootImageLength 0x%x exceeds the HSS limit 0x%x configured in "
                              "%s - HSS_Boot_VerifyMagic() will reject the image"
                              % (boot_image_length, limit, hss_config))

    print("boot image: %d chunks, bootImageLength=0x%x" % (len(chunks), boot_image_length))
    for c in chunks:
        print("  chunk owner=0x%02x exec=0x%x size=%-9d ends 0x%x%s"
              % (c["owner"], c["exec"], c["size"], c["exec"] + c["size"],
                 "  (ancilliary)" if c["ancilliary"] else ""))

    if errors:
        for e in errors:
            print("payload layout ERROR: %s" % e, file=sys.stderr)
        return 1
    print("payload layout: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
