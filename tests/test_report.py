import io
import json
import zipfile

import pymupdf
import shapefile

from app.services import report as rp
from app.services import report_export as rx

LAT0, LON0 = 39.3789, -119.7275


def _ring(dx, dy, w, h):
    kx, ky = 85_900, 110_540
    pts = [(dx, dy), (dx + w, dy), (dx + w, dy + h), (dx, dy + h), (dx, dy)]
    return [[LON0 + x / kx, LAT0 + y / ky] for x, y in pts]


def _result():
    def parcel(rid, ring, stated_sqft, apn_fit):
        return {
            "roster_id": rid, "human_confirmed": True,
            "boundary_geojson_wgs84": {"type": "Feature", "properties": {"closure_error_ft": 0.0},
                                       "geometry": {"type": "Polygon", "coordinates": [ring]}},
            "spatial_validation": {"stated_area_sqft": stated_sqft, "area_matches_stated": True, "area_diff_pct": 0.4,
                                   "self_intersects": False},
            "calibration": {"status": "cross_validated"},
            "placement": {"status": "approximate", "notes": ["n"], "apn_fit": apn_fit},
            "vision_geometry": {"boundary_calls": [{"bearing": "N00°18'55\"W", "distance": "162.59'"}]},
        }
    return {
        "inspection": {"filename": "original.pdf", "page_count": 3, "metadata": {"title": "Test TPM.pdf"}},
        "anchor": {"precision": "surveyed", "source": "state-plane coordinate printed on the document"},
        "location_evidence": {"county": "WASHOE", "state": "NEVADA"},
        "apn_site": {"source": "Washoe County, NV assessor parcels", "target_apn": "017-150-35",
                     "target": {"apn": "017-150-35", "owner": "ROBINSON", "address": "1325 BIG SMOKEY DR", "ring": []},
                     "neighbours": [{"apn": "017-150-59", "owner": "GREEN", "address": "15690 ROCKY VISTA RD", "ring": []}]},
        "pages": [
            {"page_number": 14, "sheet": {"parcels": [
                {"id": "p14-1", "label": "PARCEL AB-1", "stated_area": "25,630 ± SQ. FT.", "stated_area_sqft": 25630},
                {"id": "p14-2", "label": "PARCEL AB-2", "stated_area": "17,409 ± SQ. FT.", "stated_area_sqft": 17409},
            ]}, "regions": [{"parcels": [
                parcel("p14-1", _ring(0, 0, 50, 48), 25630, {"corroborated": True}),
                parcel("p14-2", _ring(50, 0, 40, 40), 17409, {"corroborated": False, "improves": True}),
            ]}]},
        ],
    }


def test_report_model_has_what_a_deliverable_needs():
    r = rp.build_report(_result(), "doc1")
    assert r["project"]["title"] == "Test TPM.pdf" and r["project"]["county"] == "WASHOE"
    assert r["crs"]["state_plane"] == "NAD83 / Nevada West (ftUS) (EPSG:3423)"
    assert "UTM zone 11N" in r["crs"]["utm"]
    a, b = r["parcels"]
    assert a["label"] == "PARCEL AB-1" and a["stated_area_sqft"] == 25630 and a["parent_apn"] == "017-150-35"
    assert a["location_confirmed"] and a["placement_method"] == "apn"
    assert not b["location_confirmed"] and b["placement_method"] == "apn_approx"
    assert abs(a["area_sqft"] - 50 * 48 / 0.09290304) / a["area_sqft"] < 0.01
    assert len(a["vertices"]) == 4 and {"lat", "lon", "sp_n", "sp_e", "utm_n", "utm_e"} <= set(a["vertices"][0])
    assert a["calls"][0]["bearing"].startswith("N00")
    assert r["summary"]["parcel_count"] == 2 and r["summary"]["confirmed_locations"] == 1
    assert r["location"]["neighbours"][0]["owner"] == "GREEN"


def test_edits_are_validated_logged_and_applied():
    edits = rp.merge_edits({}, {
        "project": {"client": "Acme GIS", "document_id": "hacked"},
        "parcels": {"p14-1": {"review_status": "approved", "notes": "ok", "area_sqft": "1"},
                    "p14-2": {"review_status": "bogus"}},
    }, "rajesh")
    assert edits["project"] == {"client": "Acme GIS"}
    assert edits["parcels"] == {"p14-1": {"review_status": "approved", "notes": "ok"}}
    assert [e["field"] for e in edits["log"]] == ["project.client", "p14-1.review_status", "p14-1.notes"]
    assert all(e["by"] == "rajesh" for e in edits["log"])
    res = _result()
    res["report_edits"] = edits
    r = rp.build_report(res, "doc1")
    assert r["project"]["client"] == "Acme GIS" and r["parcels"][0]["review_status"] == "approved"
    assert r["summary"]["approved"] == 1 and len(r["edit_log"]) == 3
    assert rp.merge_edits(edits, {"project": {"client": "Acme GIS"}})["log"] == edits["log"]  # no-op edit not logged


def test_vector_exports():
    r = rp.build_report(_result(), "doc1")
    fc = json.loads(rx.geojson(r))
    assert len(fc["features"]) == 2 and fc["features"][0]["properties"]["ParcelID"] == "p14-1"
    assert fc["features"][0]["properties"]["LocConf"] == "confirmed"
    assert b"<Placemark>" in rx.kml(r) and b"PARCEL AB-2" in rx.kml(r)
    for label, first_x in ((None, -119.7), (r["crs"]["state_plane"], 2_000_000)):
        z = zipfile.ZipFile(io.BytesIO(rx.shapefile_zip(r, label)))
        assert set(z.namelist()) == {"parcels.shp", "parcels.shx", "parcels.dbf", "parcels.prj", "parcels.cpg"}
        rd = shapefile.Reader(shp=io.BytesIO(z.read("parcels.shp")), shx=io.BytesIO(z.read("parcels.shx")),
                              dbf=io.BytesIO(z.read("parcels.dbf")))
        assert len(rd) == 2 and rd.record(0)["ParcelID"] == "p14-1"
        assert (rd.shape(0).points[0][0] > first_x) if label else abs(rd.shape(0).points[0][0] - first_x) < 0.1
    assert rx.attributes_csv(r).decode("utf-8-sig").startswith("ParcelID,Label,APN")
    assert rx.vertices_csv(r).count(b"\n") == 1 + 8


def test_pdf_renders_every_section_without_network():
    r = rp.build_report(_result(), "doc1")
    doc = pymupdf.open("pdf", rx.pdf(r, figs={}))
    text = "".join(p.get_text() for p in doc)
    for heading in ("Summary", "Parcel schedule", "Georeferencing", "PARCEL AB-1", "Vertex coordinates",
                    "Quality checks", "Accuracy statement"):
        assert heading in text, heading
    assert "Page 1 of" in text
