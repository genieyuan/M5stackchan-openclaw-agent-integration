#!/bin/bash
# Full 16 MB flash backup in 1 MB chunks with retries (the CoreS3 native USB drops long reads).
# The device stays in its ROM bootloader afterwards (silent). The image contains your Wi-Fi password: keep it private.
# usage: PORT=/dev/cu.usbmodemXXXX tools/backup_flash.sh out.bin
set -euo pipefail; PORT=${PORT:?set PORT}; out=${1:?output file}; tmp=$(mktemp -d); : > "$out"
for n in $(seq 0 15); do off=$(printf '0x%x' $((n*0x100000)))
  for try in 1 2 3 4 5; do
    esptool --port "$PORT" --before default-reset --after no-reset read-flash $off 0x100000 $tmp/c.bin >/dev/null 2>&1 \
      && [ "$(stat -f%z $tmp/c.bin 2>/dev/null || stat -c%s $tmp/c.bin)" -eq 1048576 ] && break
    [ $try -eq 5 ] && { echo "chunk $n failed"; exit 1; }; sleep 2; done
  cat $tmp/c.bin >> "$out"; done
rm -rf $tmp; shasum -a 256 "$out"
