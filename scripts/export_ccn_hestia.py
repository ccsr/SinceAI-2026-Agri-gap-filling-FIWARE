#!/usr/bin/env python3
"""
export_ccn_hestia.py - export the Navigator crop cycles to HESTIA Cycle JSON for the HESTIA container.

One HESTIA Cycle per farm x crop x year. Its Site aggregates the parcels declared for that crop and year:
area-weighted clay content and organic-soil share from the SOIL MAP (not the farmer's self-reported soil,
which is known to be wrong for some peat fields), centroid coordinates, total area.

Completeness (what HESTIA may gap-fill): a block is marked complete only if the source reports it in full.
  FI survey:   fuel, lime, product complete; fertiliser incomplete (no K reported); pesticides incomplete
               (only spray passes, no active ingredient)
  SE Foodhills: only N, diesel and delivered quantity -> fertiliser, lime, pesticides incomplete
  FI not surveyed: everything incomplete, no product (HESTIA never gap-fills yield) - off by default

Cycles with suspected decimal shifts / implausible outliers are left out by default (HESTIA would
compute nonsense from N = 1,028 kg/ha); use --include-flagged to export them anyway.

Term ids marked "check" must be validated: python3 hestia/run_hestia.py --check-terms
Output: results/hestia/cycles.jsonld (+ term_mapping.json, export_log.csv)
"""
import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "source-data"
OUT = ROOT / "results" / "hestia"

TERMS = {  # (term id, termType, status)
    "n": ("inorganicNitrogenFertiliserUnspecifiedKgN", "inorganicFertiliser", "verified"),
    "manure_n": ("cattleSolidManureKgN", "organicFertiliser", "check"),
    "p2o5": ("inorganicPhosphorusFertiliserUnspecifiedKgP2O5", "inorganicFertiliser", "check"),
    "diesel": ("diesel", "fuel", "verified (kg)"),
    "lime": ("agriculturalLimeKgCaCO3", "soilAmendment", "check"),
}
PRODUCT = {"pea": "peaVining", "potato": "potatoTuber", "carrot": "carrotRoot", "spinach": "spinachLeaf",
           "turnip_rape": "turnipRapeSeed"}  # all "check"
MEASUREMENT = {"clay": "clayContent", "organic_share": "histosol"}  # "check": % of area on organic soil
PRACTICE = {"catch": "coverCrop", "irrigated": "irrigatedTypeUnspecified"}  # verified in hestia-earth-models
BASE_COMPLETENESS = {"@type": "Completeness", "animalFeed": True, "animalPopulation": True, "excreta": True,
                     "freshForage": True, "ingredient": True, "liveAnimalInput": True, "material": False,
                     "operation": False, "otherChemical": False, "transport": False, "waste": True,
                     "seed": False, "cropResidue": False, "water": True}


def f(v):
    try:
        return None if v in ("", None) else float(str(v).replace(",", "."))
    except ValueError:
        return None


def read(name, sep=","):
    with open(SRC / name, encoding="utf-8") as fh:
        return list(csv.DictReader(fh, delimiter=sep))


def inp(key, value, factor=1.0):
    tid, ttype, _ = TERMS[key]
    return {"@type": "Input", "term": {"@type": "Term", "@id": tid, "termType": ttype}, "value": [round(value * factor, 4)]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--include-flagged", action="store_true")
    ap.add_argument("--include-unreported", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--source", choices=["all", "fi", "se"], default="all")
    a = ap.parse_args()
    parcels = {p["parcel_id"]: p for p in read("parcels.csv")}
    farms = {r["farm_id"]: r for r in read("farm_registry.csv")}
    fcy = defaultdict(list)
    for h in read("parcel_crop_history_and_plan.csv"):
        if h["status"] == "declared":
            fcy[(h["farm_id"], h["crop"], int(h["year"]))].append(h["parcel_id"])
    flagged = {}
    for e in json.load(open(ROOT / "entities" / "ccn" / "06_crop_cycles.json")):
        fl = [x for x in e.get("dataQualityFlags", {}).get("value", [])
              if x.startswith(("decimal_shift", "outlier_implausible"))]
        if fl:
            flagged[e["id"].split(":ccn:")[1]] = fl

    def site(farm, crop, year):
        pids = fcy.get((farm, crop, year), [])
        fm = farms.get(farm, {})
        if pids:
            area = sum(float(parcels[p]["area_ha"]) for p in pids)
            w = lambda p: float(parcels[p]["area_ha"]) / area
            lat = sum(float(parcels[p]["lat"]) * w(p) for p in pids)
            lon = sum(float(parcels[p]["lon"]) * w(p) for p in pids)
            clay = sum(float(parcels[p]["clay_pct_soilmap"] or 0) * w(p) for p in pids)
            org = sum(w(p) for p in pids if parcels[p]["soil_class_soilmap"] == "organic")
            meas = [{"@type": "Measurement", "term": {"@type": "Term", "@id": MEASUREMENT["clay"]},
                     "value": [round(clay, 1)], "depthUpper": 0, "depthLower": 30, "methodClassification": "geospatial dataset", "methodClassificationDescription": "Soil map (~88 % accurate), area-weighted over the declared parcels"},
                    {"@type": "Measurement", "term": {"@type": "Term", "@id": MEASUREMENT["organic_share"]},
                     "value": [round(org * 100, 1)], "methodClassification": "geospatial dataset", "methodClassificationDescription": "Soil map (~88 % accurate), area-weighted over the declared parcels"}]
        else:
            area, lat, lon, meas = None, f(fm.get("lat")), f(fm.get("lon")), []
        s = {"@type": "Site", "@id": f"site-{farm}-{crop}-{year}", "siteType": "cropland",
             "country": {"@type": "Term", "@id": "GADM-FIN" if farm.startswith("F") else "GADM-SWE"},
             "latitude": round(lat, 5) if lat else None, "longitude": round(lon, 5) if lon else None,
             "measurements": meas, "dataPrivate": True}
        if area:
            s["area"] = round(area, 2)
        return s

    cycles, log = [], []

    def add(key, farm, crop, year, inputs, practices, product_kg, completeness, note):
        if key in flagged and not a.include_flagged:
            log.append({"cycle": key, "exported": False, "reason": ";".join(flagged[key])})
            return
        c = {"@type": "Cycle", "@id": f"cycle-{key}", "name": f"{crop} {farm} {year} (synthetic)",
             "startDate": f"{year}-04-15", "endDate": f"{year}-09-30", "functionalUnit": "1 ha",
             "site": site(farm, crop, year), "inputs": inputs, "practices": practices,
             "completeness": dict(BASE_COMPLETENESS, **completeness), "dataPrivate": True,
             "description": f"Apetit Climate & Nature Navigator synthetic dataset (Since AI ry 2026, CC BY 4.0). {note}"}
        if product_kg is not None:
            c["products"] = [{"@type": "Product", "term": {"@type": "Term", "@id": PRODUCT[crop]},
                              "value": [round(product_kg, 1)], "primary": True}]
        cycles.append(c)
        log.append({"cycle": key, "exported": True, "reason": note})

    seen = set()
    if a.source in ("all", "fi"):
        for s in read("farm_survey_fi.csv"):
            farm, crop, year = s["farm_id"], s["crop"], int(s["year"])
            key = f"{farm}-{crop}-{year}"
            seen.add((farm, crop, year))
            inputs = []
            for k, col, fac in [("n", "n_mineral_kg_ha", 1), ("manure_n", "n_manure_kg_ha", 1),
                                ("p2o5", "p_kg_ha", 2.291), ("diesel", "diesel_l_ha", 0.835), ("lime", "lime_t_ha", 1000)]:
                v = f(s[col])
                if v:
                    inputs.append(inp(k, v, fac))
            pr = []
            if f(s["catch_crop_share"]):
                pr.append({"@type": "Practice", "term": {"@type": "Term", "@id": PRACTICE["catch"]},
                           "value": [round(f(s["catch_crop_share"]) * 100, 1)]})
            if s["irrigated"] == "True":
                pr.append({"@type": "Practice", "term": {"@type": "Term", "@id": PRACTICE["irrigated"]}, "value": [100]})
            add(key, farm, crop, year, inputs, pr, f(s["yield_t_ha"]) * 1000,
                {"fertiliser": False, "electricityFuel": True, "soilAmendment": True, "pesticideVeterinaryDrug": False,
                 "product": True, "water": s["irrigated"] != "True"},
                f"FI survey {s['survey_id']}; fertiliser product {s['fertiliser_product']}; spray passes {s['sprays']}")
        if a.include_unreported:
            for (farm, crop, year), pids in fcy.items():
                if farm.startswith("F") and crop in PRODUCT and (farm, crop, year) not in seen and year <= 2026:
                    add(f"{farm}-{crop}-{year}", farm, crop, year, [], [], None,
                        {"fertiliser": False, "electricityFuel": False, "soilAmendment": False,
                         "pesticideVeterinaryDrug": False, "product": False, "water": False},
                        "FI contract crop declared, survey not returned")
    if a.source in ("all", "se"):
        for s in read("foodhills_odlingsdata_se.csv", sep=";"):
            farm, year = s["odlar_id"], int(s["skördeår"])
            area, deliv = f(s["areal_ha"]), f(s["levererad_mängd_ton"])
            inputs = [inp("n", f(s["kvävegiva_kg_ha"]))] if f(s["kvävegiva_kg_ha"]) else []
            if f(s["diesel_l_ha"]):
                inputs.append(inp("diesel", f(s["diesel_l_ha"]), 0.835))
            pr = [{"@type": "Practice", "term": {"@type": "Term", "@id": PRACTICE["irrigated"]}, "value": [100]}] \
                if s["bevattnad"].strip().lower() == "ja" else []
            add(f"{farm}-pea-{year}", farm, "pea", year, inputs, pr,
                deliv / area * 1000 if area and deliv is not None else None,
                {"fertiliser": False, "electricityFuel": True, "soilAmendment": False, "pesticideVeterinaryDrug": False,
                 "product": deliv is not None, "water": s["bevattnad"].strip().lower() != "ja"},
                "SE Foodhills grower file (N, diesel, delivered quantity only)")
    if a.limit:
        cycles = cycles[:a.limit]
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "cycles.jsonld").write_text(json.dumps(cycles, indent=1, ensure_ascii=False))
    (OUT / "term_mapping.json").write_text(json.dumps({"inputs": TERMS, "products": PRODUCT,
                                                       "measurements": MEASUREMENT, "practices": PRACTICE}, indent=1))
    with open(OUT / "export_log.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["cycle", "exported", "reason"])
        w.writeheader()
        w.writerows(log)
    skipped = sum(1 for l in log if not l["exported"])
    print(f"Exported {len(cycles)} HESTIA cycles -> results/hestia/cycles.jsonld "
          f"({skipped} flagged cycles left out; see export_log.csv)")


if __name__ == "__main__":
    main()
