#!/usr/bin/env python3
"""
Run the real HESTIA engine on the exported cycles (results/hestia/cycles.jsonld).
Designed for the container in this folder; needs internet access to api.hestia.earth / www.hestia.earth.

  python3 hestia/run_hestia.py --check-terms          # 1. validate every term id against the live glossary
  python3 hestia/run_hestia.py --limit 50             # 2. small test run
  python3 hestia/run_hestia.py --workers 4            # 3. all cycles

What it does per cycle: HESTIA's standard Cycle configuration (hestia-earth-orchestrator), without
aggregated-data models, without spatial (Earth Engine) models unless --with-geospatial, and with the
drought indices off. Results:
  results/hestia/out/<cycle>.json      full recalculated Cycle (HESTIA JSON-LD)
  results/hestia/hestia_results.csv    one row per cycle: status, gap-filled terms, emissions, GHG totals
  results/hestia/term_check.csv        result of --check-terms
"""
import argparse
import csv
import json
import os
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

os.environ.setdefault("ENABLE_DROUGHT_INDEX", "false")
os.environ.setdefault("LOG_LEVEL", "WARNING")

ROOT = Path(__file__).resolve().parent.parent
HDIR = ROOT / "results" / "hestia"
GWP = {"n2O": 273.0, "ch4": 27.0, "co2": 1.0}  # IPCC AR6 GWP100 (CH4 non-fossil)


def require_api():
    """Fail fast with a clear message if the HESTIA API is not reachable (instead of 'all terms missing')."""
    from hestia_earth.utils.api import download_hestia
    try:
        ok = bool((download_hestia("diesel") or {}).get("@id"))
    except Exception as e:
        ok, err = False, e
    if not ok:
        sys.exit("ERROR: cannot reach the HESTIA API at "
                 f"{os.getenv('API_URL', 'https://api.hestia.earth')} (test term 'diesel' not returned).\n"
                 "Check internet / proxy access to api.hestia.earth and www.hestia.earth from this container.")


def collect_terms(cycles):
    ids = {}
    for c in cycles:
        for blk in ("inputs", "products", "practices"):
            for n in c.get(blk, []):
                ids.setdefault(n["term"]["@id"], blk)
        for m in c["site"].get("measurements", []):
            ids.setdefault(m["term"]["@id"], "site.measurements")
        ids.setdefault(c["site"]["country"]["@id"], "site.country")
    return ids


def check_terms(cycles):
    from hestia_earth.utils.api import download_hestia, search
    rows, bad = [], 0
    for tid, where in sorted(collect_terms(cycles).items()):
        node = download_hestia(tid)
        ok = bool(node and node.get("@id"))
        hint = ""
        if not ok:
            bad += 1
            try:
                words = " ".join(w for w in __import__("re").sub(r"([A-Z])", r" \1", tid).split() if len(w) > 2)
                hits = search({"bool": {"must": [{"match": {"@type": "Term"}}, {"match": {"name": words}}]}}, limit=5)
                hint = "; ".join(f"{h.get('@id')} ({h.get('name')})" for h in hits)
            except Exception as e:  # search syntax may change between API versions
                hint = f"search failed: {e}"
        rows.append({"term_id": tid, "used_in": where, "exists": ok,
                     "name": (node or {}).get("name", ""), "units": (node or {}).get("units", ""),
                     "suggestions_if_missing": hint})
        print(f"  {'OK ' if ok else 'MISSING'}  {tid:<52} {where:<18} {(node or {}).get('units', '')}"
              + (f"\n           try: {hint}" if hint else ""))
    with open(HDIR / "term_check.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(f"\n{len(rows) - bad} terms OK, {bad} missing -> fix them in pipeline/export_hestia.py, re-export, re-check.")
    return bad


def config(with_geo):
    from hestia_earth.models.config import load_config

    def keep(m):
        if isinstance(m, list):
            return [x for x in (keep(y) for y in m) if x]
        return None if (not with_geo and m.get("model") == "geospatialDatabase") else m

    cfg = load_config("Cycle", skip_aggregated_models=True)
    return cfg | {"models": keep(cfg["models"])}


def summarise(cid, before, after):
    added = lambda blk: sorted({n["term"]["@id"] for n in after.get(blk, [])} - {n["term"]["@id"] for n in before.get(blk, [])})
    row = {"cycle_id": cid, "status": "ok", "added_inputs": "|".join(added("inputs")),
           "added_products": "|".join(added("products")), "added_practices": "|".join(added("practices"))}
    tot = {"n2O": 0.0, "ch4": 0.0, "co2": 0.0}
    em = {}
    for e in after.get("emissions", []):
        tid = e["term"]["@id"]
        v = (e.get("value") or [0])[0] or 0.0
        em[tid] = em.get(tid, 0.0) + v
        for gas in tot:
            if tid.startswith(gas + "ToAir"):
                tot[gas] += v
    row.update({f"emission_{k}": round(v, 5) for k, v in sorted(em.items())})
    row["hestia_n2o_kg_ha"] = round(tot["n2O"], 4)
    row["hestia_n2o_kgco2e_ha"] = round(tot["n2O"] * GWP["n2O"], 2)
    row["hestia_ch4_kgco2e_ha"] = round(tot["ch4"] * GWP["ch4"], 2)
    row["hestia_co2_kg_ha"] = round(tot["co2"], 2)
    row["hestia_soc_co2_kg_ha"] = round(em.get("co2ToAirSoilOrganicCarbonStockChange", 0.0), 2)
    row["hestia_ghg_kgco2e_ha"] = round(row["hestia_n2o_kgco2e_ha"] + row["hestia_ch4_kgco2e_ha"] + tot["co2"], 2)
    return row


def run_one(cycle, cfg, outdir):
    from hestia_earth.orchestrator import run
    cid = cycle["@id"].removeprefix("cycle-")
    t0 = time.time()
    try:
        result = run(json.loads(json.dumps(cycle)), cfg)
        (outdir / f"{cid}.json").write_text(json.dumps(result, indent=1, default=str))
        row = summarise(cid, cycle, result)
    except Exception as e:
        row = {"cycle_id": cid, "status": f"error: {type(e).__name__}: {str(e)[:200]}"}
        (outdir / f"{cid}.error.txt").write_text(traceback.format_exc())
    row["seconds"] = round(time.time() - t0, 1)
    return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default=str(HDIR / "cycles.jsonld"))
    ap.add_argument("--check-terms", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--with-geospatial", action="store_true", help="needs hestia-earth-earth-engine + credentials")
    ap.add_argument("--skip-done", action="store_true", help="skip cycles already in out/")
    a = ap.parse_args()
    cycles = json.loads(Path(a.input).read_text())
    if a.limit:
        cycles = cycles[:a.limit]
    print(f"HESTIA API: {os.getenv('API_URL', 'https://api.hestia.earth')}  |  {len(cycles)} cycles")
    require_api()
    if a.check_terms:
        sys.exit(1 if check_terms(cycles) else 0)

    from hestia_earth.models.preload_requests import enable_preload
    enable_preload()
    cfg = config(a.with_geospatial)
    outdir = HDIR / "out"
    outdir.mkdir(parents=True, exist_ok=True)
    if a.skip_done:
        cycles = [c for c in cycles if not (outdir / f"{c['@id'].removeprefix('cycle-')}.json").exists()]
    rows = []
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = [ex.submit(run_one, c, cfg, outdir) for c in cycles]
        for i, f in enumerate(as_completed(futs), 1):
            r = f.result()
            rows.append(r)
            print(f"  [{i}/{len(cycles)}] {r['cycle_id']}: {r['status']} ({r.get('seconds')} s)"
                  + (f"  N2O {r.get('hestia_n2o_kgco2e_ha')} kg CO2e/ha" if r["status"] == "ok" else ""))
    cols = ["cycle_id", "status", "seconds", "hestia_ghg_kgco2e_ha", "hestia_n2o_kgco2e_ha", "hestia_n2o_kg_ha",
            "hestia_ch4_kgco2e_ha", "hestia_co2_kg_ha", "hestia_soc_co2_kg_ha", "added_inputs", "added_products",
            "added_practices"]
    cols += sorted({k for r in rows for k in r} - set(cols))
    with open(HDIR / "hestia_results.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    ok = sum(r["status"] == "ok" for r in rows)
    print(f"\n{ok}/{len(rows)} cycles calculated -> results/hestia/hestia_results.csv"
          f"{'  (see out/*.error.txt for failures)' if ok < len(rows) else ''}")


if __name__ == "__main__":
    main()
