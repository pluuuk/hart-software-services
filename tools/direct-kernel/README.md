# Direct kernel boot (skip U-Boot)

Boot a Linux `Image` directly from the HSS, with no U-Boot stage.

## Why it needs no OpenSBI build

The HSS already contains OpenSBI (see the OpenSBI banner on the U54 console).
Its normal flow hands the payload to OpenSBI, which jumps to it in S-mode with
`$a0 = hartid`, `$a1` = the payload's **ancilliary data**. That is exactly the
RISC-V Linux boot protocol. So the "payload" can be the kernel `Image` itself,
with the device tree as its ancilliary data:

```
payloads:
  Image: {exec-addr: '0x80200000', owner-hart: u54_1, priv-mode: prv_s,
          ancilliary-data: linux.dtb, payload-name: "Linux"}
```

All four U54s are given the *same* entry point, so OpenSBI puts them in one
domain (`Domain1 HARTs : 1*,2*,3*,4*`) and Linux brings the secondaries up
through SBI HSM. The chunks themselves belong to `u54_1` only.

## Where the device tree goes (important)

`hss-payload-generator` places ancilliary data straight after the main blob
(`blob_handler.c`), i.e. at `_edata`. The kernel's `Image` file *ends* at
`_edata`, but its `.bss` runs from `__bss_start` to `_end`, well past the end of
the file — so an appended DTB lands inside the kernel's own `.bss` and
`clear_bss()` wipes it. Linux then sees a truncated tree: no `/chosen` (it falls
back to `CONFIG_CMDLINE`) and no `/memory` (empty memblock, followed by an
access fault in `paging_init`). The failure looks nothing like a DTB problem.

`make-kernel-image.sh` therefore relocates the ancilliary chunk to `DTB_ADDR`
(default `0x88000000`) by rewriting its descriptor — safe against the header
CRC, which covers only `[0, chunkTableOffset)`, and the per-chunk CRCs cover
payload data only. It also refuses a `DTB_ADDR` below `_end`, derived from
`System.map`, and runs `validate-payload.py` on the finished image, which checks
chunk overlaps, DRAM membership, `no-map` regions, alignment and the HSS boot
image size limit.

## The 2 MiB boot image cap

`mHSS_MAX_BOOT_IMAGE_SIZE` (was a hard-coded 2 MiB) is a sanity limit on the
`bootImageLength` read from storage. A U-Boot payload fits; a kernel `Image`
does not. It is now `SERVICE_BOOT_MAX_IMAGE_SIZE` (default still 2 MiB). Enable
`def_config_direct-kernel_qspi-micron` (16 MiB) — or set
`CONFIG_SERVICE_BOOT_MAX_IMAGE_SIZE=0x1000000` on an eMMC config — to boot a
kernel this way.

## Console

Use a **bare** `earlycon`:

```
console=ttyS1,115200 earlycon ignore_loglevel
```

`earlycon=uart8250,mmio32,0x20100000` assumes `reg-shift = 0`. The real MMUART
has 32-bit-spaced registers (`reg-shift = 2` in the board DT), so that form
polls the wrong register for THRE and **spins forever on the first printk** (the
kernel looks dead). A bare `earlycon` takes `reg-shift`, `reg-io-width` and
`current-speed` from `/chosen/stdout-path`, which works on hardware and in
QEMU. RISC-V calls `parse_dtb()` before `parse_early_param()`, so the DT is
already available when the parameter is handled.

## Build and boot

```sh
# build the HSS with the direct-kernel config (see PROJECT.md for the toolchain env)
cp boards/mpfs-icicle-kit/def_config_examples/def_config_direct-kernel_qspi-micron .config
make genconfig && make -j"$(nproc)"

# wrap kernel + dtb into a 32 MiB QSPI flash image (and a raw boot image)
KERNEL=~/linux/arch/riscv/boot/Image DTB=.../mpfs-icicle-kit-qemu.dtb \
  OUT=$PWD/kernel-flash.img bash tools/direct-kernel/make-kernel-image.sh

# QEMU
QSPI_IMAGE=$PWD/kernel-flash.img ... qemu ... -drive if=sd,file=rootfs.img,format=raw

# hardware: boot from eMMC/SD - write the *raw* boot image at offset 0
# (HSS YMODEM utility, menu option 5 "MMC Write"; the host-side driver is on
# the qspi-redundant-striped branch / workspace tools/project/ymodem-flash.py)
# and remember the HSS boot image cap above.
```

Outputs: `$OUT` (padded flash image, for QSPI/QEMU) and `$OUT.raw` (raw boot
image, for eMMC/SD offset 0).

## Status

- **QEMU:** boots to a busybox shell (`Run /init`, `~ #`) with an initramfs.
- **Hardware (Icicle Kit, MPFS250T):** the boot chain is proven. HSS reads the
  boot image from eMMC offset 0 (`Boot image set name: ...::DirectKernel`,
  hart mask `0x1e`, ancilliary data at `0x88000000`), OpenSBI reports
  `Domain1 HARTs : 1*,2*,3*,4*`, and Linux prints `Machine model: Microchip
  PolarFire-SoC Icicle Kit`, `smp: Brought up 1 node, 4 CPUs`,
  `Memory: 1967624K available`, PLIC with 4 handlers, the 8250 console and the
  SDHCI/mmc driver.
- **Open:** userspace on hardware. The kernel panics with
  `uncompression error` unpacking the built-in initramfs. The same Image boots
  fine in QEMU, and the HSS's own CRC32 of the *received* payload disagrees with
  the host for a 4.2 MB image while matching exactly for a 2 KB one (with all
  per-packet CRC16s valid and the byte count exact), so the payload data is
  being corrupted on the way to the board for larger images. Options: verify the
  receive with the HSS's printed CRC32 and retry, shrink the payload (the HSS
  supports a compressed boot image), or use the redundant-striped image path
  whose per-chunk CRCs are checked at boot.
