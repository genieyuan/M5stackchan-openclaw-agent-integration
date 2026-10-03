#!/bin/bash
# Fetch Espressif's official NVS parser (Apache-2.0) next to build_nvs.py.
set -euo pipefail; cd "$(dirname "$0")"
for f in nvs_tool.py nvs_parser.py nvs_logger.py nvs_check.py; do
  curl -fsSL -o $f https://raw.githubusercontent.com/espressif/esp-idf/v5.5/components/nvs_flash/nvs_partition_tool/$f; done
