"""A bare-street anchor must agree with the document's own state (real case: Washoe County, NV -> Toronto)."""
import asyncio

from app.schemas.location import Location
from app.services import geocoding

TORONTO = Location(name="Ironwood Road, Toronto", latitude=43.7466, longitude=-79.5508, region="Ontario", country="Canada", source="nominatim")
RENO = Location(name="Reno", latitude=39.53, longitude=-119.81, region="Nevada", country="United States", source="nominatim")
NV_STREET = Location(name="Ironwood Road, Washoe County", latitude=39.75, longitude=-119.95, region="Nevada", country="United States", source="nominatim")
CANDIDATES = ["0 Ironwood Road", "Reno, NV 89512-2845", "RENO,NV 89509"]


def _run(table, candidates=CANDIDATES, monkeypatch=None):
    calls = []

    async def fake(query, limit=5):
        calls.append(query)
        return table.get(query, [])

    async def no_sleep(_):
        return None

    monkeypatch.setattr(geocoding, "geocode_place", fake)
    monkeypatch.setattr(geocoding.asyncio, "sleep", no_sleep)
    return asyncio.run(geocoding.geocode_anchor(candidates)), calls


def test_bare_street_in_wrong_country_is_requalified_with_the_documents_state(monkeypatch):
    (results, query), calls = _run({
        "0 Ironwood Road": [TORONTO],
        "Reno, NV 89512-2845": [RENO],
        "0 Ironwood Road, Nevada": [NV_STREET, TORONTO],
    }, monkeypatch=monkeypatch)
    assert query == "0 Ironwood Road, Nevada" and results == [NV_STREET]  # never Toronto
    assert calls == ["0 Ironwood Road", "Reno, NV 89512-2845", "0 Ironwood Road, Nevada"]


def test_falls_back_to_the_state_qualified_candidate_rather_than_trust_the_wrong_country(monkeypatch):
    (results, query), _ = _run({"0 Ironwood Road": [TORONTO], "Reno, NV 89512-2845": [RENO]}, monkeypatch=monkeypatch)
    assert query == "Reno, NV 89512-2845" and results == [RENO]


def test_agreeing_results_are_left_alone(monkeypatch):
    (results, query), calls = _run({"0 Ironwood Road": [NV_STREET], "Reno, NV 89512-2845": [RENO]}, monkeypatch=monkeypatch)
    assert query == "0 Ironwood Road" and results == [NV_STREET] and len(calls) == 2


def test_no_state_bearing_candidate_changes_nothing(monkeypatch):
    (results, query), calls = _run({"0 Ironwood Road": [TORONTO]}, candidates=["0 Ironwood Road"], monkeypatch=monkeypatch)
    assert query == "0 Ironwood Road" and results == [TORONTO] and calls == ["0 Ironwood Road"]


def test_state_qualified_first_candidate_is_not_second_guessed(monkeypatch):
    (results, query), calls = _run({"Reno, NV 89512-2845": [RENO]}, candidates=["Reno, NV 89512-2845", "0 Ironwood Road"], monkeypatch=monkeypatch)
    assert query == "Reno, NV 89512-2845" and calls == ["Reno, NV 89512-2845"]


# ---- Imperial County plat: "EI Centro, CA" geocoded to Venezuela; the parcel's own street address was never used

VENEZUELA = Location(name="El Centro, Venezuela", latitude=10.47, longitude=-68.01, region="Carabobo", country="Venezuela", source="nominatim")
EL_CENTRO_CA = Location(name="El Centro", latitude=32.792, longitude=-115.563, region="California", country="United States", source="nominatim")
CROSS_RD = Location(name="2201 Cross Road", latitude=32.811, longitude=-115.5529, region="California", country="United States", source="nominatim")


def test_a_result_that_contradicts_the_states_the_query_names_is_rejected(monkeypatch):
    (results, query), calls = _run(
        {"El Centro, CA, 92243": [VENEZUELA], "El Centro, CA 92243": [EL_CENTRO_CA]},
        candidates=["El Centro, CA, 92243", "El Centro, CA 92243"], monkeypatch=monkeypatch,
    )
    assert results == [EL_CENTRO_CA] and query == "El Centro, CA 92243"   # never South America
    assert geocoding._consistent_with_query(VENEZUELA, "El Centro, CA, 92243") is False
    assert geocoding._consistent_with_query(EL_CENTRO_CA, "El Centro, CA, 92243") is True
    assert geocoding._consistent_with_query(VENEZUELA, "Payette 83661") is True   # no state stated: not judged


def test_ocr_ei_is_read_as_el_street_addresses_lead_and_letterhead_and_foreign_mailing_lines_do_not():
    from app.services import georeference as geo

    text = "\n".join([
        "Project Report", "EI Centro, CA, 92243", "801 MAIN STREET, EL CENTRO, CA, 92243 (442) 265-1736",
        "2201 Cross Road EI Centro, CA 92243", "2205 Cross Road EI Centro, CA 92243",
        "17533 East Starflower Court Queen Creek, AZ 85142",
    ])
    out = geo.find_anchor_candidates([{"regions": [{"ocr_text": text, "class": "Text"}]}], limit=6)
    assert out[0] == "2201 Cross Road El Centro, CA 92243"          # street-level, "EI" repaired
    assert not any("801 MAIN" in c for c in out)                      # phone number: an agency's office
    assert out.index("2205 Cross Road El Centro, CA 92243") < out.index("17533 East Starflower Court Queen Creek, AZ 85142")
    assert out.index("2205 Cross Road El Centro, CA 92243") < out.index("El Centro, CA, 92243")


def test_county_state_candidate_for_deed_without_street_address():
    from app.services import georeference as g

    pages = [{"regions": [{"class": "text", "ocr_text":
        "Section 21, Township 13 South, Range 12 East, Gila and Salt River Meridian, "
        "Pima County, Arizona. Records of Pima County, Arizona."}]}]
    assert g._county_state_candidate(pages) == "Pima County, Arizona"
    assert g._county_state_candidate([{"regions": [{"ocr_text": "no place named here"}]}]) is None


def test_surveyed_coordinates_prefer_the_pair_nearest_the_site_not_the_first():
    """A plat lists a far-away control monument first, then the monuments at the parcel."""
    from app.services import georeference as g

    text = (
        'MONUMENT "N74SM01028" N:14872076.61 E:2257793.63 (GROUND COORDINATE) '
        'N:14834576.92 E:2289281.05 (GROUND COORDINATE) '
        "NEVADA STATE PLANE COORDINATE SYSTEM OF 1983, WEST ZONE, DISTANCES SHOWN ARE GROUND "
        "DISTANCES USING A PROJECT COMBINED GRID TO GROUND SCALE FACTOR OF 1.000197939."
    )
    pages = [{"regions": [{"ocr_text": text}]}]
    lat, lon = g.find_surveyed_coordinates(pages, "Nevada", 39.4357, -119.7724)
    assert abs(lat - 39.4362) < 0.002 and abs(lon + 119.7724) < 0.002  # the parcel-side pair, not 39.538


def test_ground_factor_wording_with_ocr_zero_in_to():
    from app.services.georeference import ground_to_grid_multiplier

    text = "WASHOE COUNTY CONTROL POINT GROUND COORDINATES ... A COMBINED GRID T0 GROUND FACT0R OF 1.000197939 WAS USED."
    assert abs(ground_to_grid_multiplier(text) - 1 / 1.000197939) < 1e-12
