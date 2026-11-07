# source-data

Put the source files here. They are **not** in git (see `.gitignore`).

Source: *Apetit Climate & Nature Navigator 2026* package, © Since AI ry, licensed CC BY 4.0
("Apetit Climate & Nature Navigator synthetic dataset, Since AI ry 2026"). It is a synthetic twin;
it contains no real farm or company data.

Copy these files from the package (flat, no sub-folders):

| File | From the package folder | Used by |
|---|---|---|
| `farm_registry.csv` | `data/01_master/` | convert |
| `parcels.csv` | `data/01_master/` | convert |
| `parcels.geojson` | `data/06_nature/` | convert (parcel polygons) |
| `catchments.csv` | `data/06_nature/` | convert |
| `catchments.geojson` | `data/06_nature/` | convert |
| `monitored_catchment_loads.csv` | `data/06_nature/` | convert |
| `farm_survey_fi.csv` | `data/03_farming/` | convert |
| `foodhills_odlingsdata_se.csv` | `data/03_farming/` | convert |
| `parcel_crop_history_and_plan.csv` | `data/03_farming/` | convert |
| `current_calculator_footprints.csv` | `data/03_farming/` | convert |
| `erp_purchase_lines_2021_2025.csv` | `data/02_raw_feeds/` | gap fill (yield from deliveries) |

Do **not** add `data/00_apetit_original/ApetitGroup_sample_data.xlsx`: it is not CC BY and is not used.
