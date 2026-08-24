# Endpoints personally verified 2026-08-19

## DeepStateMap (deepstatemap.live) — CONFIRMED WORKING, no auth
- GET https://deepstatemap.live/api/history/public
  -> JSON array, 1732 records (2022-04-03 .. 2026-08-19).
     Fields: id, description (UA, contains <a> links to map coords), descriptionEn, updatedAt, datetime, status, createdAt
- GET https://deepstatemap.live/api/history/last
  -> {id, map: FeatureCollection} ~628 KB, current occupied-area polygons
- GET https://deepstatemap.live/api/history/{id}/geojson
  -> FeatureCollection for that historical snapshot (full time series retrievable)
- GET https://deepstatemap.live/api/history          -> 401 Unauthorized
- GET https://deepstatemap.live/api/history/last/geojson -> 404 (does NOT exist)
Coordinates are lon,lat,0 triplets. Polygons = occupied territory, so frontline = polygon boundary.

## Alert APIs
- https://api.alerts.in.ua        -> 502 on bare host (needs versioned path + token)
- https://api.ukrainealarm.com    -> 403 on bare host (needs API key header)

## ISW / Critical Threats Project ArcGIS — CONFIRMED PUBLIC, no auth
Base: https://services5.arcgis.com/SaBe5HMtmnbqSWlu/arcgis/rest/services
- GET {base}?f=json -> 324 services enumerable (Ukraine, Israel/Gaza, Syria, Sahel, Africa)
Verified GeoJSON queries (append: /query?where=1%3D1&outFields=*&returnGeometry=true&f=geojson):
- VIEW_RussiaCoTinUkraine_V3/FeatureServer/49            -> Polygon, props OBJECTID/GlobalID_2/EditDate  (Assessed Russian Control of Terrain)
- Assessed_Russian_Gains_in_the_Past_24_Hours_view/FeatureServer/0 -> Polygon  (24h gains)
- RUAF_Field_Fortifications_Polylines/FeatureServer/0    -> LineString, props FID/Name/FolderPath  (Brady Africk fortifications)
Also present: VIEW_ClaimedRussianTerritoryinUkraine_V2, AssessedRussianAdvanceInUkraine_V2_view,
Claimed_Limit_of_Ukrainian_Advance_VIEW, Claimed_Russian_Advances_in_Russia_View,
Bryansk__Kursk__Belgorod__and_Voronezh_Settlements_view, monthly snapshots + timelapse layers.
NOTE: EditDate field enables change detection / diffing between polls.
Standard ArcGIS REST params apply (resultRecordCount, resultOffset, outSR, geometry filters, f=geojson|json|pbf).

## WarSpotting (ukr.warspotting.net) — CONFIRMED WORKING, no auth
Real path pattern is /api/losses/<side>/... (NOT /api/losses/?date=)
- GET /api/losses/russia/recent/            -> {"losses":[...]} 100 items, HTTP 200
- GET /api/losses/russia/<YYYY-MM-DD>/      -> losses confirmed for that date
- GET /api/losses/russia/<YYYY-MM-DD>/<status>/   status in destroyed|damaged|abandoned|captured
- GET /api/losses/russia/<limit>/           e.g. /10000/  (and /10000/abandoned/)
- GET /api/stats/russia/                    -> counts_by_status + counts_by_type aggregates
Item fields: id, type, model, status, lost_by, date, nearest_location, geo ("lat,lon"), unit, tags
=> geolocated point events with real coordinates. Ukrainian-side equivalent path is /api/losses/ukraine/...
CAVEAT: /api/losses/russia/2026-08-15/ returned 0 losses while /recent/ had entries dated 2026-08-19,
so per-date confirmation backfills with a lag — poll /recent/ AND re-poll past dates.
Docs page /api/docs/ is 200 to curl but 403 to WebFetch; it is a JS/Leaflet page, endpoints are in its HTML.

## ISW RSS — DOES NOT EXIST (correcting a common claim)
- https://understandingwar.org/feed/ , /feed , /?feed=rss2  -> all 200 but redirect to homepage HTML, 0 <item>
- /rss.xml -> 403 ; /publications/feed/ , /backgrounder/feed/ -> 404
No <link type="application/rss+xml"> declared on the homepage. ISW text must be scraped from HTML,
or consumed via its ArcGIS layers (above). Do NOT rely on an ISW RSS feed.

## OSW (Centre for Eastern Studies, Poland) — CONFIRMED WORKING
- GET https://www.osw.waw.pl/en/rss.xml -> 200, populated; carries the numbered daily
  "Day N of the war" frontline analysis (Day 1637 on 2026-08-19). Best non-ISW daily written product.

## GeoConfirmed — FULL OpenAPI SPEC FOUND, no auth (verified 2026-08-19)
Spec: https://geoconfirmed.org/openapi/v1.json   (NOT /swagger/v1/swagger.json, which 404s)
Title "Geoconfirmed API", OpenAPI 3.1.1. Conflict codes from GET /api/Conflict (20 conflicts; Ukraine=UKR).

CORRECTION: /api/placemark/Ukraine returns the FACTION+ICON TAXONOMY (11 records, ~6.9MB of embedded
icon PNGs) — it is NOT the placemark data. The real data endpoints are:

- GET /api/Placemark/{conflict}/geojson   -> 16 MB; {"factionMeta":[...], "geojson":{FeatureCollection}}
    59,359 Point features, dates 2014-03-03 .. 2026-08-19
    feature.properties: id, icon, factionId, color, date, dateSort
- GET /api/Map/export/{conflict}/csv      -> 31 MB, 59,337 rows, SEMICOLON-delimited, UTF-8 BOM
    columns: Date;Name;Faction;Origin;Latitude;Longitude;PlusCode;Description;Source;Geolocation;
             Equipment;EquipmentItems;Units;OrbatUnits;Id
    ** richest single feed found: coords + human description + source URL + unit attribution **
- GET /api/Placemark/detail/{id}          -> per-event: name, description, lat, lon, faction, origin
    (UAV/SAT/etc), gear, units, plusCode, originalSource, geolocation, orbatUnits[]
- GET /api/Map/export/{conflict}          (non-csv export)
- GET /api/Map/networklink/{conflict}     (KML NetworkLink)
- POST /api/Placemark/filter , POST /api/Placemark/{conflict}/table   (server-side filter/paging)
- GET /api/Placemark/{conflict}/icons , /orbats , /orbats/search
- GET /api/Orbat , /api/Orbat/{urlName} , /api/OrbatNode/{id}/placemarks   (order-of-battle tree)
- GET /api/Gear , /api/Gear/metadata , /api/Gear/search                    (equipment taxonomy)
- GET /api/Stats/home ; POST /api/Token
Site is Blazor WebAssembly, so API calls are not discoverable in JS — use the openapi spec.

## Second independent frontline-area time series — CONFIRMED
- https://raw.githubusercontent.com/conflict-investigations/nzz-maps/master/territory.csv
  1,638 daily rows "date,area" (occupied km2), 2022-02-24 .. 2026-08-19. Branch is master, not main.
  Useful as a cross-check against DeepStateMap-derived area figures.

## ACLED — BREAKING CHANGE CONFIRMED
- api.acleddata.com does not resolve at all (curl exit, HTTP 000). Old key-in-querystring API is gone.
  Current: POST https://acleddata.com/oauth/token then Bearer on https://acleddata.com/api/acled/read
  Key-free fallback: HDX ACLED Ukraine mirror (admin2 / monthly aggregates).
