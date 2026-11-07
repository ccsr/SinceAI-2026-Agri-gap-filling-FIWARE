"""Build Pre_ML_data.xlsx: every AgriCropCycle of tenant apetit_ccn with its gaps, before any ML filling."""
import json, sys, collections as C
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableStyleInfo
from openpyxl.formatting.rule import FormulaRule

SRC = sys.argv[1]
OUT = sys.argv[2]
ld = lambda f: json.load(open(f"{SRC}/{f}"))
cycles, parcels, farms = ld("06_crop_cycles.json"), ld("05_agri_parcels.json"), ld("04_agri_farms.json")
fps = ld("07_footprints_current_calculator.json")
v = lambda e, k: (e.get(k) or {}).get("value")
short = lambda u: u.split(":")[-1]

P = {p["id"]: p for p in parcels}
F = {f["id"]: f for f in farms}
FP = {f["refAgriCropCycle"]["object"]: v(f, "carbonFootprint") for f in fps}

TARGETS = [  # attribute, header, unit
    ("nitrogenMineralApplied", "N mineral", "kg N/ha"),
    ("manureNitrogenApplied", "N manure", "kg N/ha"),
    ("phosphorusMineralApplied", "P mineral", "kg P/ha"),
    ("dieselConsumption", "Diesel", "l/ha"),
    ("limeApplied", "Lime", "t/ha"),
    ("pesticideApplications", "Spray passes", "passes"),
    ("catchCropShare", "Catch crop share", "0-1"),
    ("harvestedYield", "Yield", "t/ha"),
    ("reportedSoilType", "Reported soil type", "text"),
    ("fertiliserProduct", "Fertiliser product", "text"),
]
FLAGMAP = {"decimal_shift_suspected": "nitrogenMineralApplied", "outlier_implausible": "harvestedYield",
           "area_mismatch": "cropArea", "soil_misreport_suspected": "reportedSoilType"}

def role(e):
    st, fl = v(e, "reportingStatus"), v(e, "dataQualityFlags")
    if fl: return "review (flagged)"
    if st == "reported": return "train"
    if "Foodhills" in st: return "predict (partial gaps)"
    return "predict (all activity data)"

def parcel_feats(ids):
    ps = [P[i] for i in ids if i in P]
    A = sum(v(p, "area") for p in ps) or 1
    w = lambda k: round(sum(v(p, "area") * (v(p, k) or 0) for p in ps) / A, 2)
    share = lambda f: round(sum(v(p, "area") for p in ps if f(p)) / A, 3)
    catch = C.Counter()
    for p in ps: catch[short(p["refCatchment"]["object"])] += v(p, "area")
    drains = C.Counter()
    for p in ps: drains[v(p, "drainageType")] += v(p, "area")
    return dict(n=len(ps), clay=w("clayContent"), slope=w("slope"), dist=w("distanceToWater"), soilp=w("soilPhosphorus"),
                buffer=share(lambda p: v(p, "bufferStrip")), drain=drains.most_common(1)[0][0] if drains else None,
                catch=catch.most_common(1)[0][0] if catch else None)

COLS = ["Crop cycle id", "Farm", "Country", "Region", "Farm segment", "Organic certified", "Livestock", "Crop", "Season",
        "Reporting status", "ML role", "Crop area (ha)", "Parcels", "Clay % (soil map, area-weighted)",
        "Organic soil share (soil map)", "Slope %", "Distance to water (m)", "Soil P (mg/l)", "Buffer strip share",
        "Main drainage", "Main catchment", "Irrigated", "Delivered (t)"] \
       + [f"{h} ({u})" if u != "text" else h for _, h, u in TARGETS] \
       + ["Current-calc footprint (kg CO2e/t)", "Gaps (count)", "Missing attributes", "Quality flags"]
TCOL0 = 24  # first target column (1-based)

wb = Workbook()
font = Font(name="Arial", size=10)
bold = Font(name="Arial", size=10, bold=True)
hdr_fill = PatternFill("solid", fgColor="1F4E78")
hdr_font = Font(name="Arial", size=10, bold=True, color="FFFFFF")
gap_fill = PatternFill("solid", fgColor="D9D9D9")
flag_fill = PatternFill("solid", fgColor="F8CBAD")
thin = Side(style="thin", color="BFBFBF")

def header(ws, row, names, widths=None):
    for i, n in enumerate(names, 1):
        c = ws.cell(row, i, n); c.font = hdr_font; c.fill = hdr_fill
        c.alignment = Alignment(wrap_text=True, vertical="center")
    ws.row_dimensions[row].height = 42

# ---------- README ----------
rd = wb.active; rd.title = "README"
lines = [
    ("Pre ML data - Apetit Climate & Nature Navigator 2026 (tenant apetit_ccn)", True),
    ("", False),
    ("What this is", True),
    ("Every farm x crop x year crop cycle loaded into NGSI-LD tenant apetit_ccn, exactly as reported, with all gaps and quality flags, before any ML or rule-based gap filling.", False),
    ("Source: Apetit Climate & Nature Navigator synthetic dataset, Since AI ry 2026 (CC BY 4.0). Synthetic twin; not real farm data.", False),
    ("Parcel features are area-weighted over the parcels declared for the crop in that year (soil map, ~88 % accurate).", False),
    ("", False),
    ("Sheets", True),
    ("Summary - gap counts by reporting status and attribute, flag counts, ML-role counts (all formulas on the Cycles sheet).", False),
    ("Cycles - one row per crop cycle (8,448). Features on the left, the 10 attributes to fill in the middle, gaps and flags on the right.", False),
    ("Gaps_long - one row per missing value (cycle x attribute): the fill list for ML.", False),
    ("Flags - the 331 cycles whose reported values look wrong; the suspect value and the reference it was compared against.", False),
    ("Dictionary - column definitions.", False),
    ("", False),
    ("Legend", True),
    ("Grey cell = missing value (gap).", False),
    ("Orange cell = reported value that is flagged as suspicious; kept as reported.", False),
    ("", False),
    ("ML role", True),
    ("train - FI survey cycle, fully reported, no flags (3,106 cycles).", False),
    ("review (flagged) - reported but flagged; check or exclude before training.", False),
    ("predict (partial gaps) - SE Foodhills cycle: N, diesel and yield known; 7 attributes missing.", False),
    ("predict (all activity data) - FI contract crop with no survey answer: only area, parcels and soil map known; 10 attributes missing.", False),
]
for i, (t, b) in enumerate(lines, 1):
    c = rd.cell(i, 1, t); c.font = Font(name="Arial", size=14 if i == 1 else 10, bold=b or i == 1)
    c.alignment = Alignment(wrap_text=True, vertical="top")
rd.column_dimensions["A"].width = 120

# ---------- Cycles ----------
ws = wb.create_sheet("Cycles")
header(ws, 1, COLS)
gaps_long, flags_rows = [], []
crop_n_median = {}
by_crop = C.defaultdict(list)
for e in cycles:
    if v(e, "reportingStatus") == "reported" and v(e, "nitrogenMineralApplied") is not None:
        by_crop[short(e["hasAgriCrop"]["object"])].append(v(e, "nitrogenMineralApplied"))
for k, xs in by_crop.items():
    xs = sorted(xs); crop_n_median[k] = xs[len(xs) // 2]

cycles.sort(key=lambda e: e["id"])
r = 2
for e in cycles:
    f = F[e["hasAgriFarm"]["object"]]
    pf = parcel_feats(e["hasAgriParcel"]["object"])
    missing = v(e, "missingAttributes") or []
    flags = v(e, "dataQualityFlags") or []
    crop = short(e["hasAgriCrop"]["object"])
    row = [short(e["id"]), short(f["id"]), v(f, "address")["addressCountry"], v(f, "address")["addressRegion"],
           v(f, "farmSegment"), v(f, "organicCertified"), v(f, "livestock"), crop, v(e, "season"),
           v(e, "reportingStatus"), role(e), v(e, "cropArea"), pf["n"], pf["clay"], v(e, "organicSoilShare"),
           pf["slope"], pf["dist"], pf["soilp"], pf["buffer"], pf["drain"], pf["catch"], v(e, "irrigated"),
           v(e, "deliveredQuantity")] \
          + [v(e, a) for a, _, _ in TARGETS] \
          + [FP.get(e["id"]), None, ", ".join(missing), ", ".join(flags)]
    for i, val in enumerate(row, 1):
        c = ws.cell(r, i, val); c.font = font
    ws.cell(r, TCOL0 + len(TARGETS) + 1, f"=COUNTBLANK({get_column_letter(TCOL0)}{r}:{get_column_letter(TCOL0+len(TARGETS)-1)}{r})").font = font
    flagged_attrs = {FLAGMAP[x.split(":")[0]] for x in flags}
    for j, (a, h, u) in enumerate(TARGETS):
        c = ws.cell(r, TCOL0 + j)
        if a in missing:
            c.fill = gap_fill
            gaps_long.append([short(e["id"]), short(f["id"]), crop, v(e, "season"), v(e, "reportingStatus"), a, u])
        if a in flagged_attrs: c.fill = flag_fill
    if "cropArea" in flagged_attrs: ws.cell(r, 12).fill = flag_fill
    for x in flags:
        kind, detail = x.split(":", 1)
        a = FLAGMAP[kind]
        if a == "nitrogenMineralApplied": ref = f"crop median {crop_n_median.get(crop)} kg N/ha"; val = v(e, a)
        elif a == "harvestedYield": ref = "outside plausible range for crop"; val = v(e, a)
        elif a == "cropArea": ref = f"declared parcels {v(e, 'declaredParcelArea')} ha"; val = v(e, a)
        else: ref = f"soil map organic share {v(e, 'organicSoilShare')}"; val = v(e, a)
        flags_rows.append([short(e["id"]), short(f["id"]), crop, v(e, "season"), kind, a, val, ref])
    r += 1
last = r - 1
widths = [26, 8, 7, 8, 9, 9, 9, 10, 7, 30, 22, 9, 7, 10, 10, 7, 9, 8, 9, 13, 9, 8, 9] + [10] * 8 + [16, 16, 18, 8, 60, 50]
for i, wd in enumerate(widths, 1): ws.column_dimensions[get_column_letter(i)].width = wd
ws.freeze_panes = "B2"
ws.auto_filter.ref = f"A1:{get_column_letter(len(COLS))}{last}"
for col, fmt in [(12, "0.00"), (14, "0.0"), (15, "0.0%"), (19, "0%"), (TCOL0 + 6, "0%")]:
    for rr in range(2, last + 1): ws.cell(rr, col).number_format = fmt
# make sure the grey shows even if values are typed in later only when still blank
rng = f"{get_column_letter(TCOL0)}2:{get_column_letter(TCOL0+len(TARGETS)-1)}{last}"
ws.conditional_formatting.add(rng, FormulaRule(formula=[f'ISBLANK({get_column_letter(TCOL0)}2)'], fill=gap_fill))

# ---------- Gaps_long ----------
gl = wb.create_sheet("Gaps_long")
GH = ["Crop cycle id", "Farm", "Crop", "Season", "Reporting status", "Missing attribute", "Unit", "Filled value", "Fill method", "Fill confidence"]
header(gl, 1, GH)
for i, row in enumerate(gaps_long, 2):
    for j, val in enumerate(row, 1): gl.cell(i, j, val).font = font
for i, wd in enumerate([26, 8, 10, 7, 36, 24, 9, 12, 18, 14], 1): gl.column_dimensions[get_column_letter(i)].width = wd
gl.freeze_panes = "A2"; gl.auto_filter.ref = f"A1:J{len(gaps_long)+1}"
for j in (8, 9, 10):
    gl.cell(1, j).fill = PatternFill("solid", fgColor="FFC000"); gl.cell(1, j).font = Font(name="Arial", size=10, bold=True)

# ---------- Flags ----------
fs = wb.create_sheet("Flags")
header(fs, 1, ["Crop cycle id", "Farm", "Crop", "Season", "Flag", "Attribute", "Reported value", "Compared against"])
for i, row in enumerate(flags_rows, 2):
    for j, val in enumerate(row, 1): fs.cell(i, j, val).font = font
for i, wd in enumerate([26, 8, 10, 7, 26, 24, 14, 34], 1): fs.column_dimensions[get_column_letter(i)].width = wd
fs.freeze_panes = "A2"; fs.auto_filter.ref = f"A1:H{len(flags_rows)+1}"

# ---------- Summary (formulas) ----------
GAPC = get_column_letter(TCOL0 + len(TARGETS) + 1); FLAGC = get_column_letter(len(COLS))
sm = wb.create_sheet("Summary", 1)
sm["A1"] = "Gap summary - tenant apetit_ccn, before gap filling"; sm["A1"].font = Font(name="Arial", size=13, bold=True)
statuses = ["reported", "reported (Foodhills grower file, partial)", "not reported (survey non-response)"]
STC = f"Cycles!$J$2:$J${last}"
sm["A3"] = "Missing values by reporting status and attribute"; sm["A3"].font = bold
header(sm, 4, ["Attribute"] + statuses + ["Total missing", "% of all cycles"])
sm.cell(5, 1, "Crop cycles").font = bold
for j, s in enumerate(statuses, 2):
    sm.cell(5, j, f'=COUNTIF({STC},"{s}")').font = bold
sm.cell(5, 5, "=SUM(B5:D5)").font = bold
for i, (a, h, u) in enumerate(TARGETS, 6):
    col = get_column_letter(TCOL0 + i - 6)
    sm.cell(i, 1, f"{h} ({u})" if u != "text" else h).font = font
    for j, s in enumerate(statuses, 2):
        sm.cell(i, j, f'=COUNTIFS({STC},"{s}",Cycles!${col}$2:${col}${last},"")').font = font
    sm.cell(i, 5, f"=SUM(B{i}:D{i})").font = font
    c = sm.cell(i, 6, f"=E{i}/$E$5"); c.font = font; c.number_format = "0.0%"
tr = 6 + len(TARGETS)
sm.cell(tr, 1, "Total missing values").font = bold
for j in range(2, 6):
    L = get_column_letter(j); sm.cell(tr, j, f"=SUM({L}6:{L}{tr-1})").font = bold
sm.cell(tr + 1, 1, "Check: rows in Gaps_long").font = font
sm.cell(tr + 1, 5, f"=COUNTA(Gaps_long!A2:A{len(gaps_long)+1})").font = font

r0 = tr + 3
sm.cell(r0, 1, "ML role").font = bold
header(sm, r0 + 1, ["Role", "Crop cycles"])
for i, ro in enumerate(["train", "review (flagged)", "predict (partial gaps)", "predict (all activity data)"], r0 + 2):
    sm.cell(i, 1, ro).font = font
    sm.cell(i, 2, f'=COUNTIF(Cycles!$K$2:$K${last},"{ro}")').font = font
sm.cell(r0 + 6, 1, "Total").font = bold; sm.cell(r0 + 6, 2, f"=SUM(B{r0+2}:B{r0+5})").font = bold

r1 = r0 + 8
sm.cell(r1, 1, "Quality flags (cycles)").font = bold
header(sm, r1 + 1, ["Flag", "Crop cycles", "Attribute affected"])
for i, (k, a) in enumerate(FLAGMAP.items(), r1 + 2):
    sm.cell(i, 1, k).font = font
    sm.cell(i, 2, f'=COUNTIF(Cycles!${FLAGC}$2:${FLAGC}${last},"*{k}*")').font = font
    sm.cell(i, 3, a).font = font

r2 = r1 + 7
crops = sorted({row[2] for row in gaps_long} | set(by_crop))
sm.cell(r2, 1, "Crop cycles with gaps, by crop").font = bold
header(sm, r2 + 1, ["Crop", "All cycles", "Cycles with gaps", "% with gaps"])
for i, cr in enumerate(crops, r2 + 2):
    sm.cell(i, 1, cr).font = font
    sm.cell(i, 2, f'=COUNTIF(Cycles!$H$2:$H${last},A{i})').font = font
    sm.cell(i, 3, f'=COUNTIFS(Cycles!$H$2:$H${last},A{i},Cycles!${GAPC}$2:${GAPC}${last},">0")').font = font
    c = sm.cell(i, 4, f"=IF(B{i}=0,0,C{i}/B{i})"); c.font = font; c.number_format = "0.0%"
for i, wd in enumerate([34, 16, 22, 24, 14, 14], 1): sm.column_dimensions[get_column_letter(i)].width = wd

# ---------- Dictionary ----------
dc = wb.create_sheet("Dictionary")
header(dc, 1, ["Column (Cycles)", "NGSI-LD attribute / source", "Meaning"])
D = [
    ("Crop cycle id", "AgriCropCycle.id", "Farm x crop x year, short form of urn:ngsi-ld:AgriCropCycle:ccn:<id>"),
    ("Farm / Country / Region / Farm segment / Organic certified / Livestock", "AgriFarm", "Farm registry attributes"),
    ("Crop / Season", "hasAgriCrop / season", "Crop and harvest year"),
    ("Reporting status", "reportingStatus", "Why data is present or missing"),
    ("ML role", "derived", "train / review / predict, see README"),
    ("Crop area (ha)", "cropArea", "Reported area; for non-response cycles the declared parcel area"),
    ("Parcels", "hasAgriParcel", "Number of parcels declared for the crop that year"),
    ("Clay %, Slope %, Distance to water, Soil P, Buffer strip share, Main drainage, Main catchment", "AgriParcel", "Area-weighted over the declared parcels; catchment and drainage by largest area"),
    ("Organic soil share (soil map)", "organicSoilShare", "Share of the crop area on organic soil according to the soil map"),
    ("Irrigated", "irrigated", "Reported irrigation"),
    ("Delivered (t)", "deliveredQuantity", "SE Foodhills: tonnes delivered; yield is derived from it"),
] + [(f"{h}", a, f"Attribute to fill ({u})") for a, h, u in TARGETS] + [
    ("Current-calc footprint", "AgriFootprintResult …-currentcalc", "Apetit's current calculator, mineral soil assumed; FI survey cycles only"),
    ("Gaps (count)", "formula", "Number of blank attribute cells in the row"),
    ("Missing attributes", "missingAttributes", "Attributes the source did not report"),
    ("Quality flags", "dataQualityFlags", "Suspected errors; values are kept as reported"),
    ("Gaps_long: Filled value / Fill method / Fill confidence", "to be filled", "Empty columns for the ML step to write its result into"),
]
for i, row in enumerate(D, 2):
    for j, val in enumerate(row, 1):
        c = dc.cell(i, j, val); c.font = font; c.alignment = Alignment(wrap_text=True, vertical="top")
for i, wd in enumerate([48, 34, 70], 1): dc.column_dimensions[get_column_letter(i)].width = wd

wb.save(OUT)
print("rows", last - 1, "gaps", len(gaps_long), "flags", len(flags_rows), GAPC, ws[GAPC+"1"].value, FLAGC, ws[FLAGC+"1"].value, ws["X1"].value)
