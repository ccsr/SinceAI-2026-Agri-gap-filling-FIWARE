#!/usr/bin/env python3
"""
gapfill_ccn.py - layered gap filling for the Apetit Climate & Nature Navigator crop cycles, read from and
written back to an NGSI-LD tenant (default apetit_ccn_post_ml). Runs in the gapfill container (see README).

Layers, most reliable first. Every filled or corrected value says how it was obtained:
  1. derived    soil type from the soil map; yield from cleaned ERP deliveries (FI contract farms 2021-2025)
  2. corrected  flagged values: decimal-shifted N /10, implausible yield -> ERP or model, misreported soil type
                -> soil map, area mismatch -> declared parcel area. The reported value is kept (reportedValue).
  3. imputed    FI non-response cycles: farm history + gradient boosting, method chosen per attribute by
                5-fold cross-validation on the clean FI survey cycles
  4. transfer   SE Foodhills peas (7 attributes): model trained on FI pea cycles using the SE values that are
                known (N, diesel, yield, soil); validated by grouped cross-validation on FI peas. Low confidence.
Ranges (p10-p90) come from the cross-validated residuals of the chosen method.

  python3 gapfill/gapfill_ccn.py --dry-run          # fill + validate + Excel, no broker writes
  python3 gapfill/gapfill_ccn.py                    # ... and update AgriCropCycle entities in the tenant
"""
import argparse
import datetime as dt
import json
import sys
import urllib.parse
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.model_selection import GroupKFold, KFold

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import load_to_broker as L  # noqa: E402

VERSION = "gapfill-ccn 1.0"
OUT = ROOT / "results" / "gapfill"
SEED = 42
TARGETS = {  # attribute: (unit, lower, upper)
    "nitrogenMineralApplied": ("kg N/ha", 0, None),
    "manureNitrogenApplied": ("kg N/ha", 0, None),
    "phosphorusMineralApplied": ("kg P/ha", 0, None),
    "dieselConsumption": ("l/ha", 0, None),
    "limeApplied": ("t/ha", 0, None),
    "pesticideApplications": ("passes", 0, None),
    "catchCropShare": ("0-1", 0, 1),
    "harvestedYield": ("t/ha", 0, None),
}
TEXT = ["reportedSoilType", "fertiliserProduct"]
MATERIAL_CROP = {"M100": "turnip-rape", "M110": "pea", "M111": "potato", "M112": "carrot", "M113": "spinach"}
NUM_FEATS = ["season", "area", "nParcels", "clay", "organicShare", "slope", "distWater", "soilP", "bufferShare",
             "irrigated", "organicCertified", "livestock"]
CAT_FEATS = ["crop", "region", "segment", "drainage"]
SE_FEATS = ["season", "area", "nParcels", "clay", "organicShare", "slope", "distWater", "soilP", "bufferShare",
            "irrigated", "nitrogenMineralApplied", "dieselConsumption", "harvestedYield"]


# ── broker I/O ───────────────────────────────────────────────────────────────
def context_maps():
    ctx = json.loads((ROOT / "data-model" / "context.jsonld").read_text())["@context"]
    prefixes = {k: v for k, v in ctx.items() if isinstance(v, str) and v.endswith(("/", "#"))}
    rev = {}
    for term, iri in ctx.items():
        if isinstance(iri, str) and ":" in iri:
            p, rest = iri.split(":", 1)
            rev[prefixes[p] + rest if p in prefixes else iri] = term
    return ctx, rev


def short(key, rev):
    if key in ("id", "type", "@context"):
        return key
    return rev.get(key, key.rsplit("/", 1)[-1].rsplit("#", 1)[-1])


def norm(obj, rev):
    if isinstance(obj, dict):
        return {short(k, rev): norm(v, rev) for k, v in obj.items() if k != "@context"}
    if isinstance(obj, list):
        return [norm(x, rev) for x in obj]
    return obj


def read_type(broker, tenant, tname, rev):
    iri = urllib.parse.quote(L.TYPE_IRI.get(tname, L.ALCA + tname), safe="")
    out, seen, off = [], set(), 0
    while True:
        st, body, _ = L.request("GET", f"{broker}/ngsi-ld/v1/entities?type={iri}&limit=100&offset={off}", tenant)
        if st != 200:
            sys.exit(f"ERROR reading {tname} from '{tenant}': HTTP {st}: {body[:300]}")
        page = [e for e in json.loads(body or "[]") if e["id"] not in seen]
        if not page:
            break
        seen.update(e["id"] for e in page)
        out += [norm(e, rev) for e in page]
        off += 100
    print(f"  read {tname:<14} {len(out):>6}      ")
    return out


def val(e, k):
    a = e.get(k)
    if isinstance(a, dict):
        return a.get("value", a.get("object"))
    return a


def orig(e, k):
    """Value as originally loaded: a value written by an earlier gap-fill run is ignored (its reportedValue,
    if any, is the original), so re-running the fill is idempotent."""
    a = e.get(k)
    if isinstance(a, dict) and "gapFillVersion" in a:
        rv = a.get("reportedValue")
        return rv.get("value") if isinstance(rv, dict) else rv  # brokers store sub-attributes as Properties
    return val(e, k)


def as_list(x):
    """JSON-LD compaction returns a one-item array as a plain value; always give a list."""
    if x is None:
        return []
    return x if isinstance(x, list) else [x]


def tail(u):
    return u.split(":")[-1] if isinstance(u, str) else u


# ── feature table ────────────────────────────────────────────────────────────
def build_table(cycles, parcels, farms):
    P = {p["id"]: p for p in parcels}
    F = {f["id"]: f for f in farms}
    rows = []
    for e in cycles:
        f = F.get(val(e, "hasAgriFarm"), {})
        pids = as_list(val(e, "hasAgriParcel"))
        ps = [P[i] for i in pids if i in P]
        A = sum(val(p, "area") or 0 for p in ps) or np.nan
        w = lambda k: sum((val(p, "area") or 0) * (val(p, k) or 0) for p in ps) / A if ps else np.nan
        dr = {}
        for p in ps:
            dr[val(p, "drainageType")] = dr.get(val(p, "drainageType"), 0) + (val(p, "area") or 0)
        addr = val(f, "address") or {}
        r = {"id": e["id"], "key": tail(e["id"]), "farm": tail(val(e, "hasAgriFarm")), "country": addr.get("addressCountry"),
             "region": addr.get("addressRegion"), "segment": val(f, "farmSegment"),
             "organicCertified": float(bool(val(f, "organicCertified"))), "livestock": float(bool(val(f, "livestock"))),
             "crop": tail(val(e, "hasAgriCrop")), "season": val(e, "season"), "status": val(e, "reportingStatus"),
             "area": val(e, "cropArea"), "declaredArea": val(e, "declaredParcelArea"), "nParcels": len(ps),
             "clay": w("clayContent"), "organicShare": val(e, "organicSoilShare"), "slope": w("slope"),
             "distWater": w("distanceToWater"), "soilP": w("soilPhosphorus"),
             "bufferShare": sum((val(p, "area") or 0) for p in ps if val(p, "bufferStrip")) / A if ps else np.nan,
             "drainage": max(dr, key=dr.get) if dr else None,
             "irrigated": np.nan if val(e, "irrigated") is None else float(bool(val(e, "irrigated"))),
             "missing": as_list(val(e, "missingAttributes")), "flags": as_list(val(e, "dataQualityFlags"))}
        for t in list(TARGETS) + TEXT:
            r[t] = orig(e, t)
        r["area"] = orig(e, "cropArea") if orig(e, "cropArea") is not None else r["area"]
        rows.append(r)
    d = pd.DataFrame(rows).sort_values("key").reset_index(drop=True)  # broker order must not change results
    d["flagged"] = d["flags"].map(bool)
    d["role"] = np.select(
        [d.flagged, d.status.eq("reported"), d.status.str.contains("Foodhills", na=False)],
        ["review", "train", "se_partial"], "fi_nonresponse")
    for c in CAT_FEATS:
        d[c] = d[c].astype("category")
    return d


# ── layer 1: ERP yield ───────────────────────────────────────────────────────
def erp_yield(d, log):
    f = ROOT / "source-data" / "erp_purchase_lines_2021_2025.csv"
    if not f.exists():
        log.append("ERP file missing - yield is not derived from deliveries")
        return pd.DataFrame(columns=["key", "erpYield", "erpNote"])
    e = pd.read_csv(f)
    e = e[e.supplier_id.astype(str).str.match(r"^F\d") & e.material_id.isin(MATERIAL_CROP)].copy()
    e["crop"] = e.material_id.map(MATERIAL_CROP)
    e["season"] = e.posting_date.str[:4].astype(int)
    e["farm"] = e.supplier_id
    cyc = d[d.country.eq("FI")][["key", "farm", "crop", "season", "area", "role", "harvestedYield"]].copy()
    cyc["crop"] = cyc.crop.astype(str)
    train = cyc[cyc.role.eq("train")]
    med = train.groupby("crop").harvestedYield.median()
    hist = train.groupby(["farm", "crop"]).harvestedYield.median()
    e = e.merge(cyc[["key", "farm", "crop", "season", "area"]], on=["farm", "crop", "season"], how="inner")
    e["note"] = ""
    # unit error: tonnes keyed as kg
    ref_line = e.crop.map(med)
    ue = (e.quantity > 0) & (e.quantity / 1000 / e.area < 0.02 * ref_line)
    e.loc[ue, "quantity"] *= 1000
    e.loc[ue, "note"] = "unit error fixed (t keyed as kg)"
    # duplicate receipts: same positive quantity twice for the same farm/crop/year
    e["q3"] = e.quantity.round(3)
    dup = (e.quantity > 0) & e.duplicated(["key", "q3"], keep="first")
    e = e[~dup]
    g = e.groupby("key").agg(kg=("quantity", "sum"), area=("area", "first"), crop=("crop", "first"),
                             farm=("farm", "first"), note=("note", lambda s: "; ".join(x for x in set(s) if x)))
    g["y"] = g.kg / 1000 / g.area
    g["ref"] = [hist.get((f, c), med.get(c)) for f, c in zip(g.farm, g.crop)]
    notes, ys = [], []
    for y, ref, note in zip(g.y, g.ref, g.note):
        if y > 4 * ref and 0.25 * ref <= y / 10 <= 2.5 * ref:
            y, note = y / 10, (note + "; " if note else "") + "decimal shift fixed (/10)"
        elif y < ref / 4 and 0.25 * ref <= y * 10 <= 2.5 * ref:
            y, note = y * 10, (note + "; " if note else "") + "decimal shift fixed (x10)"
        if not (0.25 * ref <= y <= 3 * ref):
            y, note = np.nan, "implausible after cleaning - not used"
        ys.append(y)
        notes.append(note)
    g["y"], g["note"] = ys, notes
    g = g.reset_index()
    if dup.any():
        log.append(f"ERP: {int(dup.sum())} duplicate receipt lines dropped")
    log.append(f"ERP: {int(ue.sum())} lines with tonnes keyed as kg fixed")
    # calibrate deliveries to harvested yield per crop on clean survey cycles
    j = g.merge(train[["key", "harvestedYield"]], on="key")
    ratio = (j.y / j.harvestedYield).groupby(j.crop).median()
    g["erpYield"] = g.y / g.crop.map(ratio).fillna(1.0)
    g["erpNote"] = g.note
    return g[["key", "erpYield", "erpNote"]]


# ── cross-validation helpers ─────────────────────────────────────────────────
def hist_features(fit_df, apply_df, t, same_rows=False):
    """farm x crop mean / count of target t from fit_df, applied to apply_df (leave-one-out when same_rows)."""
    g = fit_df.groupby(["farm", "crop"], observed=True)[t].agg(["sum", "count"])
    k = list(zip(apply_df.farm, apply_df.crop))
    s = np.array([g["sum"].get(x, 0.0) for x in k], dtype=float)
    n = np.array([g["count"].get(x, 0) for x in k], dtype=float)
    if same_rows:
        own = apply_df[t].fillna(0).to_numpy(float)
        has = apply_df[t].notna().to_numpy()
        s, n = s - own * has, n - has
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(n > 0, s / n, np.nan), n


def baseline(fit_df, apply_df, t):
    cr = fit_df.groupby(["crop", "region"], observed=True)[t].mean()
    c = fit_df.groupby("crop", observed=True)[t].mean()
    return np.array([cr.get((a, b), c.get(a)) for a, b in zip(apply_df.crop, apply_df.region)], dtype=float)


def design(df, hmean, hn, extra=None):
    X = df[NUM_FEATS + CAT_FEATS].copy()
    for c in CAT_FEATS:
        X[c] = X[c].cat.codes.replace(-1, np.nan)
    X["histMean"], X["histN"] = hmean, hn
    if extra:
        for c in extra:
            X[c] = df[c].astype(float)
    return X


def hgb():
    return HistGradientBoostingRegressor(max_iter=300, learning_rate=0.05, max_leaf_nodes=15, min_samples_leaf=20,
                                         l2_regularization=1.0, categorical_features=[NUM_FEATS.__len__() + i
                                                                                      for i in range(len(CAT_FEATS))],
                                         random_state=SEED)


def clamp(x, t):
    lo, hi = TARGETS[t][1], TARGETS[t][2]
    x = np.maximum(x, lo)
    return np.minimum(x, hi) if hi is not None else x


def cv_fi(train, t, with_yield=False):
    """5-fold CV for one target. Returns dict method -> out-of-fold predictions."""
    kf = KFold(5, shuffle=True, random_state=SEED)
    pred = {m: np.full(len(train), np.nan) for m in ("baseline", "history", "model")}
    extra = ["harvestedYield"] if with_yield else None
    for tr, te in kf.split(train):
        a, b = train.iloc[tr], train.iloc[te]
        base_b = baseline(a, b, t)
        hm_b, hn_b = hist_features(a, b, t)
        hm_a, hn_a = hist_features(a, a, t, same_rows=True)
        pred["baseline"][te] = base_b
        pred["history"][te] = np.where(np.isnan(hm_b), base_b, hm_b)
        m = hgb().fit(design(a, hm_a, hn_a, extra), a[t])
        pred["model"][te] = m.predict(design(b, hm_b, hn_b, extra))
    return {k: clamp(v, t) for k, v in pred.items()}


# ── main fill ────────────────────────────────────────────────────────────────
def run(d, log):
    fills, valid = [], []
    train = d[d.role.eq("train")].reset_index(drop=True)
    erp = erp_yield(d, log)
    d = d.merge(erp, on="key", how="left")

    def add(row, attr, value, origin, method, conf, lo=None, hi=None, reported=None):
        fills.append({"id": row.id, "key": row.key, "farm": row.farm, "crop": str(row.crop), "season": row.season,
                      "role": row.role, "attribute": attr, "value": value, "unit": TARGETS.get(attr, ("text",))[0]
                      if attr in TARGETS else ("ha" if attr == "cropArea" else "text"),
                      "dataOrigin": origin, "method": method, "confidence": conf, "p10": lo, "p90": hi,
                      "reportedValue": reported})

    # ERP yield validation
    j = train.merge(erp, on="key").dropna(subset=["erpYield"])
    r_erp = (j.erpYield - j.harvestedYield).to_numpy()
    valid.append({"scope": "FI", "attribute": "harvestedYield", "method": "derived: ERP deliveries", "n": len(j),
                  "MAE": np.mean(np.abs(r_erp)), "MAE_baseline": np.nan,
                  "within10pct": np.mean(np.abs(r_erp) <= 0.1 * j.harvestedYield), "chosen": True})
    erp_q = np.quantile(r_erp, [0.1, 0.9]) if len(r_erp) else (0, 0)

    # soil type from soil map
    soil = lambda share: "Organic soil" if (share or 0) >= 0.3 else "Mineral soil"
    acc = np.mean([soil(s) == r for s, r in zip(train.organicShare, train.reportedSoilType)])
    valid.append({"scope": "FI+SE", "attribute": "reportedSoilType", "method": "derived: soil map (organic share >= 30 %)",
                  "n": len(train), "accuracy": acc, "chosen": True})

    # FI numeric targets: choose method by CV
    chosen, resid = {}, {}
    for t in TARGETS:
        p = cv_fi(train, t)
        y = train[t].to_numpy(float)
        mae = {m: np.nanmean(np.abs(p[m] - y)) for m in p}
        best = min(mae, key=mae.get)
        chosen[t], resid[t] = best, np.quantile(y - p[best], [0.1, 0.9])
        cover = np.mean((y >= p[best] + resid[t][0]) & (y <= p[best] + resid[t][1]))
        for m in p:
            valid.append({"scope": "FI", "attribute": t, "method": m, "n": len(y), "MAE": mae[m],
                          "bias": float(np.nanmean(p[m] - y)),
                          "MAE_baseline": mae["baseline"], "skill_vs_baseline": 1 - mae[m] / mae["baseline"],
                          "p10_p90_coverage": cover if m == best else np.nan, "chosen": m == best})
        if t != "harvestedYield":  # variant that also knows the yield (used where ERP gives it)
            py = cv_fi(train, t, with_yield=True)["model"]
            valid.append({"scope": "FI", "attribute": t, "method": "model + known yield", "n": len(y),
                          "MAE": np.nanmean(np.abs(py - y)), "MAE_baseline": mae["baseline"],
                          "skill_vs_baseline": 1 - np.nanmean(np.abs(py - y)) / mae["baseline"], "chosen": False})
        print(f"  CV {t:<26} baseline {mae['baseline']:.3f}  history {mae['history']:.3f}  model {mae['model']:.3f}  -> {best}")

    # final FI models on all clean cycles
    fi_need = d[d.role.isin(["fi_nonresponse", "review"])].copy()
    for t in TARGETS:
        hm_tr, hn_tr = hist_features(train, train, t, same_rows=True)
        hm, hn = hist_features(train, fi_need, t)
        base = baseline(train, fi_need, t)
        if chosen[t] == "model":
            pr = hgb().fit(design(train, hm_tr, hn_tr), train[t]).predict(design(fi_need, hm, hn))
            mname = "imputed: gradient boosting (farm history + crop, region, soil-map and parcel features)"
        elif chosen[t] == "baseline":
            pr = base
            mname = "imputed: mean of clean survey cycles for crop and region"
        else:
            pr = np.where(np.isnan(hm), base, hm)
            mname = "imputed: farm history (same farm and crop, other years)"
        fi_need[f"pred_{t}"] = clamp(pr, t)
        fi_need[f"meth_{t}"] = mname

    rnd = lambda x, n=3: None if x is None or (isinstance(x, float) and np.isnan(x)) else round(float(x), n)
    for row in fi_need.itertuples():
        if row.role == "fi_nonresponse":
            for t in TARGETS:
                if t == "harvestedYield" and not np.isnan(row.erpYield if row.erpYield == row.erpYield else np.nan):
                    v = row.erpYield
                    add(row, t, rnd(v), "derived", "derived: ERP deliveries / declared area, calibrated to survey yields"
                        + (f" ({row.erpNote})" if isinstance(row.erpNote, str) and row.erpNote else ""), "high",
                        rnd(max(v + erp_q[0], 0)), rnd(v + erp_q[1]))
                    continue
                v = getattr(row, f"pred_{t}")
                lo, hi = clamp(np.array([v + resid[t][0], v + resid[t][1]]), t)
                add(row, t, rnd(v), "imputed", getattr(row, f"meth_{t}"), "medium", rnd(lo), rnd(hi))
            add(row, "reportedSoilType", soil(row.organicShare), "derived", "derived: soil map (organic share >= 30 %)", "high")
            add(row, "fertiliserProduct", None, "imputed", "", "medium")  # filled below
        else:  # flagged review cycles: corrections only
            for fl in row.flags:
                kind = fl.split(":")[0]
                if kind == "decimal_shift_suspected":
                    add(row, "nitrogenMineralApplied", rnd(row.nitrogenMineralApplied / 10), "corrected",
                        "corrected: decimal shift (reported value / 10)", "medium", reported=row.nitrogenMineralApplied)
                elif kind == "outlier_implausible":
                    ok = row.erpYield == row.erpYield
                    v = row.erpYield if ok else getattr(row, "pred_harvestedYield")
                    add(row, "harvestedYield", rnd(v), "corrected",
                        "corrected: implausible yield replaced by ERP deliveries" if ok else
                        "corrected: implausible yield replaced by model estimate", "high" if ok else "medium",
                        reported=row.harvestedYield)
                elif kind == "soil_misreport_suspected":
                    add(row, "reportedSoilType", soil(row.organicShare), "corrected",
                        "corrected: soil type from soil map (>= 30 % organic soil)", "medium", reported=row.reportedSoilType)
                elif kind == "area_mismatch" and row.declaredArea:
                    add(row, "cropArea", rnd(row.declaredArea, 2), "corrected",
                        "corrected: crop area from declared parcels", "medium", reported=row.area)

    # fertiliser product: farm x crop mode -> farm mode -> crop x region mode
    fm_c = train.groupby(["farm", "crop"], observed=True).fertiliserProduct.agg(lambda s: s.mode().iat[0])
    fm = train.groupby("farm").fertiliserProduct.agg(lambda s: s.mode().iat[0])
    cr = train.groupby(["crop", "region"], observed=True).fertiliserProduct.agg(lambda s: s.mode().iat[0])
    cm = train.groupby("crop", observed=True).fertiliserProduct.agg(lambda s: s.mode().iat[0])

    def product(farm, crop, region):
        for src, key, m in ((fm_c, (farm, crop), "farm history (same crop)"), (fm, farm, "farm history (any crop)"),
                            (cr, (crop, region), "most common for crop and region"), (cm, crop, "most common for crop")):
            if key in src.index:
                return src[key], m
        return None, ""
    # validate: leave-one-out of the cycle's own product is approximated with 5-fold
    kf, hit, hitb = KFold(5, shuffle=True, random_state=SEED), 0, 0
    for tr, te in kf.split(train):
        a, b = train.iloc[tr], train.iloc[te]
        fmc = a.groupby(["farm", "crop"], observed=True).fertiliserProduct.agg(lambda s: s.mode().iat[0])
        cmm = a.groupby("crop", observed=True).fertiliserProduct.agg(lambda s: s.mode().iat[0])
        for r in b.itertuples():
            pr = fmc.get((r.farm, r.crop), cmm.get(r.crop))
            hit += pr == r.fertiliserProduct
            hitb += cmm.get(r.crop) == r.fertiliserProduct
    use_hist = hit >= hitb
    valid.append({"scope": "FI", "attribute": "fertiliserProduct", "method": "farm history mode", "n": len(train),
                  "accuracy": hit / len(train), "accuracy_baseline": hitb / len(train), "chosen": use_hist})
    valid.append({"scope": "FI", "attribute": "fertiliserProduct", "method": "most common for crop", "n": len(train),
                  "accuracy": hitb / len(train), "chosen": not use_hist})
    pconf = "medium" if max(hit, hitb) / len(train) >= 0.5 else "low"
    for f in fills:
        if f["attribute"] == "fertiliserProduct" and f["value"] is None:
            if use_hist:
                v, m = product(f["farm"], f["crop"], d.loc[d.key.eq(f["key"]), "region"].iat[0])
            else:
                v, m = cm.get(f["crop"]), "most common for crop"
            f["value"], f["method"], f["confidence"] = v, f"imputed: {m}", pconf

    # layer 4: SE peas, transfer from FI peas
    se = d[d.role.eq("se_partial")].copy()
    fp = train[train.crop.astype(str).eq("pea")].reset_index(drop=True)
    se_targets = [t for t in TARGETS if t not in ("nitrogenMineralApplied", "dieselConsumption", "harvestedYield")]
    for t in se_targets:
        # grouped validation on FI peas (by region when there are several, else by farm)
        oof = np.full(len(fp), np.nan)
        groups = fp.region.astype(str) if fp.region.nunique() >= 3 else fp.farm
        for _, te in GroupKFold(min(5, groups.nunique())).split(fp, groups=groups):
            te = np.isin(np.arange(len(fp)), te)
            m = HistGradientBoostingRegressor(max_iter=200, learning_rate=0.05, min_samples_leaf=20, random_state=SEED)
            oof[te] = m.fit(fp.loc[~te, SE_FEATS].astype(float), fp.loc[~te, t]).predict(fp.loc[te, SE_FEATS].astype(float))
        oof = clamp(oof, t)
        y = fp[t].to_numpy(float)
        base = np.full(len(fp), np.nanmean(y))
        mae_m, mae_b = np.mean(np.abs(oof - y)), np.mean(np.abs(base - y))
        use_model = mae_m < mae_b
        q = np.quantile(y - (oof if use_model else base), [0.1, 0.9])
        for meth, mae_x, ch in (("transfer model", mae_m, use_model), ("FI pea mean", mae_b, not use_model)):
            valid.append({"scope": "SE (proxy: FI peas, grouped CV)", "attribute": t, "method": meth, "n": len(fp),
                          "MAE": mae_x, "MAE_baseline": mae_b, "skill_vs_baseline": 1 - mae_x / mae_b, "chosen": ch})
        if use_model:
            m = HistGradientBoostingRegressor(max_iter=200, learning_rate=0.05, min_samples_leaf=20, random_state=SEED)
            pr = clamp(m.fit(fp[SE_FEATS].astype(float), fp[t]).predict(se[SE_FEATS].astype(float)), t)
            mtext = "imputed: transfer model trained on FI pea cycles (N, diesel, yield, soil map)"
        else:
            pr = np.full(len(se), np.nanmean(y))
            mtext = "imputed: mean of clean FI pea cycles"
        for row, v in zip(se.itertuples(), pr):
            lo, hi = clamp(np.array([v + q[0], v + q[1]]), t)
            add(row, t, rnd(v), "imputed", mtext, "low", rnd(lo), rnd(hi))
    pea_prod = fp.fertiliserProduct.mode().iat[0]
    pea_share = fp.fertiliserProduct.eq(pea_prod).mean()
    for row in se.itertuples():
        add(row, "reportedSoilType", soil(row.organicShare), "derived", "derived: soil map (organic share >= 30 %)", "high")
        add(row, "fertiliserProduct", pea_prod if (row.nitrogenMineralApplied or 0) > 0 else "none (no mineral N)",
            "imputed", f"imputed: most common FI pea product ({pea_share:.0%} of FI pea cycles)", "low")
    return pd.DataFrame(fills), pd.DataFrame(valid), d


# ── write-back ───────────────────────────────────────────────────────────────
def P(v, **s):
    a = {"type": "Property", "value": v}
    a.update({k: x for k, x in s.items() if x is not None})
    return a


def updates(fills, run_ts):
    ups = []
    for cid, g in fills.groupby("id"):
        e = {"id": cid, "type": "AgriCropCycle"}
        for f in g.itertuples():
            if f.value is None:
                continue
            sub = {"dataOrigin": f.dataOrigin, "imputationMethod": f.method, "confidence": f.confidence,
                   "gapFillVersion": VERSION}
            if f.unit not in ("text",):
                sub["unitText"] = f.unit
            if f.p10 is not None and f.p10 == f.p10:
                sub["predictionInterval"] = [f.p10, f.p90]
            if f.reportedValue is not None and f.reportedValue == f.reportedValue:
                sub["reportedValue"] = f.reportedValue
            e[f.attribute] = P(f.value, **sub)
        e["filledAttributes"] = P(sorted(g.attribute[g.dataOrigin.ne("corrected")].unique().tolist()) or ["none"])
        e["correctedAttributes"] = P(sorted(g.attribute[g.dataOrigin.eq("corrected")].unique().tolist()) or ["none"])
        e["gapFillStatus"] = P("filled")
        e["gapFillRun"] = P({"@type": "DateTime", "@value": run_ts})
        ups.append(e)
    return ups


def write_back(broker, tenant, ups, ctx, batch=50):
    ok, errs = 0, []
    for i in range(0, len(ups), batch):
        chunk = [dict(u, **{"@context": ctx}) for u in ups[i:i + batch]]
        st, body, _ = L.request("POST", f"{broker}/ngsi-ld/v1/entityOperations/update", tenant, chunk)
        if st in (200, 204):
            ok += len(chunk)
        elif st == 207:
            e = json.loads(body or "{}").get("errors", [])
            ok += len(chunk) - len(e)
            errs += e
        else:
            errs.append(f"HTTP {st}: {body[:300]}")
        if (i // batch) % 20 == 0:
            print(f"  updated {ok}/{len(ups)}", flush=True)
    print(f"  updated {ok}/{len(ups)} AgriCropCycle entities, {len(errs)} errors")
    for e in errs[:5]:
        print("   ", json.dumps(e)[:300])
    return ok, errs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--broker", default="http://localhost:1026")
    ap.add_argument("--tenant", default="apetit_ccn_post_ml")
    ap.add_argument("--dry-run", action="store_true", help="fill, validate and write files; no broker updates")
    a = ap.parse_args()
    if a.tenant == "apetit_ccn":
        sys.exit("Refusing to write gap fills into the as-reported tenant apetit_ccn. Use apetit_ccn_post_ml.")
    broker = a.broker.rstrip("/")
    L.TYPE_IRI["Catchment"] = L.ALCA + "Catchment"
    ctx_terms, rev = context_maps()
    L.wait_for_broker(broker)
    print(f"Reading tenant '{a.tenant}' ...")
    cycles = read_type(broker, a.tenant, "AgriCropCycle", rev)
    if not cycles:
        sys.exit("No AgriCropCycle entities - copy the data first: python3 scripts/copy_tenant.py")
    parcels = read_type(broker, a.tenant, "AgriParcel", rev)
    farms = read_type(broker, a.tenant, "AgriFarm", rev)
    d = build_table(cycles, parcels, farms)
    print("Roles:", d.role.value_counts().to_dict())
    log = []
    print("Cross-validating and filling ...")
    fills, valid, d = run(d, log)
    for x in log:
        print(" ", x)
    OUT.mkdir(parents=True, exist_ok=True)
    fills.to_csv(OUT / "fills.csv", index=False)
    valid.to_csv(OUT / "validation.csv", index=False)
    print(f"Fills: {len(fills)} values on {fills.id.nunique()} cycles  ->  results/gapfill/fills.csv")
    print(fills.groupby(["dataOrigin", "confidence"]).size().to_string())
    run_ts = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        import post_ml_excel
        post_ml_excel.write(d, fills, valid, log, OUT / "Post ML data.xlsx", a.tenant, run_ts, VERSION)
        print("Excel: results/gapfill/Post ML data.xlsx")
    except ImportError as ex:
        print("Excel skipped:", ex)
    ups = updates(fills, run_ts)
    (OUT / "updates_preview.json").write_text(json.dumps(ups[:3], indent=1, default=str))
    if a.dry_run:
        print(f"DRY RUN - {len(ups)} AgriCropCycle updates prepared, nothing written (see results/gapfill/updates_preview.json)")
        return
    ctx = [L.CORE, ctx_terms]
    write_back(broker, a.tenant, json.loads(json.dumps(ups, default=lambda o: o.item() if hasattr(o, "item") else str(o))), ctx)


if __name__ == "__main__":
    main()
