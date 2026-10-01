#!/usr/bin/env python3
"""
ymodem-flash.py -- drive the custom HSS YMODEM utility over the serial console
to erase + program the external QSPI flash with a redundant/striped image.

Phases:
  0. (optional) reset SoC via OpenOCD so the eNVM HSS boots
  1. enter pre-boot TinyCLI ("Press a key to enter CLI")
  2. run YMODEM command
  3. menu 1: QSPI Erase Bulk
  4. menu 3: YMODEM Receive  (Python YMODEM-1K sender)
  5. menu 4: QSPI Write (offset 0)
  6. menu 6: Quit
  7. reset SoC again to boot from QSPI

Usage:
  python3 ymodem-flash.py --image rs-flash-16m.img [--no-erase] [--no-reset] [--skip-cli] [--phase N]
"""

import os
import sys
import time
import select
import termios
import argparse
import subprocess

DEV = "/dev/ttyUSB0"
BAUD = termios.B115200

SOH = 0x01
STX = 0x02
EOT = 0x04
ACK = 0x06
NAK = 0x15
CAN = 0x18
CRC_C = 0x43

LOGF = None


def log(msg, also_stdout=True):
    line = "[%s] %s" % (time.strftime("%H:%M:%S"), msg)
    if also_stdout:
        print(line, flush=True)
    if LOGF:
        LOGF.write(line + "\n")
        LOGF.flush()


def log_raw(data):
    if LOGF:
        LOGF.write(data.decode("latin-1"))
        LOGF.flush()


def open_port(dev):
    fd = os.open(dev, os.O_RDWR | os.O_NOCTTY)
    attrs = termios.tcgetattr(fd)
    attrs[0] = 0                       # iflag
    attrs[1] = 0                       # oflag
    attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL  # cflag
    attrs[3] = 0                       # lflag
    attrs[4] = BAUD                    # ispeed
    attrs[5] = BAUD                    # ospeed
    cc = attrs[6]
    cc[termios.VMIN] = 0
    cc[termios.VTIME] = 0
    termios.tcsetattr(fd, termios.TCSANOW, attrs)
    termios.tcflush(fd, termios.TCIOFLUSH)
    return fd


def read_all(fd, timeout=0.25):
    data = b""
    end = time.time() + timeout
    while True:
        remain = end - time.time()
        if remain <= 0:
            break
        r, _, _ = select.select([fd], [], [], remain)
        if not r:
            break
        c = os.read(fd, 4096)
        if not c:
            break
        data += c
    if data:
        log_raw(data)
    return data


def write_all(fd, data, pace=0.0):
    off = 0
    while off < len(data):
        n = os.write(fd, data[off:off + 4096])
        off += n
        if pace:
            time.sleep(pace)


def send(fd, data):
    write_all(fd, data)


def wait_for(fd, markers, timeout, collect=b"", progress=None):
    """Wait until any marker (bytes) found in the accumulated stream."""
    if isinstance(markers, bytes):
        markers = [markers]
    end = time.time() + timeout
    buf = collect
    last = None
    while time.time() < end:
        chunk = read_all(fd, 0.25)
        if chunk:
            buf += chunk
        for m in markers:
            if m in buf:
                return True, buf, m
        if progress and (time.time() - (last or 0)) > progress[1]:
            last = time.time()
            progress[0]()
    return False, buf, None


# ---------------------------------------------------------------------------
# HSS TinyCLI / YMODEM menu input is flaky: characters can be dropped or
# corrupted.  Send commands one character at a time and confirm each is echoed
# back before sending the next.  Menu options are single characters.
# ---------------------------------------------------------------------------
def send_verified(fd, text, char_delay=0.25, retries=15):
    """Type a TinyCLI command slowly, resending each char until echoed."""
    for ch in text:
        cb = ch.encode()
        for _ in range(retries):
            write_all(fd, cb)
            time.sleep(char_delay)
            echo = read_all(fd, 0.30)
            if cb in echo:
                break
        else:
            log("WARN: char %r not echoed by HSS" % ch)
    write_all(fd, b"\r")
    time.sleep(0.3)


def select_menu(fd, ch, expect, timeout=15.0, attempts=6):
    """Send one YMODEM-menu digit until the expected banner appears."""
    buf = b""
    for attempt in range(attempts):
        write_all(fd, ch.encode())
        ok, buf2, _ = wait_for(fd, expect, timeout, collect=buf)
        buf = buf2
        if ok:
            return True, buf
        log("menu option %r: no %r yet (attempt %d)" % (ch, expect, attempt + 1))
    return False, buf


# ---------------------------------------------------------------------------
# YMODEM
# ---------------------------------------------------------------------------
def crc16(data):
    crc = 0
    for byte in data:
        crc ^= (byte << 8) & 0xFFFF
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


def build_packet(blk, payload):
    if len(payload) <= 128:
        payload = payload + b"\x00" * (128 - len(payload))
        hdr = bytes([SOH, blk & 0xFF, (0xFF - blk) & 0xFF])
    else:
        payload = payload + b"\x00" * (1024 - len(payload))
        hdr = bytes([STX, blk & 0xFF, (0xFF - blk) & 0xFF])
    c = crc16(payload)
    return hdr + payload + bytes([(c >> 8) & 0xFF, c & 0xFF])


def wait_ack(fd, timeout=5.0):
    """Return 'ACK', 'NAK', 'C', 'CAN' or None.

    Returns *immediately* on the first ACK/NAK/CAN seen -- do not wait out a
    read timeout after the byte has arrived, or every 1 KiB block pays a full
    extra poll interval.
    """
    end = time.time() + timeout
    while time.time() < end:
        r, _, _ = select.select([fd], [], [], 0.05)
        if not r:
            continue
        chunk = os.read(fd, 4096)
        if not chunk:
            continue
        log_raw(chunk)
        for b in chunk:
            if b == ACK:
                return "ACK"
            if b == NAK:
                return "NAK"
            if b == CAN:
                return "CAN"
    return None


def ymodem_send(fd, path, name):
    size = os.path.getsize(path)
    log("YMODEM: sending %s (%d bytes)" % (name, size))

    # Header block 0: "name\0<size>\0"
    payload = name.encode() + b"\x00" + str(size).encode() + b"\x00"
    pkt = build_packet(0, payload)
    for attempt in range(10):
        send(fd, pkt)
        r = wait_ack(fd, 10.0)
        if r == "ACK":
            log("YMODEM: header ACK")
            break
        log("YMODEM: header -> %s (attempt %d)" % (r, attempt + 1))
    else:
        raise RuntimeError("YMODEM: header not acknowledged")

    # drain any trailing 'C'
    read_all(fd, 0.3)

    blk = 1
    sent = 0
    t0 = time.time()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1024)
            if not chunk:
                break
            pkt = build_packet(blk, chunk)
            ok = False
            for attempt in range(10):
                send(fd, pkt)
                r = wait_ack(fd, 5.0)
                if r == "ACK":
                    ok = True
                    break
                log("YMODEM: blk %d -> %s (attempt %d)" % (blk, r, attempt + 1))
                if r == "CAN":
                    raise RuntimeError("YMODEM: receiver cancelled")
            if not ok:
                raise RuntimeError("YMODEM: block %d not acknowledged" % blk)
            sent += len(chunk)
            blk = (blk + 1) & 0xFF
            if sent % (1024 * 256) == 0:
                dt = time.time() - t0
                kbps = sent / dt / 1024 if dt else 0
                pct = 100.0 * sent / size
                log("YMODEM: %d/%d bytes (%.1f%%, %.1f KiB/s)"
                    % (sent, size, pct, kbps))

    # EOT handshake: EOT -> NAK -> EOT -> ACK + 'C' -> null block -> ACK
    for attempt in range(10):
        send(fd, bytes([EOT]))
        r = wait_ack(fd, 5.0)
        log("YMODEM: first EOT -> %s" % r)
        if r in ("NAK", None):
            break
    send(fd, bytes([EOT]))
    r = wait_ack(fd, 5.0)
    log("YMODEM: second EOT -> %s" % r)
    # null batch-terminator block
    send(fd, build_packet(0, b""))
    r = wait_ack(fd, 5.0)
    log("YMODEM: null block -> %s" % r)
    log("YMODEM: transfer complete")
    return size


# ---------------------------------------------------------------------------
def reset_board():
    # Reset the SoC before/after flashing.  Set HSS_RESET_CMD to a host command
    # (e.g. an OpenOCD reset or a board power/serial reset helper).  When unset,
    # no reset is issued -- the boot CLI window is caught by sending CR below.
    cmd = os.environ.get("HSS_RESET_CMD", "").strip()
    if not cmd:
        log("HSS_RESET_CMD not set; skipping reset")
        return
    log("Resetting SoC: %s" % cmd)
    subprocess.run(cmd, shell=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    log("Reset command returned")


def enter_cli(fd, timeout=30):
    log("Waiting for pre-boot TinyCLI ...")
    start = time.time()
    buf = b""
    while time.time() - start < timeout:
        send(fd, b"\r")
        chunk = read_all(fd, 0.35)
        buf += chunk
        if b">> " in buf:
            log("Entered TinyCLI")
            read_all(fd, 0.3)
            return True
    return False


def main():
    global LOGF
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", required=True)
    ap.add_argument("--name", default=None)
    ap.add_argument("--dev", default=DEV)
    ap.add_argument("--log", default="/tmp/ymodem-flash.log")
    ap.add_argument("--no-erase", action="store_true")
    ap.add_argument("--no-reset", action="store_true")
    ap.add_argument("--mmc", action="store_true",
                    help="write to MMC (YMODEM option 5) instead of QSPI (option 4); "
                         "no erase is performed")
    ap.add_argument("--dry-run", action="store_true",
                    help="only reset, enter CLI and open YMODEM menu, then quit")
    args = ap.parse_args()

    name = args.name or os.path.basename(args.image)
    if not os.path.exists(args.image):
        print("image not found: %s" % args.image)
        return 2

    LOGF = open(args.log, "w")

    if not args.no_reset:
        reset_board()
        time.sleep(0.3)

    fd = open_port(args.dev)
    log("opened %s" % args.dev)

    if not enter_cli(fd):
        log("ERROR: did not enter CLI")
        return 3

    # ---- YMODEM command ----
    send_verified(fd, "YMODEM")
    ok, buf, _ = wait_for(fd, [b"Select a number:", b"QSPI/MMC Utility"],
                          12.0, collect=b"")
    if not ok:
        log("retrying YMODEM command")
        send_verified(fd, "YMODEM")
        ok, buf, _ = wait_for(fd, [b"Select a number:"], 12.0, collect=buf)
    if not ok:
        log("ERROR: YMODEM menu did not appear")
        return 4
    log("YMODEM utility menu up")

    if args.dry_run:
        log("DRY RUN: menu reached; quitting without touching flash")
        write_all(fd, b"6")
        time.sleep(0.5)
        read_all(fd, 1.0)
        os.close(fd)
        log("DRY RUN OK")
        return 0

    # ---- erase ----
    if not args.no_erase and not args.mmc:
        ok, buf = select_menu(fd, "1",
            [b"Erasing all of QSPI", b"Initialized Flash (JEDEC", b"FAILED"], 30.0)
        # if QSPI init/erase already failed, fail fast instead of waiting 15 min
        if b"FAILED" in buf and b"Erasing all of QSPI" not in buf:
            log("ERROR: QSPI init/erase FAILED (see HSS output above)")
            return 5
        if not ok:
            log("WARN: no erase banner; continuing anyway")
        log("Erasing QSPI (this can take a while) ...")
        ok, buf, m = wait_for(fd, [b" Success", b" FAILED"], 900.0, collect=buf,
                              progress=(lambda: log("  ... erase still running", False), 30))
        if b"FAILED" in buf:
            log("ERROR: erase FAILED")
            return 5
        if not ok:
            log("ERROR: erase did not complete")
            return 5
        log("QSPI erase complete")
        # menu should be reprinted
        wait_for(fd, [b"Select a number:"], 10.0, collect=buf)
    else:
        log("Skipping erase (--no-erase or --mmc)")

    # ---- receive ----
    ok, buf = select_menu(fd, "3", [b"CTRL-C to cancel"], 12.0)
    if not ok:
        log("WARN: no receive banner")

    # wait for the receiver's 'C'
    log("Waiting for YMODEM 'C' ...")
    end = time.time() + 20
    found = False
    buf = b""
    while time.time() < end:
        chunk = read_all(fd, 0.25)
        if not chunk:
            continue
        buf += chunk
        if CRC_C in chunk:
            found = True
            break
    if not found:
        log("ERROR: receiver never sent 'C'")
        return 6
    log("Receiver ready ('C' seen)")

    try:
        ymodem_send(fd, args.image, name)
    except Exception as e:
        log("ERROR: %s" % e)
        return 7

    ok, buf, _ = wait_for(fd, [b"Received ", b"YMODEM failed"], 20.0)
    if ok and b"YMODEM failed" in buf:
        log("ERROR: HSS reports YMODEM failure")
        return 7
    log("HSS confirmed receive")
    wait_for(fd, [b"Select a number:"], 10.0, collect=buf)

    # ---- program ----
    if args.mmc:
        prog_ch = "5"
        prog_banner = b"Programming MMC"
        prog_name = "MMC"
    else:
        prog_ch = "4"
        prog_banner = b"Programming QSPI"
        prog_name = "QSPI"

    ok, buf = select_menu(fd, prog_ch,
        [b"Attempting to flash received data", prog_banner], 15.0)
    if not ok:
        log("WARN: no program banner")
    log("Programming %s ... (may take a few minutes)" % prog_name)
    ok, buf, m = wait_for(fd, [b" Success", b" FAILED"], 900.0, collect=buf,
                          progress=(lambda: log("  ... programming still running", False), 30))
    if b"FAILED" in buf:
        log("ERROR: programming FAILED")
        return 8
    if not ok:
        log("ERROR: programming did not complete")
        return 8
    log("QSPI programming complete")

    # ---- quit ----
    wait_for(fd, [b"Select a number:"], 10.0, collect=buf)
    send(fd, b"6\r")
    time.sleep(0.5)
    read_all(fd, 1.0)
    log("Quit YMODEM utility")

    os.close(fd)

    # ---- reset to boot from QSPI ----
    if not args.no_reset:
        time.sleep(0.5)
        reset_board()

    log("DONE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
