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
