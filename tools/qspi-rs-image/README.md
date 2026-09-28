# qspi-rs-image

Build a redundant, striped QSPI boot image for the HSS
`CONFIG_SERVICE_BOOT_QSPI_REDUNDANT` boot flow.

This is an **optional, separate** QSPI boot mode.  When the configuration is
off, the original HSS QSPI boot code (a single HSS boot image at flash offset
0) is used unchanged.

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

`qspi-rs-image.py` parses these constants straight out of the C header, so the
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
#   CONFIG_SERVICE_BOOT_QSPI_REDUNDANT=y
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
