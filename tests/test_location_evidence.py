from app.services import location_evidence as le
from app.services import parcel_roster as pr
from app.services import plss
from app.services.vision import parse_roster_locations

OCR = """WASHOE COUNTY SURVEY MONUMENT "N74SM01028" N:14872076.61 E：2257793.63
N:14834576.92 E:2289281.05 (GROUND COORDINATE)
NEVADA STATE PLANE COORDINATE SYSTEM OF 1983, WEST ZONE, DISTANCES SHOWN ARE GROUND
DISTANCES USING A PROJECT COMBINED GRID TO GROUND SCALE FACT0R OF 1.000197939.
A.P.N. 085-570-29 RENO INVESTMENT GROUP   APN 085-500-08 LAWS FAMILY TRUST
470 FOOTHILL ROAD, RENO, NEVADA"""

RAW = {
    "apns": [
        {"apn": "085-570-29", "role": "neighbour", "side": "n"},
        {"apn": "085 500 08", "role": "subject", "side": None},
        {"apn": "999-999-99", "role": "neighbour", "side": "S"},  # not printed: invented / misread
    ],
    "coordinates": [
        {"northing": 14872076.61, "easting": 2257793.63, "kind": "monument", "label": "N74SM01028"},
        {"northing": 14834576.92, "easting": 2289281.05, "kind": "parcel_corner", "label": "NE corner"},
        {"northing": 14800000.0, "easting": 2200000.0, "kind": "parcel_corner", "label": None},  # not printed
    ],
    "coordinate_system": {"zone": "Nevada West", "units": "us_survey_feet", "coordinates_are": "ground",
                          "combined_factor": 1.000197939},
    "plss": {"township": 18, "township_dir": "North", "range": 20, "range_dir": "East",
             "meridian": "Mount Diablo Meridian", "sections": [3], "aliquot": None},
    "address": "470 Foothill Road", "city": "Reno", "county": "Washoe", "state": "Nevada",
}


def test_sanitize_normalises_and_drops_junk():
    ev = le.sanitize_location(RAW)
    assert [a["apn"] for a in ev["apns"]] == ["085-570-29", "085-500-08", "999-999-99"]
    assert ev["apns"][0]["side"] == "N"
    assert ev["plss"]["township_dir"] == "N" and ev["plss"]["range_dir"] == "E"
    assert le.sanitize_location({"coordinate_system": {"combined_factor": 1.7}})["coordinate_system"]["combined_factor"] is None
    assert le.sanitize_location("garbage")["apns"] == []


def test_verify_keeps_only_what_the_ocr_text_prints():
    ev = le.verify(le.sanitize_location(RAW), OCR)
    assert [a["apn"] for a in ev["apns"]] == ["085-570-29", "085-500-08"]
    assert [c["label"] for c in ev["coordinates"]] == ["N74SM01028", "NE corner"]
    assert ev["coordinate_system"]["combined_factor"] == 1.000197939  # printed as "FACT0R OF 1.000197939"
    assert "APN 999-999-99" in ev["dropped"]
    assert ev["plss"]["vision_only"]  # T18N R20E is not in this OCR text
    assert "address" not in ev["vision_only"]


def test_helpers_feed_the_deterministic_readers():
    ev = le.merge([le.verify(le.sanitize_location(RAW), OCR)])
    assert le.subject_apns(ev) == ["085-500-08"]
    assert le.parcel_coordinate_pairs(ev) == [(14834576.92, 2289281.05)]  # the monument is not a parcel corner
    assert abs(le.ground_to_grid(ev) - 1 / 1.000197939) < 1e-12
    assert le.geocode_queries(ev) == ["470 Foothill Road, Reno, Nevada", "Washoe County, Nevada"]
    no_city = {**ev, "city": None}
    assert le.geocode_queries(no_city)[0] == "470 Foothill Road, Washoe County, Nevada"


def test_canonical_plss_text_parses():
    ev = {"plss": {"township": 13, "township_dir": "S", "range": 12, "range_dir": "E",
                   "meridian": "G&SRM", "sections": [21], "aliquot": "NE 1/4"}}
    trs = plss._find_trs(le.plss_text(ev))
    assert (trs.township_no, trs.township_dir, trs.range_no, trs.range_dir) == (13, "S", 12, "E")


def test_merge_prefers_a_subject_role_and_unions_sheets():
    a = {"apns": [{"apn": "1-2-3", "role": "neighbour", "side": "N"}], "coordinates": [], "coordinate_system": {}}
    b = {"apns": [{"apn": "1-2-3", "role": "subject", "side": None}], "coordinates": [],
         "coordinate_system": {"zone": "X"}, "state": "Nevada"}
    m = le.merge([a, None, b])
    assert m["apns"] == [{"apn": "1-2-3", "role": "subject", "side": None}]
    assert m["coordinate_system"]["zone"] == "X" and m["state"] == "Nevada"
    assert le.merge([None]) is None


def test_roster_area_unit_is_used_only_when_its_number_is_printed():
    assert pr.verified_area_sqft("±16,022 S.F.", 16022, "square_feet") == 16022
    assert abs(pr.verified_area_sqft("10.04± ACRES", 10.04, "acres") - 10.04 * 43560) < 1e-6
    assert pr.verified_area_sqft("16,022 S.F.", 16022, "acres") == 16022 * 43560  # unit is the model's call ...
    assert pr.verified_area_sqft("16,022 S.F.", 16220, "square_feet") is None  # ... the number is not
    out = pr.sanitize_roster([{"label": "PARCEL 1", "stated_area": "±16,022 S.F.", "area_value": 16022,
                               "area_unit": "square_feet", "point_x": 0.3, "point_y": 0.4}])
    assert out[0]["stated_area_sqft"] == 16022


def test_roster_payload_locations_are_parsed_per_image():
    payload = [{"image_number": 2, "parcels": [], "location": RAW}, {"image_number": 1, "parcels": []}]
    found = parse_roster_locations(payload, 2)
    assert list(found) == [1] and found[1]["county"] == "Washoe"


def test_surveyed_coordinates_try_parcel_corner_pairs_first():
    from app.services import georeference as g

    # only the far monument is printed with "N:/E:" labels the regex reads; the evidence names the corner
    pages = [{"regions": [{"ocr_text": 'MONUMENT N:14872076.61 E:2257793.63 NEVADA STATE PLANE WEST'}]}]
    lat, lon = g.find_surveyed_coordinates(
        pages, "Nevada", 39.4357, -119.7724, parcel_pairs=[(14834576.92, 2289281.05)],
        ground_to_grid=1 / 1.000197939,
    )
    assert abs(lat - 39.4362) < 0.002 and abs(lon + 119.7724) < 0.002
