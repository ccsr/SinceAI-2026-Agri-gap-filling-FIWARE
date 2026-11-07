#!/usr/bin/env python3
"""
convert_ccn_to_ngsild.py
========================
Converts the farming + nature part of the "Apetit Climate & Nature Navigator 2026" package
(Since AI ry, synthetic twin, CC BY 4.0) into NGSI-LD entities for tenant `apetit_ccn`,
using the data model in data-model/DATA_MODEL.md (Smart Data Models Agrifood + alca: extensions).

Values are loaded AS REPORTED (raw). Planted / suspicious data problems are not corrected; they are
flagged in `dataQualityFlags` so an expert (or a gap-filling service) can act on them.

Entities (entities/ccn/):
  AgriSoil (soil-map classes), AgriCrop, AgriFarm (680), AgriParcel (14,213 polygons + crop history),
  AgriCropCycle  farm x crop x year:  FI survey (reported) | FI contract crop declared but NOT surveyed |
                 SE Foodhills grower file,
  AgriFootprintResult  'current calculator' footprints (mineral soil assumed),
  Catchment (42, with monitored loads).
Standard library only.  Usage: python3 scripts/convert_ccn_to_ngsild.py
"""
import csv
import json
import statistics
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "source-data"
OUT = ROOT / "entities" / "ccn"
NS = "urn:ngsi-ld"
PROVIDER = "Apetit Climate & Nature Navigator synthetic dataset, Since AI ry 2026 (CC BY 4.0)"
CONTRACT_CROPS = {"pea", "potato", "carrot", "spinach", "turnip_rape"}
SOILMAP = {  # soil-map class -> (USDA texture for SDM soilTextureType, organic soil)
    "clay": ("Clay", False), "silt": ("Silt loam", False), "fine_sand": ("Loamy sands", False),
    "sand": ("Sands", False), "organic": (None, True),
}
CROP_FI = {"pea": "herne", "potato": "peruna", "carrot": "porkkana", "spinach": "pinaatti",
           "turnip_rape": "rypsi/rapsi", "barley": "ohra", "wheat": "vehnä", "oats": "kaura",
           "grass_ley": "nurmi", "fallow": "kesanto", "rapeseed": "rapsi"}
INPUT_ATTRS = ["nitrogenMineralApplied", "manureNitrogenApplied", "phosphorusMineralApplied", "dieselConsumption",
               "limeApplied", "pesticideApplications", "catchCropShare", "harvestedYield"]


def P(v, **sub):
    a = {"type": "Property", "value": v}
    a.update({k: x for k, x in sub.items() if x is not None})
    return a


def R(o):
    return {"type": "Relationship", "object": o}


def rid(t, k):
    return f"{NS}:{t}:ccn:{k}"


def ent(i, t, **a):
    e = {"id": i, "type": t}
    e.update({k: v for k, v in a.items() if v is not None})
    return e


def f(v):
    try:
        return None if v in ("", None) else float(str(v).replace(",", "."))
    except ValueError:
        return None


def read(name, sep=","):
    with open(SRC / name, encoding="utf-8") as fh:
        return list(csv.DictReader(fh, delimiter=sep))


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    farms = read("farm_registry.csv")
    parcels = {p["parcel_id"]: p for p in read("parcels.csv")}
    geo = {ft["properties"]["parcel_id"]: ft["geometry"] for ft in json.load(open(SRC / "parcels.geojson"))["features"]}
    hist = read("parcel_crop_history_and_plan.csv")
    survey = read("farm_survey_fi.csv")
    se = read("foodhills_odlingsdata_se.csv", sep=";")
    calc = read("current_calculator_footprints.csv")
    catch = read("catchments.csv")
    loads = read("monitored_catchment_loads.csv")
    cgeo = {ft["properties"].get("catchment_id"): ft["geometry"] for ft in json.load(open(SRC / "catchments.geojson"))["features"]}

    # ---- crop history per parcel; declared contract-crop parcels per farm x crop x year
    hist_by_parcel = defaultdict(list)
    fcy = defaultdict(list)  # (farm, crop, year) -> parcel ids (declared)
    for h in hist:
        hist_by_parcel[h["parcel_id"]].append({"year": int(h["year"]), "crop": h["crop"],
                                               "catchCrop": h["catch_crop"] == "True", "status": h["status"]})
        if h["status"] == "declared":
            fcy[(h["farm_id"], h["crop"], int(h["year"]))].append(h["parcel_id"])
    parcels_by_farm = defaultdict(list)
    for pid, p in parcels.items():
        parcels_by_farm[p["farm_id"]].append(pid)

    def parcel_stats(pids):
        area = sum(float(parcels[p]["area_ha"]) for p in pids)
        org = sum(float(parcels[p]["area_ha"]) for p in pids if parcels[p]["soil_class_soilmap"] == "organic")
        return area, (org / area if area else None)

    # ---- reference entities
    soils = [ent(f"{NS}:AgriSoil:soilmap-{k}", "AgriSoil", name=P(k.replace("_", " ")), organicSoil=P(org),
                 soilTextureType=P(tex) if tex else None, soilDataSource=P("soil map (~88 % accurate)"))
             for k, (tex, org) in SOILMAP.items()]
    crop_names = sorted({h["crop"] for h in hist} | {s["crop"] for s in survey} | {"pea"})
    crops = [ent(f"{NS}:AgriCrop:{c.replace('_', '-')}", "AgriCrop", name=P(c),
                 alternateName=P(CROP_FI.get(c, c)), contractCrop=P(c in CONTRACT_CROPS)) for c in crop_names]
    crop_ref = lambda c: R(f"{NS}:AgriCrop:{c.replace('_', '-')}")

    farm_ents = []
    for fm in farms:
        farm_ents.append(ent(
            rid("AgriFarm", fm["farm_id"]), "AgriFarm", name=P(f"Contract farm {fm['farm_id']} (synthetic)"),
            location={"type": "GeoProperty", "value": {"type": "Point", "coordinates": [f(fm["lon"]), f(fm["lat"])]}},
            address=P({"addressCountry": fm["country"], "addressRegion": fm["region"]}),
            farmSegment=P(fm["segment"]), organicCertified=P(fm["organic_certified"] == "True"),
            livestock=P(fm["livestock"] == "True"), contractSince=P(int(fm["joined_year"])),
            contractCrops=P(fm["contract_crops"].split(";")),
            hasAgriParcel=R([rid("AgriParcel", p) for p in sorted(parcels_by_farm[fm["farm_id"]])])
            if parcels_by_farm[fm["farm_id"]] else None,
            dataProvider=P(PROVIDER)))

    parcel_ents = []
    for pid, p in parcels.items():
        tex, org = SOILMAP[p["soil_class_soilmap"]]
        ptest = f(p["p_test_mgl_latest"])
        parcel_ents.append(ent(
            rid("AgriParcel", pid), "AgriParcel", name=P(f"Parcel {pid} (synthetic)"),
            location={"type": "GeoProperty", "value": geo[pid]} if pid in geo else
            {"type": "GeoProperty", "value": {"type": "Point", "coordinates": [f(p["lon"]), f(p["lat"])]}},
            area=P(round(float(p["area_ha"]) * 10000), unitCode="MTK"), category=P("arable"),
            belongsTo=R(rid("AgriFarm", p["farm_id"])), hasAgriSoil=R(f"{NS}:AgriSoil:soilmap-{p['soil_class_soilmap']}"),
            soilTextureType=P(tex) if tex else None, organicSoil=P(org, soilDataSource="soil map (~88 % accurate)"),
            clayContent=P(f(p["clay_pct_soilmap"]), unitCode="P1"), slope=P(f(p["slope_pct"]), unitCode="P1"),
            distanceToWater=P(f(p["dist_to_water_m"]), unitCode="MTR"), drainageType=P(p["drainage"]),
            bufferStrip=P(p["buffer_strip"] == "True"), refCatchment=R(rid("Catchment", p["catchment_id"])),
            soilPhosphorus=P(ptest, unitText="mg/l (acid ammonium acetate)", dataOrigin="reported") if ptest is not None else None,
            missingAttributes=P(["soilPhosphorus"]) if ptest is None else None,
            cropHistory=P(sorted(hist_by_parcel[pid], key=lambda x: x["year"])),
            dataProvider=P(PROVIDER)))

    # ---- crop cycles
    med = defaultdict(dict)
    for c in CONTRACT_CROPS:
        rows = [s for s in survey if s["crop"] == c]
        for col in ["yield_t_ha", "n_mineral_kg_ha", "diesel_l_ha"]:
            vals = [f(s[col]) for s in rows if f(s[col]) is not None]
            med[c][col] = statistics.median(vals) if vals else None

    def flags(crop, n_min, yld, diesel, area_rep, area_hist, soil_rep, org_share):
        fl = []
        m = med.get(crop, {})
        mn = m.get("n_mineral_kg_ha")
        if n_min is not None and mn:
            r = n_min / max(mn, 20)
            if n_min > 0 and (r > 6 or r < 0.2):  # x10 or /10 typing slip; 0 is legitimate (organic, peas)
                fl.append("decimal_shift_suspected:nitrogenMineralApplied")
            elif n_min > 400:
                fl.append("outlier_implausible:nitrogenMineralApplied")
        if yld is not None and m.get("yield_t_ha") and (yld > 3 * m["yield_t_ha"] or yld < 0.15 * m["yield_t_ha"]):
            fl.append("outlier_implausible:harvestedYield")
        if diesel is not None and m.get("diesel_l_ha") and diesel > 2.5 * m["diesel_l_ha"]:
            fl.append("outlier_implausible:dieselConsumption")
        if area_rep and area_hist and abs(area_rep - area_hist) / area_hist > 0.3:
            fl.append("area_mismatch:cropArea_vs_parcel_declarations")
        if soil_rep == "Mineral soil" and org_share is not None and org_share >= 0.3:
            fl.append("soil_misreport_suspected:soil_map_organic_share>=30%")
        return fl

    cycles, seen = [], set()
    for s in survey:
        key = (s["farm_id"], s["crop"], int(s["year"]))
        pids = fcy.get(key, [])
        area_hist, org_share = parcel_stats(pids) if pids else (None, None)
        n_min, yld, dsl, area = f(s["n_mineral_kg_ha"]), f(s["yield_t_ha"]), f(s["diesel_l_ha"]), f(s["area_ha"])
        fl = flags(s["crop"], n_min, yld, dsl, area, area_hist, s["soil_type_reported"], org_share)
        rep = lambda v, unit: P(v, unitText=unit, dataOrigin="reported")
        cycles.append(ent(
            rid("AgriCropCycle", f"{s['farm_id']}-{s['crop']}-{s['year']}"), "AgriCropCycle",
            name=P(f"{s['crop']} {s['farm_id']} {s['year']}"), season=P(int(s["year"])),
            hasAgriFarm=R(rid("AgriFarm", s["farm_id"])), hasAgriCrop=crop_ref(s["crop"]),
            hasAgriParcel=R([rid("AgriParcel", p) for p in pids]) if pids else None,
            cropArea=P(area, unitText="ha", dataOrigin="reported"),
            declaredParcelArea=P(round(area_hist, 2), unitText="ha") if area_hist else None,
            reportedSoilType=P(s["soil_type_reported"]),
            organicSoilShare=P(round(org_share, 3), unitCode="C62", soilDataSource="soil map") if org_share is not None else None,
            fertiliserProduct=P(s["fertiliser_product"]),
            nitrogenMineralApplied=rep(n_min, "kg N/ha"), manureNitrogenApplied=rep(f(s["n_manure_kg_ha"]), "kg N/ha"),
            phosphorusMineralApplied=rep(f(s["p_kg_ha"]), "kg P/ha"), dieselConsumption=rep(dsl, "l/ha"),
            limeApplied=rep(f(s["lime_t_ha"]), "t/ha"), pesticideApplications=rep(f(s["sprays"]), "passes"),
            irrigated=P(s["irrigated"] == "True"), catchCropShare=rep(f(s["catch_crop_share"]), "share of area"),
            harvestedYield=rep(yld, "t/ha"), contractCrop=P(True),
            reportingStatus=P("reported"), dataSource=P("farm_survey_fi.csv " + s["survey_id"]),
            dataQualityFlags=P(fl) if fl else None, dataProvider=P(PROVIDER)))
        seen.add(key)
    # FI contract crops declared on parcels but not surveyed (non-response)
    for (farm, crop, year), pids in sorted(fcy.items()):
        if not farm.startswith("F") or crop not in CONTRACT_CROPS or (farm, crop, year) in seen or year > 2026:
            continue
        area_hist, org_share = parcel_stats(pids)
        cycles.append(ent(
            rid("AgriCropCycle", f"{farm}-{crop}-{year}"), "AgriCropCycle", name=P(f"{crop} {farm} {year}"),
            season=P(year), hasAgriFarm=R(rid("AgriFarm", farm)), hasAgriCrop=crop_ref(crop),
            hasAgriParcel=R([rid("AgriParcel", p) for p in pids]), cropArea=P(round(area_hist, 2), unitText="ha",
                                                                                  dataOrigin="parcel declarations"),
            organicSoilShare=P(round(org_share, 3), unitCode="C62", soilDataSource="soil map"),
            contractCrop=P(True), reportingStatus=P("not reported (survey non-response)"),
            missingAttributes=P(INPUT_ATTRS + ["reportedSoilType", "fertiliserProduct"]),
            dataSource=P("parcel_crop_history_and_plan.csv (declared)"), dataProvider=P(PROVIDER)))
    # SE Foodhills grower file
    for s in se:
        farm, year = s["odlar_id"], int(s["skördeår"])
        area, deliv = f(s["areal_ha"]), f(s["levererad_mängd_ton"])
        yld = round(deliv / area, 3) if area and deliv is not None else None
        pids = fcy.get((farm, "pea", year), [])
        area_hist, org_share = parcel_stats(pids) if pids else (None, None)
        n_min, dsl = f(s["kvävegiva_kg_ha"]), f(s["diesel_l_ha"])
        fl = flags("pea", n_min, yld, dsl, area, area_hist, None, None)
        cycles.append(ent(
            rid("AgriCropCycle", f"{farm}-pea-{year}"), "AgriCropCycle", name=P(f"pea {farm} {year}"),
            season=P(year), hasAgriFarm=R(rid("AgriFarm", farm)), hasAgriCrop=crop_ref("pea"),
            hasAgriParcel=R([rid("AgriParcel", p) for p in pids]) if pids else None,
            cropArea=P(area, unitText="ha", dataOrigin="reported"),
            declaredParcelArea=P(round(area_hist, 2), unitText="ha") if area_hist else None,
            organicSoilShare=P(round(org_share, 3), unitCode="C62", soilDataSource="soil map") if org_share is not None else None,
            deliveredQuantity=P(deliv, unitText="t", dataOrigin="reported"),
            harvestedYield=P(yld, unitText="t/ha", dataOrigin="derived (delivered / area)") if yld is not None else None,
            nitrogenMineralApplied=P(n_min, unitText="kg N/ha", dataOrigin="reported"),
            dieselConsumption=P(dsl, unitText="l/ha", dataOrigin="reported"),
            irrigated=P(s["bevattnad"].strip().lower() == "ja"), contractCrop=P(True),
            reportingStatus=P("reported (Foodhills grower file, partial)"),
            missingAttributes=P(["manureNitrogenApplied", "phosphorusMineralApplied", "limeApplied",
                                 "pesticideApplications", "catchCropShare", "reportedSoilType", "fertiliserProduct"]),
            dataSource=P("foodhills_odlingsdata_se.csv"), dataQualityFlags=P(fl) if fl else None,
            dataProvider=P(PROVIDER)))

    # ---- current-calculator footprints
    fps = []
    for c in calc:
        key = f"{c['farm_id']}-{c['crop']}-{c['year']}"
        v = f(c["carbon_footprint_kgco2e_per_kg"])
        fps.append(ent(
            rid("AgriFootprintResult", key + "-currentcalc"), "AgriFootprintResult",
            refAgriCropCycle=R(rid("AgriCropCycle", key)), hasAgriCrop=crop_ref(c["crop"]), season=P(int(c["year"])),
            systemBoundary=P("cradle-to-farm-gate"), calculationMethod=P(c["calculator_note"]),
            carbonFootprint=P(round(v * 1000, 1) if v is not None else None, unitText="kg CO2e/t",
                              functionalUnit="1 t harvested product", dataOrigin="reported"),
            dataOrigin=P("reported (current calculator; mineral soil assumed, organic soils and LUC excluded)"),
            dataProvider=P(PROVIDER)))

    # ---- catchments
    lb = defaultdict(list)
    for l in loads:
        lb[l["catchment_id"]].append({"year": int(l["year"]), "totalPhosphorusKg": f(l["total_p_load_kg"]),
                                      "totalNitrogenKg": f(l["total_n_load_kg"]), "method": l["method"]})
    cats = []
    for c in catch:
        g = cgeo.get(c["catchment_id"])
        cats.append(ent(
            rid("Catchment", c["catchment_id"]), "Catchment", name=P(f"Catchment {c['catchment_id']} (synthetic)"),
            location={"type": "GeoProperty", "value": g} if g else None, address=P({"addressRegion": c["region"]}),
            receivingWaterType=P(c["receiving_water_type"]), ecologicalStatus=P(c["ecological_status_class"]),
            nutrientRetention=P(f(c["nutrient_retention_frac_estimate"]), unitCode="C62"),
            catchmentArea=P(f(c["catchment_area_km2"]), unitCode="KMK"),
            fieldAreaTotal=P(f(c["field_area_total_ha"]), unitText="ha"),
            phosphorusSensitivity=P(f(c["p_sensitivity"])), monitoringStation=P(c["monitoring_station"] == "True"),
            monitoredLoads=P(sorted(lb[c["catchment_id"]], key=lambda x: x["year"])) if lb[c["catchment_id"]] else None,
            dataProvider=P(PROVIDER)))

    files = [("01_agri_soils.json", soils), ("02_agri_crops.json", crops), ("03_catchments.json", cats),
             ("04_agri_farms.json", farm_ents), ("05_agri_parcels.json", parcel_ents),
             ("06_crop_cycles.json", cycles), ("07_footprints_current_calculator.json", fps)]
    manifest = []
    for name, items in files:
        (OUT / name).write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
        manifest.append({"file": name, "type": items[0]["type"], "count": len(items)})
        print(f"ccn/{name}: {len(items)} {items[0]['type']}")
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=1))
    st = defaultdict(int)
    for c in cycles:
        st[c["reportingStatus"]["value"]] += 1
    fl = defaultdict(int)
    for c in cycles:
        for x in c.get("dataQualityFlags", {}).get("value", []):
            fl[x.split(":")[0]] += 1
    print("crop cycles by status:", dict(st))
    print("quality flags:", dict(fl))


if __name__ == "__main__":
    main()
