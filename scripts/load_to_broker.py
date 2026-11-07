#!/usr/bin/env python3
"""
load_to_broker.py - shared NGSI-LD helpers (HTTP with tenant header, batch upsert, purge, verify).
Used by load_ccn.py, hestia_writeback.py and delete_tenant.py. Standard library only.
Run the loader through:  python3 scripts/load_ccn.py
"""
import argparse
import functools
import http.server
import json
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CORE = "https://uri.etsi.org/ngsi-ld/v1/ngsi-ld-core-context-v1.8.jsonld"
ALCA = "https://uri.fiware.org/ns/data-models/agrifood-lca/"
SDM_AF = "https://smartdatamodels.org/dataModel.Agrifood/"
TYPE_IRI = {
    "AgriSoil": SDM_AF + "AgriSoil", "AgriCrop": SDM_AF + "AgriCrop", "AgriFarm": SDM_AF + "AgriFarm",
    "AgriParcel": SDM_AF + "AgriParcel", "AgriParcelRecord": SDM_AF + "AgriParcelRecord",
    "GrowingSeasonWeather": ALCA + "GrowingSeasonWeather", "AgriCropCycle": ALCA + "AgriCropCycle",
    "ProductCarbonFootprint": ALCA + "ProductCarbonFootprint", "AgriFootprintResult": ALCA + "AgriFootprintResult",
}


# ── HTTP helpers ─────────────────────────────────────────────────────────────
def request(method, url, tenant, body=None, ctype="application/ld+json", timeout=120):
    headers = {"Accept": "application/json"}
    if tenant:
        headers["NGSILD-Tenant"] = tenant
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = ctype
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode() or "", dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), dict(e.headers)


def wait_for_broker(broker, retries=20, delay=2.0):
    print(f"Waiting for broker at {broker} ...", end="", flush=True)
    for _ in range(retries):
        try:
            st, _, _ = request("GET", f"{broker}/ngsi-ld/v1/types", None, timeout=3)
            if st < 500:
                print(" ready.")
                return
        except Exception:
            pass
        print(".", end="", flush=True)
        time.sleep(delay)
    sys.exit(f"\nERROR: no NGSI-LD broker answering at {broker}. "
             f"Start the broker first.")


def serve_context(port):
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(ROOT / "data-model"))
    handler.log_message = lambda *a, **k: None
    srv = http.server.ThreadingHTTPServer(("0.0.0.0", port), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"Serving data-model/ on http://0.0.0.0:{port}/ (context.jsonld)")
    return srv


# ── load / purge ─────────────────────────────────────────────────────────────
def with_context(entities, ctx):
    out = []
    for e in entities:
        e = dict(e)
        e["@context"] = ctx
        out.append(e)
    return out


def upsert(broker, tenant, batch, retries=3):
    url = f"{broker}/ngsi-ld/v1/entityOperations/upsert"
    for attempt in range(1, retries + 1):
        try:
            st, body, _ = request("POST", url, tenant, batch)
        except Exception as ex:  # connection reset, timeout
            if attempt == retries:
                return 0, [str(ex)]
            time.sleep(2 * attempt)
            continue
        if st in (200, 201, 204):
            return len(batch), []
        if st == 207:  # partial success: body lists errors
            try:
                errs = json.loads(body).get("errors", [])
            except Exception:
                errs = [body[:300]]
            return len(batch) - len(errs), [json.dumps(x)[:300] for x in errs]
        if st >= 500 and attempt < retries:
            time.sleep(2 * attempt)
            continue
        return 0, [f"HTTP {st}: {body[:400]}"]
    return 0, ["unknown"]


def load_tenant(broker, tenant, folder, ctx, batch_size, dry_run):
    manifest = json.loads((folder / "manifest.json").read_text())
    print(f"\n══ tenant '{tenant}'  ({folder.relative_to(ROOT)})")
    tot_ok = tot_err = 0
    for m in manifest:
        ents = with_context(json.loads((folder / m["file"]).read_text(encoding="utf-8")), ctx)
        ok = 0
        errors = []
        for i in range(0, len(ents), batch_size):
            chunk = ents[i:i + batch_size]
            if dry_run:
                ok += len(chunk)
            else:
                c_ok, c_err = upsert(broker, tenant, chunk)
                ok += c_ok
                errors += c_err
            if (i // batch_size) % 10 == 0:
                print(f"  {m['type']:<24} {min(i + batch_size, len(ents)):>5}/{len(ents)}", end="\r", flush=True)
        print(f"  {m['type']:<24} {len(ents):>5}/{len(ents)}  ✓ {ok}  ✗ {len(ents) - ok}        ")
        for e in errors[:3]:
            print(f"      error: {e}")
        if len(errors) > 3:
            print(f"      ... {len(errors) - 3} more errors")
        tot_ok += ok
        tot_err += len(ents) - ok
    return tot_ok, tot_err


def purge_tenant(broker, tenant, types):
    print(f"Purging our entity types from tenant '{tenant}' ...")
    for t in types:
        iri = urllib.parse.quote(TYPE_IRI[t], safe="")
        deleted = 0
        while True:
            st, body, _ = request("GET", f"{broker}/ngsi-ld/v1/entities?type={iri}&limit=100", tenant)
            if st != 200:
                print(f"  could not list {t}: HTTP {st}: {body[:200]}")
                break
            ids = [e["id"] for e in json.loads(body or "[]")]
            if not ids:
                break
            request("POST", f"{broker}/ngsi-ld/v1/entityOperations/delete", tenant, ids,
                    ctype="application/json")
            deleted += len(ids)
        if deleted:
            print(f"  deleted {deleted} {t}")


def verify(broker, tenant, types):
    print(f"\nVerification, tenant '{tenant}':")
    for t in types:
        iri = urllib.parse.quote(TYPE_IRI[t], safe="")
        st, body, hdr = request("GET", f"{broker}/ngsi-ld/v1/entities?type={iri}&count=true&limit=1", tenant)
        cnt = {k.lower(): v for k, v in hdr.items()}.get("ngsild-results-count", "?")
        print(f"  {t:<24} HTTP {st}  count={cnt}")


def main():
    sys.exit("This module only holds helpers. Use: python3 scripts/load_ccn.py  (see README.md)")


if __name__ == "__main__":
    main()
