#!/usr/bin/env python3
"""
hestia_writeback.py - put the results of the HESTIA run (results/hestia/hestia_results.csv from
hestia/run_hestia.py) into tenant `apetit_ccn`:

  AgriFootprintResult  <cycle>-hestia   dataOrigin "hestia": GHG per ha (AR6), N2O, CH4, CO2, soil-carbon CO2,
                                         and kg CO2e per t when a yield is known; HESTIA version in calculationMethod
  AgriCropCycle        + hestiaStatus, hestiaAddedTerms (what HESTIA gap-filled: inputs / products / practices)

The reported values in the tenant are never overwritten. Next to them sit the current-calculator footprint
(<cycle>-currentcalc) and the HESTIA one (<cycle>-hestia), so the two can be compared per cycle.

  python3 scripts/hestia_writeback.py [--broker ...] [--tenant apetit_ccn] [--dry-run]
"""
import argparse
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import load_to_broker as L  # noqa: E402

NS = "urn:ngsi-ld"
METHOD = "HESTIA engine (hestia-earth-models 0.93.4, orchestrator 0.6.20; Cycle config, no aggregated or spatial models)"


def P(v, **s):
    a = {"type": "Property", "value": v}
    a.update({k: x for k, x in s.items() if x is not None})
    return a


def num(v):
    try:
        return None if v in ("", None) else round(float(v), 3)
    except ValueError:
        return None


def build():
    res = list(csv.DictReader(open(ROOT / "results" / "hestia" / "hestia_results.csv", encoding="utf-8")))
    cyc = {json.loads(json.dumps(c))["@id"].removeprefix("cycle-"): c
           for c in json.load(open(ROOT / "results" / "hestia" / "cycles.jsonld"))}
    fps, updates = [], []
    for r in res:
        key = r["cycle_id"]
        cid = f"{NS}:AgriCropCycle:ccn:{key}"
        added = [x for col in ("added_inputs", "added_products", "added_practices")
                 for x in (r.get(col) or "").split("|") if x]
        updates.append({"id": cid, "type": "AgriCropCycle", "hestiaStatus": P(r["status"]),
                        "hestiaAddedTerms": P(added) if added else P(["none"])})
        if r["status"] != "ok":
            continue
        crop = key.split("-")[1]
        ghg = num(r.get("hestia_ghg_kgco2e_ha"))
        prod = (cyc.get(key, {}).get("products") or [{}])[0].get("value", [None])[0]
        fp = {"id": f"{NS}:AgriFootprintResult:ccn:{key}-hestia", "type": "AgriFootprintResult",
              "refAgriCropCycle": {"type": "Relationship", "object": cid},
              "hasAgriCrop": {"type": "Relationship", "object": f"{NS}:AgriCrop:{crop.replace('_', '-')}"},
              "season": P(int(key.split("-")[-1])), "systemBoundary": P("cradle-to-farm-gate (HESTIA Cycle scope)"),
              "calculationMethod": P(METHOD), "characterisation": P("IPCC AR6 GWP100 (N2O 273, CH4 27)"),
              "ghgEmissionsPerArea": P(ghg, unitText="kg CO2e/ha", dataOrigin="hestia",
                                       description="sum of N2O, CH4 and CO2 emissions returned by HESTIA"),
              "emissionBreakdown": P({"n2o": num(r.get("hestia_n2o_kgco2e_ha")), "ch4": num(r.get("hestia_ch4_kgco2e_ha")),
                                      "co2": num(r.get("hestia_co2_kg_ha")), "soilCarbonCO2": num(r.get("hestia_soc_co2_kg_ha"))},
                                     unitText="kg CO2e/ha"),
              "dataOrigin": P("hestia")}
        if ghg is not None and prod:
            fp["carbonFootprint"] = P(round(ghg / (float(prod) / 1000), 1), unitText="kg CO2e/t",
                                      functionalUnit="1 t harvested product", dataOrigin="hestia")
        fps.append(fp)
    return updates, fps


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--broker", default="http://localhost:1026")
    ap.add_argument("--tenant", default="apetit_ccn")
    ap.add_argument("--batch-size", type=int, default=50)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if not (ROOT / "results" / "hestia" / "hestia_results.csv").exists():
        sys.exit("No results/hestia/hestia_results.csv yet - run the HESTIA container first (see README).")
    updates, fps = build()
    custom = json.loads((ROOT / "data-model" / "context.jsonld").read_text())["@context"]
    ctx = [L.CORE, custom]
    broker = a.broker.rstrip("/")
    if not a.dry_run:
        L.wait_for_broker(broker)
    for label, op, ents in [("AgriCropCycle (hestiaStatus, hestiaAddedTerms)", "update", updates),
                            ("AgriFootprintResult (dataOrigin hestia)", "upsert", fps)]:
        ok = err = 0
        for i in range(0, len(ents), a.batch_size):
            chunk = [dict(e, **{"@context": ctx}) for e in ents[i:i + a.batch_size]]
            if a.dry_run:
                ok += len(chunk)
                continue
            st, body, _ = L.request("POST", f"{broker}/ngsi-ld/v1/entityOperations/{op}", a.tenant, chunk)
            if st in (200, 201, 204):
                ok += len(chunk)
            else:
                n_err = len(json.loads(body or "{}").get("errors", [])) if st == 207 else len(chunk)
                ok += len(chunk) - n_err
                err += n_err
                if err <= len(chunk):
                    print(f"    HTTP {st}: {body[:300]}")
        print(f"  {label:<48} {len(ents):>5}  ✓ {ok}  ✗ {err}")
    print("Done." + (" (dry run)" if a.dry_run else ""))


if __name__ == "__main__":
    main()
