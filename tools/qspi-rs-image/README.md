# qspi-rs-image

Build a redundant, striped boot image for the storage-independent HSS
`CONFIG_SERVICE_BOOT_REDUNDANT` boot flow (storage selected with
`CONFIG_SERVICE_BOOT_REDUNDANT_QSPI` or `CONFIG_SERVICE_BOOT_REDUNDANT_MMC`).

This is an **optional, separate** boot mode.  When the configuration is
off, each storage keeps its original HSS boot code (a single HSS boot image at
offset 0 for QSPI, or the GPT/partition lookup for MMC) unchanged.

The on-storage layout is identical for every backend, so this one tool builds
both QSPI flash images and MMC/SD-card images.

## Layout

Three images - the HSS uboot boot image, a Linux kernel `Image` and a device
tree blob - are each stored as three redundant copies, giving nine chunks in
total.  The chunks are **striped by image**, so the copies of any one image
are spread across the flash:

```
+---------+---------+---------+---------+---------+---------+---------+---------+---------+
| uboot 0 | kernel0 | dtb  0  | uboot 1 | kernel1 | dtb  1  | uboot 2 | kernel2 | dtb  2  |
+---------+---------+---------+---------+---------+---------+---------+---------+---------+
```

## Per-chunk headers

Every chunk begins with its own `HSS_RS_ChunkHeader`.  There is **no single
global header**, so corrupting one header can only ever lose the one copy it
belongs to; the HSS simply tries the next copy.

| field         | meaning                                        |
|---------------|------------------------------------------------|
| `magic`       | `0x52534348` (`RSCH`)                          |
| `version`     | `2`                                            |
| `imageId`     | 0 = uboot, 1 = kernel, 2 = dtb                 |
| `copyIndex`   | redundant copy index (0..2)                    |
| `payloadSize` | actual payload bytes following the header      |
| `payloadCrc`  | CRC32 of the payload                           |
| `headerCrc`   | CRC32 of the header with `headerCrc` zeroed    |
| `ownerHart`   | enum `HSSHartId` (1..4 = u54_1..u54_4) whose PMP
                  must permit a write to `execAddr`; the HSS
                  checks this before staging                |
| `execAddr`    | DDR staging address                            |
| `flashOffset` | absolute flash offset (cross-checked by HSS)   |

The header is 48 bytes; the C definition lives in
`services/boot/hss_boot_rsimage.h` and is pinned with `_Static_assert`.

## Precalculated, constant offsets

The chunk offsets are `#define`d constants in `hss_boot_rsimage.h`
(`HSS_RS_OFF_<IMAGE>_<COPY>`), derived from the fixed, block-aligned chunk
capacities:

```
HSS_RS_BOOT_BLOCK        = 0x10000
HSS_RS_CHUNK_UBOOT_SIZE  = 0x100000
HSS_RS_CHUNK_KERNEL_SIZE = 0x300000
HSS_RS_CHUNK_DTB_SIZE    = 0x10000
HSS_RS_COPY_STRIDE       = 0x410000   (sum of the three chunks)

HSS_RS_OFF_UBOOT_0 = 0x010000   HSS_RS_OFF_UBOOT_1 = 0x420000   HSS_RS_OFF_UBOOT_2 = 0x830000
HSS_RS_OFF_KERNEL_0= 0x110000   HSS_RS_OFF_KERNEL_1= 0x520000   HSS_RS_OFF_KERNEL_2= 0x930000
HSS_RS_OFF_DTB_0   = 0x410000   HSS_RS_OFF_DTB_1   = 0x820000   HSS_RS_OFF_DTB_2   = 0xc30000
```

The HSS therefore never reads anything to locate a chunk.  The actual payload
size lives in each chunk header, so the images can be smaller than their
chunk capacity (they must not be larger).

### Generated layout (optional)

The capacities above are the committed defaults.  If you would rather have the
layout follow the images, run the tool with `--auto-layout`, which sizes every
chunk to its payload (rounded up to `--chunk-align`, default 4 KiB, plus any
`--slack`) and computes the offsets, and `--emit-header` to write the matching
C header:

```sh
./qspi-rs-image.py --uboot … --kernel … --dtb … --output rs.img \
    --auto-layout --emit-header ../../services/boot/hss_boot_rsimage_layout.h
```

`services/boot/hss_boot_rsimage.h` includes `hss_boot_rsimage_layout.h` when it
exists, so the firmware picks the generated layout up automatically.  **The HSS
must be rebuilt and reprogrammed after the layout changes**, because the chunk
sizes/offsets are compile-time constants (that is the whole point: the HSS
never reads a table to find a chunk).  Without a generated header the committed
defaults are used and the tool parses them straight out of the C header, so the
tool and the firmware cannot drift apart.

## Usage

```sh
./qspi-rs-image.py \
    --uboot  uboot_boot.bin \
    --kernel arch/riscv/boot/Image \
    --dtb    mpfs-icicle-kit.dtb \
    --output rs-flash.img
```

Useful options:

* `--header` - path to `hss_boot_rsimage.h` (default points into the HSS tree).
* `--flash-size 32M` - total flash size.
* `--owner-hart u54_1` - U54 whose PMP must allow writes to the staging
  addresses (default `u54_1`); recorded in every chunk header.
* `--auto-layout` / `--chunk-align` / `--slack` - derive chunk sizes and
  offsets from the images instead of the header constants.
* `--emit-header PATH` - write the generated C layout header.
* `--kernel-load` / `--dtb-load` - DDR staging addresses.  The kernel and dtb
  must land in the HSS `ddrhi` window (`0x1000000000`..`0x1076000000`); the
  low `ddr` window is only 32 MiB and is used for the QSPI cache.
* `--corrupt image:copy` - flip a byte in a copy's **payload**.
* `--corrupt-header image:copy` - flip a byte in a copy's **chunk header**.

## Enabling the HSS mode

```sh
cd ../..    # hart-software-services
# select the option via menuconfig, or edit .config:
#   CONFIG_SERVICE_QSPI=y
#   CONFIG_SERVICE_QSPI_MICRON_MQ25T=y
#   CONFIG_SERVICE_BOOT_REDUNDANT=y
#   CONFIG_SERVICE_BOOT_REDUNDANT_QSPI=y      # or _MMC=y
make BOARD=mpfs-icicle-kit-es menuconfig
```

Then boot the generated image with QEMU as usual, supplying it with
`-drive if=mtd,file=rs-flash.img`.

## Booting the staged kernel

The HSS stages the kernel and dtb into DDR (default `0x1050000000` and
`0x1070000000`).  U-Boot can start them with:

```
booti 0x1050000000 - 0x1070000000
```
