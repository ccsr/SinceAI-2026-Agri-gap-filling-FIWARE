#!/usr/bin/env bash
# smoke_test.sh - end-to-end test against tests/mock_broker.py (no real broker, no Docker).
# Needs the source CSVs in source-data/ and the gap-fill dependencies installed locally:
#   pip install -r gapfill/requirements-gapfill.txt
set -euo pipefail
cd "$(dirname "$0")/.."
PORT=11026
B="http://127.0.0.1:$PORT"

python3 tests/mock_broker.py "$PORT" & MOCK=$!
trap 'kill $MOCK 2>/dev/null' EXIT
sleep 1

python3 scripts/convert_ccn_to_ngsild.py | tail -3
python3 scripts/load_ccn.py --broker "$B" | tail -1
python3 scripts/copy_tenant.py --broker "$B" | tail -1

python3 gapfill/gapfill_ccn.py --broker "$B" | grep -E "AgriCropCycle entities|^Fills"
cp results/gapfill/fills.csv /tmp/fills_run1.csv
python3 gapfill/gapfill_ccn.py --broker "$B" | grep -E "AgriCropCycle entities|^Fills"

if cmp -s /tmp/fills_run1.csv results/gapfill/fills.csv; then
  echo "OK: second run gives identical fills"
else
  echo "FAIL: fills differ between runs"; exit 1
fi
python3 gapfill/gapfill_ccn.py --broker "$B" --tenant apetit_ccn >/dev/null 2>&1 \
  && { echo "FAIL: wrote into the as-reported tenant"; exit 1; } || echo "OK: as-reported tenant is protected"
