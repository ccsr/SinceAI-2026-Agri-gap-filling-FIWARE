# apetit-ccn-ngsi-ld

**A FIWARE NGSI-LD stack with an ML gap-filling service for agri-food LCA data: reported data and model estimates side by side in the context broker, each value tagged with its origin and confidence.**

It takes the farming part of the *Apetit Climate & Nature Navigator 2026* dataset (a synthetic twin of a Finnish food company's contract-farming data, published by Since AI ry under CC BY 4.0), turns it into NGSI-LD entities based on the Smart Data Models Agrifood models, loads it into an NGSI-LD context broker, and fills the gaps in the farm activity data that a crop footprint (LCA) needs.

```
source CSVs ──convert──► NGSI-LD entities ──load──► tenant apetit_ccn            (as reported, never changed)
                                                     │ copy
                                                     ▼
                                       tenant apetit_ccn_post_ml ◄──read / update── gapfill container
                                                     │                                 (pandas, scikit-learn)
                                                     └── export ──► HESTIA container (emissions, optional)
```

The repository contains **code, data model and documentation only**. No data is included; see [Get the data](#get-the-data).

## What the gap filling does

Farm surveys are incomplete: in the source data, 2,328 Finnish contract-crop cycles have no survey answer at all, 2,683 Swedish cycles report only N, diesel and yield, and 331 reported cycles contain values that look wrong. The gap fill works in layers, most reliable first, and every value it writes says how it was obtained.

| Layer | What | Confidence |
|---|---|---|
| **derived** | soil type from the soil map; yield from ERP delivery records divided by declared area (after fixing unit errors, duplicates and decimal shifts in the ERP lines) | high |
| **corrected** | flagged values: decimal-shifted N, implausible yield, soil type contradicted by the soil map, crop area that does not match the declared parcels. The reported value is kept as `reportedValue`. | medium / high |
| **imputed** | cycles with no answer: farm history (same farm and crop, other years), gradient boosting, or the crop-and-region mean, **chosen per attribute by 5-fold cross-validation** on the clean survey cycles | medium |
| **imputed (transfer)** | Swedish cycles: a model trained on Finnish pea cycles using the Swedish values that are known; chosen against the Finnish mean by grouped cross-validation | low |

Each filled NGSI-LD attribute carries `dataOrigin`, `imputationMethod`, `confidence`, `predictionInterval` (p10–p90 from cross-validated residuals) and `gapFillVersion`. The crop cycle gets `filledAttributes`, `correctedAttributes`, `gapFillStatus` and `gapFillRun`. `missingAttributes` is never changed: it records what was not reported. Re-running the gap fill is safe; values written by an earlier run are not used as inputs.

Example of a filled attribute:

```json
"nitrogenMineralApplied": {
  "type": "Property", "value": 25.622, "unitText": "kg N/ha",
  "dataOrigin": "imputed",
  "imputationMethod": "imputed: farm history (same farm and crop, other years)",
  "confidence": "medium",
  "predictionInterval": [17.251, 34.297],
  "gapFillVersion": "gapfill-ccn 1.0"
}
```

Results on the Navigator data (hidden-value test on 3,106 clean Finnish cycles; skill = error reduction against the crop-and-region mean):

| Attribute | Method chosen | Skill |
|---|---|---|
| Mineral N | farm history | 62 % |
| Manure N | farm history | 99 % |
| Yield | gradient boosting | 31 % (ERP-derived yield: 99 % within 10 %) |
| Spray passes | gradient boosting | 25 % |
| P, diesel, lime, catch crops | gradient boosting / mean | ≈ 0 % – no usable signal, the unbiased mean is used |
| Soil type | soil map | 99.5 % agreement |
| Fertiliser product | most common for crop | 24 % accuracy – not predictable from this data |

The Swedish values have no ground truth and are not anchored to Swedish statistics; treat them as placeholders.

## Repository layout

```
data-model/        NGSI-LD @context, data model description, JSON schemas (AgriCropCycle, AgriFootprintResult)
scripts/           convert, load, copy, query, delete, Excel export (standard library + openpyxl)
gapfill/           gap-filling service: gapfill_ccn.py, Excel report, Dockerfile, docker-compose.yml
hestia/            optional: run the HESTIA engine (hestia.earth) on the crop cycles
tests/             a small in-memory NGSI-LD test broker and a smoke test
source-data/       put the source CSVs here (not in git, see source-data/README.md)
```

## Requirements

- An NGSI-LD context broker (ETSI GS CIM 009), see [Context broker](#context-broker).
- Python 3.10+ for the scripts in `scripts/` (standard library; `openpyxl` for `build_pre_ml.py`).
- Docker for the gap-filling and HESTIA containers.

## Context broker

Developed and tested with **[coraine](https://coraine.readthedocs.io/) 0.7.0** (Seamware, Apache-2.0): a lightweight NGSI-LD context broker written in C that implements ETSI GS CIM 009 v1.9.1. It ran as a Docker container with the in-memory store and the admin plugin:

```bash
docker run -d --name coraine -p 1026:1026 quay.io/seamware/coraine:0.7.0 \
  --database corDB --apiPlugins admin
```

| Setting | Value |
|---|---|
| Image | `quay.io/seamware/coraine:0.7.0` |
| API | NGSI-LD, port 1026 |
| Store | `corDB` (in memory: data is lost on restart; use `--database mongoc` with MongoDB for persistence) |
| Multi-tenancy | `NGSILD-Tenant` header; tenants `apetit_ccn` (as reported) and `apetit_ccn_post_ml` (gap filled) |
| @context | sent inline with every write (`data-model/context.jsonld` + NGSI-LD core context v1.8) |

The scripts use only standard NGSI-LD operations (batch upsert / update / delete, paged entity queries, `/types`), so other NGSI-LD brokers such as the FIWARE Generic Enablers **Orion-LD**, **Scorpio** or **Stellio** should work by changing `--broker` (and `network_mode` in `gapfill/docker-compose.yml`). They have not been tested here. Behaviour that differs between brokers and is handled in the code: result order, one-item arrays returned as plain values, and sub-attributes stored as Properties.

## Get the data

Download the *Apetit Climate & Nature Navigator 2026* package (Since AI ry, CC BY 4.0) and copy the files listed in [`source-data/README.md`](source-data/README.md) into `source-data/`. Only the synthetic CC BY files are used. The package's Apetit-originated workbook (`ApetitGroup_sample_data.xlsx`) is not used and must not be added to this repository.

## Run

```bash
# 1. convert the CSVs to NGSI-LD (writes entities/ccn/, 26,835 entities)
python3 scripts/convert_ccn_to_ngsild.py

# 2. load into tenant apetit_ccn (as reported)
python3 scripts/load_ccn.py --broker http://localhost:1026

# 3. optional: test queries and an Excel overview of the gaps
bash scripts/test_queries.sh
python3 scripts/build_pre_ml.py entities/ccn "Pre ML data.xlsx"

# 4. copy to the working tenant and fill the gaps
python3 scripts/copy_tenant.py                       # apetit_ccn -> apetit_ccn_post_ml
docker compose -f gapfill/docker-compose.yml build
docker compose -f gapfill/docker-compose.yml run --rm gapfill --dry-run   # fill + validate + Excel, no writes
docker compose -f gapfill/docker-compose.yml run --rm gapfill             # ... and update the entities
```

The gap-fill container joins the broker container's network (`network_mode: container:coraine` in `gapfill/docker-compose.yml`; change the name if your broker container is called differently, or pass `--broker`). It writes `results/gapfill/`: `fills.csv` (one row per filled value), `validation.csv` (cross-validation of every method) and `Post ML data.xlsx`.

Other tools:

| Script | Purpose |
|---|---|
| `scripts/copy_tenant.py` | copy every entity of one tenant to another (`--source`, `--target`, `--dry-run`) |
| `scripts/delete_tenant.py` | delete every entity in one tenant (asks for confirmation; `--yes`, `--dry-run`) |
| `scripts/test_queries.sh` | counts per type, status, quality flag; range, relationship and geo queries |
| `hestia/` + `scripts/export_ccn_hestia.py`, `scripts/hestia_writeback.py` | run HESTIA on the crop cycles and write `AgriFootprintResult` entities back |

NGSI-LD has no API to delete a tenant itself. `delete_tenant.py` empties it; with an in-memory broker, a restart removes all tenants.

### Query with short names

Entities are loaded with an inline `@context`. To query with short attribute names, serve `data-model/context.jsonld` where the broker can reach it and send it as a `Link` header:

```bash
NET=$(docker inspect coraine --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}{{end}}')
docker run -d --name ccn-context --network "$NET" -v "$PWD/data-model":/srv:ro python:3.11-slim \
  python -c "import http.server as h,functools; h.SimpleHTTPRequestHandler.extensions_map['.jsonld']='application/ld+json'; h.ThreadingHTTPServer(('',80),functools.partial(h.SimpleHTTPRequestHandler,directory='/srv')).serve_forever()"
L='Link: <http://ccn-context/context.jsonld>; rel="http://www.w3.org/ns/json-ld#context"; type="application/ld+json"'
curl -sG -H 'NGSILD-Tenant: apetit_ccn_post_ml' -H "$L" http://localhost:1026/ngsi-ld/v1/entities \
  --data-urlencode 'type=AgriCropCycle' --data-urlencode 'q=gapFillStatus=="filled"' --data-urlencode 'limit=2'
```

## Data model

Smart Data Models Agrifood (`AgriFarm`, `AgriParcel`, `AgriSoil`, `AgriCrop`) plus an `alca:` namespace (`https://uri.fiware.org/ns/data-models/agrifood-lca/`) for `AgriCropCycle`, `AgriFootprintResult` and `Catchment`. See [`data-model/DATA_MODEL.md`](data-model/DATA_MODEL.md).

| Type | Count (Navigator data) |
|---|---|
| AgriSoil / AgriCrop / Catchment | 5 / 10 / 42 |
| AgriFarm | 680 |
| AgriParcel | 14,213 (polygons, soil map, crop history 2012–2026 and plan 2027–2031) |
| AgriCropCycle | 8,448 (farm × crop × year) |
| AgriFootprintResult | 3,437 (current calculator) |

## Testing without a broker

`tests/mock_broker.py` is a small in-memory NGSI-LD broker that behaves like a real one where it matters here (tenants, batch operations, paging, one-item arrays returned as plain values, sub-attributes stored as Properties, arbitrary result order).

```bash
bash tests/smoke_test.sh        # convert, load, copy, gap fill twice, check the results are identical
```

## Licence

Code and data model: MIT, see [`LICENSE`](LICENSE). The Navigator data is © Since AI ry, CC BY 4.0, and is not part of this repository.
