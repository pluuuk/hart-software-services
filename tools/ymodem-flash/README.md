# ymodem-flash

Host-side flasher for the HSS **YMODEM** utility.  Drives the pre-boot TinyCLI
over the HSS UART, enters `YMODEM`, receives the image into DDR and writes it to
the selected storage (QSPI option 4 / MMC option 5).

This is the production transfer path for a boot image built by
`tools/qspi-rs-image` (see `make-rs-image.sh`).

## Usage

    python3 ymodem-flash.py --image rs-mmc.img --mmc --no-reset
    python3 ymodem-flash.py --image rs-flash-16m.img

Notes:
- Transfer takes ~20 min for 12.25 MiB at 115200 baud.
- `--no-reset` is normally used: reset the board and press a key during the
  TinyCLI countdown to land in the pre-boot CLI, then run the tool.
- The MMC write starts at byte offset 0: use a dedicated SD card, or accept
  overwriting the start of the eMMC.
- `HSS_RESET_CMD` (optional) is a host shell command run before/after the
  transfer (e.g. a power or reset helper).  Unset by default.
