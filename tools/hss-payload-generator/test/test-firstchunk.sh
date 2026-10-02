#!/usr/bin/env bash
#
# Regression test for the firstChunk "chunk 0 cannot be represented" bug.
#
# Two binary payloads on one owner hart must produce firstChunk == 0,
# lastChunk == 1, numChunks == 2.  Before the fix the second payload reset
# firstChunk to 1, orphaning the first payload's chunk while numChunks still
# reported a plausible value.
#
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
gen="$here/../hss-payload-generator"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

printf 'AAAAAAAAAAAAAAAA' >"$work/blob_a.bin"
printf 'BBBBBBBBBBBBBBBB' >"$work/blob_b.bin"

cat >"$work/config.yaml" <<EOF
hart-entry-points: {u54_1: '0x80200000'}
payloads:
  $work/blob_a.bin: {exec-addr: '0x80200000', owner-hart: u54_1, priv-mode: prv_m}
  $work/blob_b.bin: {exec-addr: '0x80400000', owner-hart: u54_1, priv-mode: prv_m}
EOF

"$gen" -c "$work/config.yaml" "$work/image.bin" >/dev/null
dump="$("$gen" -d "$work/image.bin")"

get() { echo "$dump" | awk -v k="$1" '$1 == k { print $2 }'; }

first="$(get 'firstChunk[0]')"
last="$(get 'lastChunk[0]')"
count="$(get 'numChunks[0]')"

echo "firstChunk[0]=$first lastChunk[0]=$last numChunks[0]=$count"

[ "$first" = "0" ] || { echo "FAIL: firstChunk[0] is $first, expected 0"; exit 1; }
[ "$last"  = "1" ] || { echo "FAIL: lastChunk[0] is $last, expected 1"; exit 1; }
[ "$count" = "2" ] || { echo "FAIL: numChunks[0] is $count, expected 2"; exit 1; }

echo "PASS"
