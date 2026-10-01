# Direct kernel boot (skip U-Boot)

Boot a Linux `Image` directly from the HSS, with no U-Boot stage.

## Why it needs no OpenSBI build

The HSS already contains OpenSBI (see the OpenSBI banner on the U54 console).
Its normal flow hands the payload to OpenSBI, which jumps to it in S-mode with
`$a1` pointing at the payload's **ancilliary data**. That is exactly the RISC-V
Linux boot protocol. So the "payload" can be the kernel `Image` itself, with the
device tree as its ancilliary data:

```
payloads:
  Image: {exec-addr: '0x80200000', owner-hart: u54_1, priv-mode: prv_s,
          ancilliary-data: linux.dtb, payload-name: "Linux"}
```

Only the boot hart is started; Linux brings the others up through SBI HSM.

## The 2 MiB boot image cap

`mHSS_MAX_BOOT_IMAGE_SIZE` (was a hard-coded 2 MiB) is a sanity limit on the
`bootImageLength` read from storage. A U-Boot payload fits; a kernel `Image`
does not. It is now `SERVICE_BOOT_MAX_IMAGE_SIZE` (default still 2 MiB). Enable
`def_config_direct-kernel_qspi-micron` (16 MiB) to boot a kernel this way.

## Build and boot (QEMU)

```sh
# build the HSS with the direct-kernel config (see PROJECT.md for the toolchain env)
cp boards/mpfs-icicle-kit/def_config_examples/def_config_direct-kernel_qspi-micron .config
make genconfig && make -j"$(nproc)"

# wrap kernel + dtb into a 32 MiB QSPI image
KERNEL=~/linux/arch/riscv/boot/Image DTB=.../mpfs-icicle-kit-qemu.dtb \
  OUT=$PWD/kernel-flash.img bash tools/direct-kernel/make-kernel-image.sh

# boot
QSPI_IMAGE=$PWD/kernel-flash.img ... qemu ... -drive if=sd,file=rootfs.img,format=raw
```

## Status

The HSS loads the image, OpenSBI prints its banner (`Domain1 Next Address
0x80200000`, next mode S), and the kernel starts (SBI detected, `efi: UEFI not
found`). It then faults in `create_kernel_page_table -> memset` during
`paging_init` with a **store/AMO access fault** (cause 7, i.e. a physical access
fault), before the console is up.

Open items:

- Earlycon: the MMUART has 32-bit registers, so use `earlycon=uart8250,mmio32,0x20100000,115200`.
- `paging_init` physical access fault: needs the kernel's full memory map
  (ring buffer dump). Suspect the FDT / physical memory view rather than PMP
  (the Icicle PMP is permissive: `pmpcfg0=0x9F`, `pmpaddr0=~0`).
- Alternative if the HSS's U-Boot-oriented OpenSBI is the cause: build a
  standalone OpenSBI `fw_payload` (kernel+DTB baked in) and boot it with
  `priv-mode: prv_m, skip-opensbi: true` at `0x80000000`. The in-tree
  `thirdparty/opensbi` is HSS-patched and does not build standalone.
