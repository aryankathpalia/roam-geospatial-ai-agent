"""The placement-review agent's read-only tools, on a synthetic document (no network)."""
import math

from app.services import agent_tools as at

LAT, LON = 39.59, -119.777
KX = 111_320 * math.cos(math.radians(LAT))


def _square(cx_m, cy_m, half_m):
    pts = [(-half_m, -half_m), (half_m, -half_m), (half_m, half_m), (-half_m, half_m), (-half_m, -half_m)]
    return [[LON + (cx_m + x) / KX, LAT + (cy_m + y) / 110_540] for x, y in pts]


def _doc(neighbour_offset_m=0.0):
    parcel = {
        "human_confirmed": True, "roster_id": "p1",
        "vision_geometry": {"parcel_label": "PARCEL 1"},
        "boundary_geojson_wgs84": {"type": "Feature", "properties": {"georeferenced": "from_confirmed_boundary"},
                                   "geometry": {"type": "Polygon", "coordinates": [_square(0, 0, 20)]}},
        "calibration": {"status": "unverified", "scale_ft_per_px": 0.5, "notes": ["n1"]},
        "spatial_validation": {"stated_area_sqft": 17222.0, "area_matches_stated": True, "area_diff_pct": 0.5},
        "placement": {"status": "approximate", "notes": ["from the anchor"]},
    }
    # four 40 m neighbours around the parcel, shifted north by neighbour_offset_m
    neighbours = [{"apn": f"000-000-0{i}", "ring": _square(dx, dy + neighbour_offset_m, 20)}
                  for i, (dx, dy) in enumerate([(40, 0), (-40, 0), (0, 40), (0, -40)])]
    return {
        "anchor_lat": LAT, "anchor_lon": LON, "anchor": {"precision": "surveyed", "source": "printed"},
        "apn_site": {"source": "test", "target_apn": None, "target": None, "neighbours": neighbours},
        "pages": [{
            "page_number": 3,
            "sheet": {"role": "target_parcel_map", "parcels": [{"id": "p1", "stated_area": "17,222 SQ. FT."}],
                      "location_evidence": {
                          "apns": [], "coordinates": [{"northing": 14887373.87, "easting": 2288444.92, "kind": "monument"}],
                          "coordinate_system": {"coordinates_are": "ground", "combined_factor": 1.000197938}}},
            "regions": [{"class": "ParcelMap", "ocr_text": "N 14887373.87 E 2288444.92 COMBINED FACTOR OF 1.0001973",
                         "parcels": [parcel]}],
        }],
    }


def test_overview_and_details_describe_the_confirmed_parcel():
    doc = _doc()
    over = at.run_tool(doc, "get_document_overview", {})
    p = over["sheets"][0]["parcels"][0]
    assert p["label"] == "PARCEL 1" and abs(p["area_acres_on_map"] - 0.395) < 0.005 and over["anchor"]["precision"] == "surveyed"
    det = at.run_tool(doc, "get_parcel_details", {"page_number": 3, "label": "parcel 1"})
    assert det["corners"] == 4 and det["extent_m"] == [40.0, 40.0] and det["stated_area_as_printed"] == "17,222 SQ. FT."


def test_evaluate_position_finds_the_shift_that_seats_the_parcel():
    doc = _doc(neighbour_offset_m=-300.0)  # the neighbours are 300 m south of where the parcel was placed
    here = at.run_tool(doc, "evaluate_position", {"page_number": 3})
    there = at.run_tool(doc, "evaluate_position", {"page_number": 3, "north_m": -300.0})
    assert here["outline_hugging_a_neighbour_pct"] == 0 and here["nearest_neighbour_edge_m"] > 200
    assert there["overlap_with_neighbours_pct"] < 1 and there["outline_hugging_a_neighbour_pct"] > 90


def test_evidence_keeps_a_garbled_factor_and_converts_ground_coordinates():
    doc = _doc()
    ev = at.run_tool(doc, "get_location_evidence", {"page_number": 3})
    assert ev["coordinate_system"]["combined_factor"] == 1.000197938 and ev["dropped_by_ocr_check"] == []
    raw = at.run_tool(doc, "convert_state_plane", {"northing": 14887373.87, "easting": 2288444.92, "state": "Nevada"})
    grid = at.run_tool(doc, "convert_state_plane", {"northing": 14887373.87, "easting": 2288444.92, "state": "Nevada",
                                                    "ground_to_grid": ev["ground_to_grid_multiplier_used"]})
    assert raw["conversions"][0]["epsg"] == 3423 and all("+" not in c["zone"] for c in raw["conversions"])
    moved = (raw["conversions"][0]["lat"] - grid["conversions"][0]["lat"]) * 110_540
    assert 850 < moved < 950  # the ~900 m a dropped ground factor costs on a Washoe sheet


def test_text_search_and_errors():
    doc = _doc()
    hits = at.run_tool(doc, "search_document_text", {"pattern": "FACT[O0]R"})["hits"]
    assert hits and "1.0001973" in hits[0]["context"]
    assert "error" in at.run_tool(doc, "no_such_tool", {})
    assert "error" in at.run_tool(doc, "evaluate_position", {"page": 3})
    assert "error" in at.run_tool(doc, "get_parcel_details", {"page_number": 3, "label": "PARCEL 9"})
    names = [t["function"]["name"] for t in at.tool_specs()]
    assert set(names) == set(at.TOOLS) and all(t["type"] == "function" for t in at.tool_specs())
