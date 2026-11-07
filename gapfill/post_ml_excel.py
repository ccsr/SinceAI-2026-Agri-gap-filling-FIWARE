"""post_ml_excel.py - writes 'Post ML data.xlsx' for gapfill_ccn.py (crop cycles after gap filling)."""
import numpy as np
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

ATTRS = [("cropArea", "Crop area (ha)"), ("nitrogenMineralApplied", "N mineral (kg N/ha)"),
         ("manureNitrogenApplied", "N manure (kg N/ha)"), ("phosphorusMineralApplied", "P mineral (kg P/ha)"),
         ("dieselConsumption", "Diesel (l/ha)"), ("limeApplied", "Lime (t/ha)"),
         ("pesticideApplications", "Spray passes"), ("catchCropShare", "Catch crop share (0-1)"),
         ("harvestedYield", "Yield (t/ha)"), ("reportedSoilType", "Soil type"), ("fertiliserProduct", "Fertiliser product")]
ROLE = {"train": "reported, clean", "review": "reported, flagged (corrected)", "fi_nonresponse": "FI non-response (filled)",
        "se_partial": "SE Foodhills (partly filled)"}
FILL = {"derived": "C6EFCE", "imputed:medium": "DDEBF7", "imputed:low": "FFF2CC", "corrected": "F8CBAD"}
F = Font(name="Arial", size=10)
B = Font(name="Arial", size=10, bold=True)
HF = PatternFill("solid", fgColor="1F4E78")
HFont = Font(name="Arial", size=10, bold=True, color="FFFFFF")


def header(ws, row, names):
    for i, n in enumerate(names, 1):
        c = ws.cell(row, i, n)
        c.font, c.fill = HFont, HF
        c.alignment = Alignment(wrap_text=True, vertical="center")
    ws.row_dimensions[row].height = 32


def clean(x):
    if x is None:
        return None
    if isinstance(x, (float, np.floating)) and np.isnan(x):
        return None
    if isinstance(x, np.generic):
        return x.item()
    return x


def write(d, fills, valid, log, path, tenant, run_ts, version):
    wb = Workbook()
    wb.calculation.fullCalcOnLoad = True
    # README
    rd = wb.active
    rd.title = "README"
    lines = [
        (f"Post ML data - Apetit Climate & Nature Navigator 2026 (tenant {tenant})", 14),
        (f"Gap fill run {run_ts}, {version}. Synthetic twin by Since AI ry (CC BY 4.0); not real farm data.", 10),
        ("", 10), ("Layers (most reliable first)", 11),
        ("derived - calculated from another source: soil type from the soil map; yield from ERP deliveries / declared area (FI 2021-2025).", 10),
        ("corrected - a flagged reported value replaced; the reported value is kept in the Fills sheet and as reportedValue in the broker.", 10),
        ("imputed (medium) - FI non-response cycles: farm history and gradient boosting, method chosen per attribute by 5-fold cross-validation.", 10),
        ("imputed (low) - SE Foodhills peas: transfer model trained on FI peas using the SE values that are known; no SE ground truth exists.", 10),
        ("", 10), ("Legend (Cycles sheet)", 11),
        ("White = reported   Green = derived   Blue = imputed (medium)   Yellow = imputed (low)   Orange = corrected", 10),
        ("", 10), ("Sheets", 11),
        ("Summary - counts by attribute, origin and confidence (formulas on the Fills sheet).", 10),
        ("Validation - cross-validated error of each method against hidden reported values; 'chosen' marks the method used.", 10),
        ("Cycles - one row per crop cycle with the values after filling.", 10),
        ("Fills - one row per filled or corrected value: method, confidence, p10-p90 range, reported value.", 10),
        ("", 10), ("Run notes", 11)] + [(x, 10) for x in log] + [
        ("", 10), ("Not done yet", 11),
        ("SE values are not anchored to Swedish statistics (e.g. SCB fertiliser use by crop); treat them as placeholders until checked.", 10),
        ("Fills change activity data only. Emissions are recalculated in a separate step (HESTIA or the footprint calculator).", 10)]
    for i, (t, sz) in enumerate(lines, 1):
        c = rd.cell(i, 1, t)
        c.font = Font(name="Arial", size=sz, bold=sz > 10)
        c.alignment = Alignment(wrap_text=True)
    rd.column_dimensions["A"].width = 130

    # Fills (long)
    fs = wb.create_sheet("Fills")
    cols = ["key", "farm", "crop", "season", "role", "attribute", "value", "unit", "dataOrigin", "confidence", "p10", "p90",
            "reportedValue", "method"]
    header(fs, 1, ["Crop cycle", "Farm", "Crop", "Season", "Role", "Attribute", "Value", "Unit", "Origin", "Confidence",
                   "p10", "p90", "Reported value", "Method"])
    fl = fills.sort_values(["key", "attribute"])
    for r, row in enumerate(fl[cols].itertuples(index=False), 2):
        for c, v in enumerate(row, 1):
            v = ROLE.get(v, v) if c == 5 else v
            fs.cell(r, c, clean(v)).font = F
    nf = len(fl) + 1
    for i, w in enumerate([24, 8, 11, 7, 24, 24, 12, 9, 10, 10, 9, 9, 13, 80], 1):
        fs.column_dimensions[get_column_letter(i)].width = w
    fs.freeze_panes, fs.auto_filter.ref = "A2", f"A1:N{nf}"

    # Summary (formulas)
    sm = wb.create_sheet("Summary", 1)
    sm["A1"] = "Gap fill summary"
    sm["A1"].font = Font(name="Arial", size=13, bold=True)
    origins = ["derived", "imputed", "corrected"]
    header(sm, 3, ["Attribute"] + origins + ["Total", "of which low confidence"])
    attrs = [a for a, _ in ATTRS if a in set(fills.attribute)]
    A, O, C = f"Fills!$F$2:$F${nf}", f"Fills!$I$2:$I${nf}", f"Fills!$J$2:$J${nf}"
    for i, a in enumerate(attrs, 4):
        sm.cell(i, 1, a).font = F
        for j, o in enumerate(origins, 2):
            sm.cell(i, j, f'=COUNTIFS({A},$A{i},{O},"{o}")').font = F
        sm.cell(i, 5, f"=SUM(B{i}:D{i})").font = F
        sm.cell(i, 6, f'=COUNTIFS({A},$A{i},{C},"low")').font = F
    t = 4 + len(attrs)
    sm.cell(t, 1, "Total").font = B
    for j in range(2, 7):
        L = get_column_letter(j)
        sm.cell(t, j, f"=SUM({L}4:{L}{t - 1})").font = B
    sm.cell(t + 2, 1, "Crop cycles by role").font = B
    header(sm, t + 3, ["Role", "Crop cycles"])
    for i, (k, lab) in enumerate(ROLE.items(), t + 4):
        sm.cell(i, 1, lab).font = F
        sm.cell(i, 2, int((d.role == k).sum())).font = F
    sm.cell(t + 8, 1, "Total").font = B
    sm.cell(t + 8, 2, f"=SUM(B{t + 4}:B{t + 7})").font = B
    for i, w in enumerate([28, 12, 12, 12, 12, 22], 1):
        sm.column_dimensions[get_column_letter(i)].width = w

    # Validation
    vs = wb.create_sheet("Validation", 2)
    vcols = [c for c in ["scope", "attribute", "method", "n", "MAE", "bias", "MAE_baseline", "skill_vs_baseline",
                         "p10_p90_coverage", "within10pct", "accuracy", "accuracy_baseline", "chosen"] if c in valid]
    header(vs, 1, [c.replace("_", " ") for c in vcols])
    for r, row in enumerate(valid[vcols].itertuples(index=False), 2):
        for c, v in enumerate(row, 1):
            cell = vs.cell(r, c, clean(v))
            cell.font = B if row[-1] is True else F
            if vcols[c - 1] in ("skill_vs_baseline", "p10_p90_coverage", "within10pct", "accuracy", "accuracy_baseline"):
                cell.number_format = "0.0%"
            elif vcols[c - 1] in ("MAE", "MAE_baseline", "bias"):
                cell.number_format = "0.000"
    r = len(valid) + 3
    for t_ in ["MAE = mean absolute error on hidden reported values (out-of-fold). Baseline = median for crop and region.",
               "skill vs baseline = 1 - MAE / baseline MAE (higher is better; 0 = no better than the median).",
               "p10-p90 coverage = share of hidden values inside the stated range (target 80 %). Bold = method used."]:
        vs.cell(r, 1, t_).font = F
        r += 1
    for i, w in enumerate([34, 24, 40, 7, 9, 9, 12, 12, 12, 11, 10, 12, 8], 1):
        vs.column_dimensions[get_column_letter(i)].width = w
    vs.freeze_panes = "A2"

    # Cycles after fill
    cs = wb.create_sheet("Cycles", 3)
    base = ["key", "farm", "country", "region", "crop", "season", "role", "nParcels", "clay", "organicShare"]
    names = ["Crop cycle", "Farm", "Country", "Region", "Crop", "Season", "Role", "Parcels", "Clay % (soil map)",
             "Organic soil share"] + [h for _, h in ATTRS] + ["Filled / corrected"]
    header(cs, 1, names)
    fx = {(k, a): (v, o, c) for k, a, v, o, c in fills[["key", "attribute", "value", "dataOrigin", "confidence"]]
          .itertuples(index=False)}
    src = {"cropArea": "area", "reportedSoilType": "reportedSoilType"}
    for r, row in enumerate(d.sort_values("key").itertuples(), 2):
        for c, k in enumerate(base, 1):
            v = getattr(row, k)
            cs.cell(r, c, clean(ROLE.get(v, v) if k == "role" else (str(v) if k in ("crop", "region") else v))).font = F
        n = 0
        for j, (a, _) in enumerate(ATTRS, len(base) + 1):
            cell = cs.cell(r, j)
            if (row.key, a) in fx:
                v, o, conf = fx[(row.key, a)]
                cell.value = clean(v)
                cell.fill = PatternFill("solid", fgColor=FILL.get(o if o != "imputed" else f"imputed:{conf}", "FFFFFF"))
                n += 1
            else:
                cell.value = clean(getattr(row, src.get(a, a)))
            cell.font = F
        cs.cell(r, len(names), n).font = F
    for i, w in enumerate([24, 8, 7, 8, 11, 7, 24, 7, 9, 9] + [11] * len(ATTRS) + [9], 1):
        cs.column_dimensions[get_column_letter(i)].width = w
    cs.freeze_panes, cs.auto_filter.ref = "B2", f"A1:{get_column_letter(len(names))}{len(d) + 1}"
    for rr in range(2, len(d) + 2):
        cs.cell(rr, 10).number_format = "0%"
        cs.cell(rr, 9).number_format = "0.0"
    wb.save(path)
