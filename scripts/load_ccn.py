#!/usr/bin/env python3
"""
load_ccn.py - load the Navigator entities (entities/ccn/) into tenant `apetit_ccn` of the coraine broker.
Reuses the batch-upsert logic of load_to_broker.py. Other tenants are not touched.

  python3 scripts/load_ccn.py                      # inline @context, tenant apetit_ccn
  python3 scripts/load_ccn.py --purge              # delete these entity types in the tenant first
  python3 scripts/load_ccn.py --tenant other_name --broker http://host:1026
  python3 scripts/load_ccn.py --dry-run
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import load_to_broker as L  # noqa: E402

L.TYPE_IRI["Catchment"] = L.ALCA + "Catchment"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--broker", default="http://localhost:1026")
    ap.add_argument("--tenant", default="apetit_ccn")
    ap.add_argument("--batch-size", type=int, default=50)
    ap.add_argument("--context-url", default=None, help="reference a hosted context instead of inline")
    ap.add_argument("--purge", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    custom = json.loads((ROOT / "data-model" / "context.jsonld").read_text())["@context"]
    ctx = [L.CORE, a.context_url] if a.context_url else [L.CORE, custom]
    folder = ROOT / "entities" / "ccn"
    types = [m["type"] for m in json.loads((folder / "manifest.json").read_text())]
    broker = a.broker.rstrip("/")
    if not a.dry_run:
        L.wait_for_broker(broker)
        if a.purge:
            L.purge_tenant(broker, a.tenant, types)
    ok, err = L.load_tenant(broker, a.tenant, folder, ctx, a.batch_size, a.dry_run)
    if not a.dry_run:
        L.verify(broker, a.tenant, types)
    print(f"\n{'DRY RUN - ' if a.dry_run else ''}Done: {ok} entities loaded, {err} errors into tenant '{a.tenant}'")


if __name__ == "__main__":
    main()
