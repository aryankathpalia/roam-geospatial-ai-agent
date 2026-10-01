"""Parcel roster: sanitizing, the evidence join, the printed-id safeguard, user-data carry-over."""
from app.services import parcel_roster as pr


def _roster(*items):
    return pr.sanitize_roster(list(items))


# What the roster call returns for the Patnaude sheet (page 7).
PATNAUDE = [
    {"label": "Remainder Parcel", "printed_id": "17-2-1-4", "stated_area": "120.4 AC.±", "point_x": 0.3, "point_y": 0.6},
    {"label": "Parcel 1", "printed_id": None, "stated_area": "40.00 AC.±", "point_x": 0.7, "point_y": 0.6},
]


def test_sanitize_keeps_only_what_the_model_actually_printed():
    out = pr.sanitize_roster(PATNAUDE + [
        {"label": "parcel 1", "printed_id": "17-2-1-3"},              # duplicate label -> dropped
        {"label": "  ", "printed_id": "x"},                            # no label -> dropped
        {"label": "Lot 9", "printed_id": "null", "stated_area": "", "point_x": 5, "point_y": 0.2},
        "junk",
    ])
    assert [e["label"] for e in out] == ["Remainder Parcel", "Parcel 1", "Lot 9"]
    assert out[0]["printed_id"] == "17-2-1-4" and out[1]["printed_id"] is None
    assert out[2]["printed_id"] is None and out[2]["stated_area"] is None and out[2]["point"] is None  # 5 is off the page


def test_parse_acres():
    assert pr.parse_acres("120.4 AC.±") == 120.4 and pr.parse_acres("[40.00 AC.±]") == 40.0 and pr.parse_acres("1.78") == 1.78
    assert abs(pr.parse_acres("43,560 SF") - 1.0) < 1e-9 and pr.parse_acres(None) is None and pr.parse_acres("n/a") is None


def _entities(page=7):
    out = []
    for r in pr.sanitize_roster(PATNAUDE):
        out.append(pr.new_entity(page, out, label=r["label"], region_index=0, source="roster",
                                 printed_id=r["printed_id"], stated_area=r["stated_area"], point=r["point"]))
    return out


def _region(*labels_areas):
    return {"class": "ParcelMap", "parcels": [{"vision_geometry": {"parcel_label": l, "stated_area_acres": a}} for l, a in labels_areas]}


def test_roster_entities_get_stable_distinct_ids_and_their_own_polygon_slots():
    a, b = _entities()
    assert (a["id"], b["id"]) == ("p7-1", "p7-2") and a["confirmed_polygon"] is None and b["confirmed_polygon"] is None
    assert a["printed_id"] == "17-2-1-4" and b["printed_id"] is None  # Parcel 1 has no printed id: never inferred


def test_evidence_join_matches_by_label_and_never_confuses_parcel_1_with_the_remainder():
    sheet = {"regions": [0], "inset_regions": [], "parcels": _entities()}
    # extraction reads the labels the way the plat prints them, in the opposite order
    regions = [_region(("PARCEL 1", "40.00"), ("REMAINDER PARCEL 17-2-1-4", "120.4"))]
    pr.join_evidence(7, sheet, regions)
    rem, p1 = sheet["parcels"]
    assert p1["evidence_ref"] == {"region": 0, "parcel": 0} and rem["evidence_ref"] == {"region": 0, "parcel": 1}
    assert regions[0]["parcels"][0]["roster_id"] == p1["id"] and regions[0]["parcels"][1]["roster_id"] == rem["id"]
    assert len(sheet["parcels"]) == 2  # nothing duplicated


def test_join_falls_back_to_stated_area_when_labels_do_not_agree():
    sheet = {"regions": [0], "inset_regions": [], "parcels": _entities()}
    regions = [_region(("TRACT A", "120.4"), ("TRACT B", "40.0"))]
    pr.join_evidence(7, sheet, regions)
    rem, p1 = sheet["parcels"]
    assert rem["evidence_ref"]["parcel"] == 0 and p1["evidence_ref"]["parcel"] == 1


def test_a_conflicting_area_blocks_a_label_only_match_from_being_trusted():
    sheet = {"regions": [0], "inset_regions": [], "parcels": _entities()[1:]}      # Parcel 1, 40 ac
    regions = [_region(("PARCEL 1", "2.78"))]                                       # same label, wildly different area
    pr.join_evidence(7, sheet, regions)
    assert sheet["parcels"][0]["evidence_ref"] is None                              # not attached
    assert [e["source"] for e in sheet["parcels"]] == ["roster", "extraction"]      # extraction kept as its own entity


def test_unmatched_extraction_becomes_its_own_entity_and_unmatched_roster_keeps_its_outline():
    sheet = {"regions": [0], "inset_regions": [1], "parcels": _entities()}
    regions = [_region(("OUTLOT Z", "3.0")), _region(("VICINITY BLOB", None))]
    pr.join_evidence(7, sheet, regions)
    assert all(e["evidence_ref"] is None for e in sheet["parcels"][:2])             # roster entities untouched
    extra = sheet["parcels"][2:]
    assert [e["label"] for e in extra] == ["OUTLOT Z", "VICINITY BLOB"] and all(e["source"] == "extraction" for e in extra)
    assert "excluded_reason" not in extra[0] and "inset" in extra[1]["excluded_reason"]


def test_manually_named_parcel_joins_by_label_and_stays_marked_manual():
    entities = []
    entities.append(pr.new_entity(7, entities, label="Parcel 1", region_index=0, source="manual", manual=True))
    sheet = {"regions": [0], "inset_regions": [], "parcels": entities}
    pr.join_evidence(7, sheet, [_region(("PARCEL 1", "40.00"))])
    assert entities[0]["evidence_ref"] == {"region": 0, "parcel": 0} and entities[0]["manual"] is True and entities[0]["id"] == "p7-m1"


def test_printed_id_the_sheet_does_not_contain_is_dropped():
    es = _entities()
    es[1]["printed_id"] = "17-2-1-3"                      # a model borrowing the 1976 survey's parcel number
    pr.verify_printed_ids(es, "REMAINDER PARCEL 17-2-1-4 [120.4 AC.±] PARCEL 1 [40.00 AC.±] A DIVISION OF PARCEL 17-2-1-4")
    assert es[0]["printed_id"] == "17-2-1-4" and es[1]["printed_id"] is None and es[1]["printed_id_dropped"] == "17-2-1-3"
    es2 = _entities()
    pr.verify_printed_ids(es2, "")                         # no OCR text yet: nothing to check against, nothing dropped
    assert es2[0]["printed_id"] == "17-2-1-4"


def test_carry_over_keeps_each_parcels_own_polygon_and_hand_named_parcels():
    stored = {"pages": [{"page_number": 7, "sheet": {"parcels": _entities()}}]}
    a, b = stored["pages"][0]["sheet"]["parcels"]
    a["confirmed_polygon"], b["confirmed_polygon"] = {"vertices": [[0, 0]], "id": "A"}, {"vertices": [[9, 9]], "id": "B"}
    manual = pr.new_entity(7, stored["pages"][0]["sheet"]["parcels"], label="Mystery", region_index=0, source="manual", manual=True)
    stored["pages"][0]["sheet"]["parcels"].append(manual)
    new = {"pages": [{"page_number": 7, "sheet": {"parcels": _entities()}}]}   # a fresh checkpoint of the pipeline
    pr.carry_over_user_data(stored, new)
    got = {e["id"]: e for e in new["pages"][0]["sheet"]["parcels"]}
    assert got["p7-1"]["confirmed_polygon"]["id"] == "A" and got["p7-2"]["confirmed_polygon"]["id"] == "B"
    assert "p7-m1" in got and got["p7-m1"]["manual"] is True


def test_one_printed_id_cannot_identify_two_parcels():
    es = _entities()
    es[1]["printed_id"] = "17-2-1-4"                      # a model copying the Remainder's id onto Parcel 1
    pr.verify_printed_ids(es, "REMAINDER PARCEL 17-2-1-4 PARCEL 1")
    assert es[0]["printed_id"] == "17-2-1-4" and es[1]["printed_id"] is None


def test_join_is_idempotent_and_never_steals_linked_evidence():
    sheet = {"regions": [0], "inset_regions": [], "parcels": _entities()}
    regions = [_region(("PARCEL 1", "40.00"), ("REMAINDER PARCEL 17-2-1-4", "120.4"))]
    pr.join_evidence(7, sheet, regions)
    before = [dict(e["evidence_ref"]) for e in sheet["parcels"]]
    pr.join_evidence(7, sheet, regions)                    # run again (the finalize step does)
    assert [e["evidence_ref"] for e in sheet["parcels"]] == before and len(sheet["parcels"]) == 2
