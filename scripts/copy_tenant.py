#!/usr/bin/env python3
"""
copy_tenant.py - copy every entity of one NGSI-LD tenant into another tenant on the same broker.
The source tenant is only read. Entities are read in their stored form (same @context the broker
used) and upserted unchanged, so attribute names, sub-properties and geometry are preserved.

  python3 scripts/copy_tenant.py                                   # apetit_ccn -> apetit_ccn_post_ml
  python3 scripts/copy_tenant.py --source apetit_ccn --target apetit_ccn_post_ml
  python3 scripts/copy_tenant.py --dry-run                         # only count
"""
import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from load_to_broker import request, wait_for_broker  # noqa: E402
from delete_tenant import types_in  # noqa: E402

PAGE = 100


def count(broker, tenant, etype):
    q = urllib.parse.quote(etype, safe="")
    st, body, hdr = request("GET", f"{broker}/ngsi-ld/v1/entities?type={q}&count=true&limit=1", tenant)
    if st != 200:
        return None
    n = {k.lower(): v for k, v in hdr.items()}.get("ngsild-results-count")
    return int(n) if n is not None else None


def read_page(broker, tenant, etype, offset):
    """One page of full entities. Asks for JSON-LD so every entity carries the @context it was compacted with."""
    q = urllib.parse.quote(etype, safe="")
    url = f"{broker}/ngsi-ld/v1/entities?type={q}&limit={PAGE}&offset={offset}"
    req_headers = {"Accept": "application/ld+json", "NGSILD-Tenant": tenant}
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=req_headers), timeout=120) as r:
            return json.loads(r.read().decode() or "[]")
    except urllib.error.HTTPError as e:
        sys.exit(f"ERROR reading {etype} offset {offset} from '{tenant}': HTTP {e.code}: {e.read().decode()[:300]}")


def write_batch(broker, tenant, ents):
    with_ctx = all("@context" in e for e in ents)
    body = ents if with_ctx else [{k: v for k, v in e.items() if k != "@context"} for e in ents]
    st, resp, _ = request("POST", f"{broker}/ngsi-ld/v1/entityOperations/upsert", tenant, body,
                          ctype="application/ld+json" if with_ctx else "application/json")
    if st in (200, 201, 204):
        return len(ents), []
    if st == 207:
        errs = json.loads(resp or "{}").get("errors", [])
        return len(ents) - len(errs), [json.dumps(x)[:300] for x in errs]
    return 0, [f"HTTP {st}: {resp[:300]}"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--broker", default="http://localhost:1026")
    ap.add_argument("--source", default="apetit_ccn")
    ap.add_argument("--target", default="apetit_ccn_post_ml")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if not a.source.strip() or not a.target.strip() or a.source == a.target:
        sys.exit("Give two different, non-empty tenant names.")
    broker = a.broker.rstrip("/")
    wait_for_broker(broker)

    types = types_in(broker, a.source)
    if not types:
        sys.exit(f"Tenant '{a.source}' has no entities - load it first (python3 scripts/load_ccn.py).")
    st, body, _ = request("GET", f"{broker}/ngsi-ld/v1/types", a.target)  # unknown tenant -> error, i.e. empty
    existing = json.loads(body or "[]") if st == 200 else []
    if isinstance(existing, dict):
        existing = existing.get("typeList", [])
    if existing:
        print(f"Note: tenant '{a.target}' already has {len(existing)} types; matching ids are overwritten, others kept.")

    print(f"Copy '{a.source}' -> '{a.target}'{'  (dry run)' if a.dry_run else ''}")
    total_ok = total_err = 0
    for t in types:
        n_src = count(broker, a.source, t)
        ok, errors, seen, offset = 0, [], set(), 0
        while True:
            page = read_page(broker, a.source, t, offset)
            page = [e for e in page if e["id"] not in seen]
            if not page:
                break
            seen.update(e["id"] for e in page)
            if a.dry_run:
                ok += len(page)
            else:
                c_ok, c_err = write_batch(broker, a.target, page)
                ok += c_ok
                errors += c_err
            offset += PAGE
            print(f"  {t.rsplit('/', 1)[-1]:<22} {len(seen):>6}/{n_src}", end="\r", flush=True)
        n_tgt = n_src if a.dry_run else count(broker, a.target, t)
        mark = "OK" if n_tgt == n_src and not errors else "CHECK"
        print(f"  {t.rsplit('/', 1)[-1]:<22} source {n_src:>6}  copied {ok:>6}  target {n_tgt:>6}  {mark}")
        for e in errors[:3]:
            print(f"      error: {e}")
        total_ok += ok
        total_err += len(errors)
    print(f"\nDone: {total_ok} entities copied, {total_err} errors.")


if __name__ == "__main__":
    main()
