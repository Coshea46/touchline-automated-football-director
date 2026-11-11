"""Offline tests for unit football_platform test_catalog."""


from touchline.football_platform.catalog import COMPETITIONS, DEFAULT_COMPETITION_CODES, search_catalog


def test_eight_defaults_and_natural_fuzzy_competition_search():
    assert list(DEFAULT_COMPETITION_CODES) == ["PL", "PD", "BL1", "SA", "FL1", "ELC", "DED", "PPL"]
    for query in ("EPL", "Where is the Premier League table?", "premier leage", "English top division"):
        result, ambiguous = search_catalog(query, list(COMPETITIONS.values()), [])
        assert result[0]["competition_code"] == "PL" and not ambiguous
