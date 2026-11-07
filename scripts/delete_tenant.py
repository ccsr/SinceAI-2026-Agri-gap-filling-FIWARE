#!/usr/bin/env python3
"""
delete_tenant.py - delete ALL entities in one NGSI-LD tenant (every type the tenant reports in /types).
Other tenants are not touched. Irreversible: asks for confirmation unless --yes.

  python3 scripts/delete_tenant.py --tenant apetit_ccn
  python3 scripts/delete_tenant.py --tenant apetit_ccn --yes
  python3 scripts/delete_tenant.py --tenant apetit_ccn --dry-run      # only count
"""
import argparse
import json
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from load_to_broker import request, wait_for_broker  # noqa: E402


def types_in(broker, tenant):
    st, body, _ = request("GET", f"{broker}/ngsi-ld/v1/types", tenant)
    if st != 200:
        sys.exit(f"ERROR: GET /types for tenant '{tenant}' -> HTTP {st}: {body[:300]}")
    data = json.loads(body or "[]")
    if isinstance(data, dict):  # EntityTypeList form: {"typeList": [...]}
        data = data.get("typeList", [])
    return [t["id"] if isinstance(t, dict) else t for t in data]


def ids_of(broker, tenant, etype, limit=100):
    q = urllib.parse.quote(etype, safe="")
    st, body, _ = request("GET", f"{broker}/ngsi-ld/v1/entities?type={q}&limit={limit}", tenant)
    if st != 200:
        sys.exit(f"ERROR listing {etype}: HTTP {st}: {body[:300]}")
    return [e["id"] for e in json.loads(body or "[]")]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--broker", default="http://localhost:1026")
    ap.add_argument("--tenant", required=True)
    ap.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if not a.tenant.strip():
        sys.exit("Refusing to run on the default tenant (empty name).")
    broker = a.broker.rstrip("/")
    wait_for_broker(broker)
    types = types_in(broker, a.tenant)
    counts = {}
    for t in types:
        st, body, hdr = request("GET", f"{broker}/ngsi-ld/v1/entities?type={urllib.parse.quote(t, safe='')}"
                                       f"&count=true&limit=1", a.tenant)
        counts[t] = {k.lower(): v for k, v in hdr.items()}.get("ngsild-results-count", "?")
    print(f"Tenant '{a.tenant}': {len(types)} types")
    for t, n in counts.items():
        print(f"  {n:>7}  {t}")
    if not types or a.dry_run:
        print("Nothing deleted." if not types else "Dry run - nothing deleted.")
        return
    if not a.yes and input(f"Delete ALL entities in tenant '{a.tenant}'? Type the tenant name to confirm: ") != a.tenant:
        sys.exit("Not confirmed - nothing deleted.")
    total = 0
    for t in types:
        while True:
            ids = ids_of(broker, a.tenant, t)
            if not ids:
                break
            st, body, _ = request("POST", f"{broker}/ngsi-ld/v1/entityOperations/delete", a.tenant, ids,
                                  ctype="application/json")
            if st not in (200, 204, 207):
                sys.exit(f"ERROR deleting {t}: HTTP {st}: {body[:300]}")
            total += len(ids)
            print(f"  deleted {total} ...", end="\r", flush=True)
    left = types_in(broker, a.tenant)
    print(f"\nDeleted {total} entities from tenant '{a.tenant}'. Types left: {left or 'none'}")


if __name__ == "__main__":
    main()
