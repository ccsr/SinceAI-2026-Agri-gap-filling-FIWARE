#!/usr/bin/env bash
# test_queries.sh - sample NGSI-LD queries against tenant apetit_ccn.
# Usage: bash scripts/test_queries.sh [broker-url]      (default http://localhost:1026)
# Needs curl and jq, and the ccn-context container (see README) so short names resolve.
B="${1:-http://localhost:1026}/ngsi-ld/v1"
T='NGSILD-Tenant: apetit_ccn'
L='Link: <http://ccn-context/context.jsonld>; rel="http://www.w3.org/ns/json-ld#context"; type="application/ld+json"'

count() {  # $1 = title, rest = --data-urlencode params
  local title="$1"; shift
  local n
  n=$(curl -sG -o /dev/null -D - -H "$T" -H "$L" "$B/entities" --data-urlencode count=true --data-urlencode limit=1 "$@" \
      | tr -d '\r' | awk -F': ' 'tolower($1)=="ngsild-results-count"{print $2}')
  printf '%-62s %8s\n' "$title" "${n:-ERROR}"
}
show() {   # $1 = title, rest = params; prints JSON
  echo; echo "── $1"; shift
  curl -sG -H "$T" -H "$L" -H 'Accept: application/json' "$B/entities" "$@" | jq "${JQ:-.}"
}

echo "== 1. Entity counts per type (expected in brackets)"
for t in "AgriSoil 5" "AgriCrop 10" "Catchment 42" "AgriFarm 680" "AgriParcel 14213" "AgriCropCycle 8448" "AgriFootprintResult 3437"; do
  set -- $t; count "$1 [$2]" --data-urlencode "type=$1"
done

echo; echo "== 2. Crop cycles by reporting status"
count "reported FI survey [3437]"             --data-urlencode type=AgriCropCycle --data-urlencode 'q=reportingStatus=="reported"'
count "Foodhills SE, partial [2683]"          --data-urlencode type=AgriCropCycle --data-urlencode 'q=reportingStatus=="reported (Foodhills grower file, partial)"'
count "not reported - gaps to fill [2328]"    --data-urlencode type=AgriCropCycle --data-urlencode 'q=reportingStatus=="not reported (survey non-response)"'

echo; echo "== 3. Data quality flags"
count "soil misreport suspected [195]"  --data-urlencode type=AgriCropCycle --data-urlencode 'q=dataQualityFlags=="soil_misreport_suspected:soil_map_organic_share>=30%"'
count "area mismatch [85]"              --data-urlencode type=AgriCropCycle --data-urlencode 'q=dataQualityFlags=="area_mismatch:cropArea_vs_parcel_declarations"'
count "decimal shift on N [44]"         --data-urlencode type=AgriCropCycle --data-urlencode 'q=dataQualityFlags=="decimal_shift_suspected:nitrogenMineralApplied"'
count "implausible yield [18]"          --data-urlencode type=AgriCropCycle --data-urlencode 'q=dataQualityFlags=="outlier_implausible:harvestedYield"'

echo; echo "== 4. Range / relationship filters"
count "pea cycles 2024 [420]"                            --data-urlencode type=AgriCropCycle --data-urlencode 'q=hasAgriCrop=="urn:ngsi-ld:AgriCrop:pea";season==2024'
count "cycles with mineral N > 150 kg/ha [49]"          --data-urlencode type=AgriCropCycle --data-urlencode 'q=nitrogenMineralApplied>150'
count "parcels on organic soil, soil map [653]"         --data-urlencode type=AgriParcel    --data-urlencode 'q=organicSoil==true'
count "parcels with clay >= 30 % and no buffer [4553]"    --data-urlencode type=AgriParcel    --data-urlencode 'q=clayContent>=30;bufferStrip==false'
count "footprints > 1000 kg CO2e/t [1303]"                --data-urlencode type=AgriFootprintResult --data-urlencode 'q=carbonFootprint>1000'
count "catchments in moderate/poor status [34]"         --data-urlencode type=Catchment    --data-urlencode 'q=ecologicalStatus=="moderate","poor"'

echo; echo "== 5. Geo: farms within 10 km of 22.536E 61.009N (farm F0001)"
count "farms within 10 km [21]" --data-urlencode type=AgriFarm --data-urlencode georel='near;maxDistance==10000' \
      --data-urlencode geometry=Point --data-urlencode 'coordinates=[22.53619,61.00931]'

echo; echo "== 6. Sample records"
JQ='.[] | {id, season, crop: .hasAgriCrop, n: .nitrogenMineralApplied, flags: .dataQualityFlags}' \
  show "two soil-misreport cycles (keyValues)" --data-urlencode type=AgriCropCycle \
       --data-urlencode 'q=dataQualityFlags=="soil_misreport_suspected:soil_map_organic_share>=30%"' \
       --data-urlencode options=keyValues --data-urlencode limit=2
JQ='.[] | {id, missing: .missingAttributes}' \
  show "one gap cycle and what it is missing" --data-urlencode type=AgriCropCycle \
       --data-urlencode 'q=reportingStatus=="not reported (survey non-response)"' --data-urlencode options=keyValues --data-urlencode limit=1
JQ='.' show "one farm" --data-urlencode type=AgriFarm --data-urlencode 'id=urn:ngsi-ld:AgriFarm:ccn:F0001' \
       --data-urlencode options=keyValues --data-urlencode 'attrs=name,farmSegment,contractCrops,organicCertified'
echo; echo "── footprint of F0001 pea 2016"
curl -s -H "$T" -H "$L" -H 'Accept: application/json' \
  "$B/entities/urn:ngsi-ld:AgriFootprintResult:ccn:F0001-pea-2016-currentcalc?options=keyValues" | jq
