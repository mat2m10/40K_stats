import asyncio
from pathlib import Path

import aiohttp
import pytest

import scrape

FIXTURE = Path(__file__).parent / "fixtures" / "datasheet_sample.html"


@pytest.fixture(scope="module")
def units():
    return scrape.parse_page(FIXTURE.read_text(encoding="utf-8"))


def test_finds_every_datasheet(units):
    assert [u["name"] for u in units] == ["Test Brood", "Lone Beast"]
    assert units[0]["id"] == "test_brood"


def test_stat_block(units):
    assert units[0]["stats"] == {"M": '8"', "T": "5", "SV": "4+", "W": "3"}


def test_to_snake_drops_ascii_and_curly_apostrophes():
    assert scrape.to_snake("T'au Empire") == "tau_empire"
    assert scrape.to_snake("Ghazghkull’s Waaagh") == "ghazghkulls_waaagh"
    assert scrape.to_snake("‘Ard Boyz") == "ard_boyz"


def test_weapon_profiles(units):
    weapons = units[0]["weapons"]
    assert len(weapons) == 2  # header rows and the colspan duplicates are skipped
    ranged, melee = weapons
    assert (ranged["range"], ranged["A"], ranged["BS_WS"], ranged["S"], ranged["AP"], ranged["D"]) == (
        '18"', "D6", "4+", "5", "-1", "2")
    assert (melee["name"], melee["range"], melee["D"]) == ("Sharp bits", "Melee", "1")


def test_weapon_keywords_split_from_name(units):
    ranged, melee = units[0]["weapons"]
    assert ranged["name"] == "Goo spitter"
    assert ranged["abilities"] == "assault, ignores cover"
    assert melee["abilities"] == ""


def test_abilities_use_cyrillic_class(units):
    # Only found if the "dsRightСol" (Cyrillic С) lookup still matches.
    names = [a["name"] for a in units[0]["abilities"]]
    assert "Slimy Hide" in names


def test_abilities_sections_and_splitting(units):
    abilities = [(a["name"], a["type"]) for a in units[0]["abilities"]]
    assert abilities == [
        ("Deep Strike", "core"),               # "CORE: ..." -> one entry per rule
        ("Slimy Hide", "datasheet"),
        ("Spore Burst", "datasheet"),          # two <b>Name:</b> in one div -> two entries
        ("Regrowth", "datasheet"),
        ("Damaged: 1-3 Wounds Remaining", "damaged"),
    ]  # UNIT COMPOSITION "<b>...:</b>" lines are not abilities
    by_name = {a["name"]: a["description"] for a in units[0]["abilities"]}
    assert by_name["Slimy Hide"] == "Each time an attack targets this unit, subtract 1 from the Wound roll."
    assert by_name["Spore Burst"] == "Once per battle, this unit can burst."
    assert by_name["Regrowth"] == "At the end of your turn, heal 1 wound."


def test_keywords(units):
    # Multi-word keywords stay whole; faction keywords are kept separately.
    assert units[0]["keywords"] == ["INFANTRY", "GREAT DEVOURER", "TEST BROOD"]
    assert units[0]["faction_keywords"] == ["TYRANIDS"]


def test_minimal_datasheet(units):
    lone = units[1]
    assert lone["stats"] == {} and lone["weapons"] == [] and lone["abilities"] == []
    assert lone["keywords"] == [] and lone["faction_keywords"] == []


# ── fetch retry behaviour ─────────────────────────────────────────────────────

class FakeResponse:
    def __init__(self, status, text="", headers=None):
        self.status, self._text, self.headers = status, text, headers or {}

    async def text(self):
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    """Plays back a script of responses (or exceptions) for session.get()."""

    def __init__(self, script):
        self.script, self.calls = list(script), 0

    def get(self, url, **kwargs):
        self.calls += 1
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def run_fetch(script, retries=4):
    session, sleeps = FakeSession(script), []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    result = asyncio.run(scrape.fetch(session, "tyranids", "Tyranids", retries=retries, sleep=fake_sleep))
    return result, session.calls, sleeps


def test_retries_on_429_and_5xx_then_succeeds():
    (slug, _, status, html), calls, sleeps = run_fetch([
        FakeResponse(429, headers={"Retry-After": "7"}),
        FakeResponse(503),
        FakeResponse(200, "<html>ok</html>"),
    ])
    assert (slug, status, html, calls) == ("tyranids", 200, "<html>ok</html>", 3)
    assert sleeps[0] == 7.0           # Retry-After honoured
    assert 4.0 <= sleeps[1] < 5.0     # backoff 2 * 2**1 + jitter


def test_retries_on_network_error():
    (_, _, status, _), calls, _ = run_fetch([aiohttp.ClientConnectionError("boom"), FakeResponse(200, "x")])
    assert (status, calls) == (200, 2)


def test_does_not_retry_404():
    (_, _, status, html), calls, sleeps = run_fetch([FakeResponse(404)])
    assert (status, html, calls, sleeps) == (404, "", 1, [])


def test_gives_up_after_retries():
    (_, _, status, html), calls, sleeps = run_fetch([FakeResponse(502)] * 3, retries=3)
    assert (status, html, calls, len(sleeps)) == (502, "", 3, 2)


def test_retry_after_is_capped():
    assert scrape._retry_delay(1, 2.0, "3600") == scrape.MAX_RETRY_AFTER
    assert scrape._retry_delay(1, 2.0, "7") == 7.0
