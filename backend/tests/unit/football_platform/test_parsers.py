"""Offline tests for unit football_platform test_parsers."""


import copy
import json
import pytest
from touchline.football_platform.providers import ProviderError, news_payload, standings_payload
from tests.support.football_platform import standing, story, news


def test_standings_preserve_group_format_null_unknown_stats_and_deductions():
    payload = standing()
    first = payload["standings"][0]["table"][0]
    first.update(playedGames=None, points=-2, goalDifference=-10)
    payload["standings"][0].update(stage="GROUP_STAGE", group="GROUP_A")
    second = copy.deepcopy(payload["standings"][0])
    second["group"] = "GROUP_B"
    payload["standings"].append(second)
    result = standings_payload(payload, "PL")
    assert [table["group"] for table in result["tables"]] == ["GROUP_A", "GROUP_B"]
    row = result["tables"][0]["rows"][0]
    assert row["played"] is None and row["form"] is None
    assert row["points"] == -2 and row["goal_difference"] == -10
    assert result["season"]["start_date"] == "2026-08-01"


@pytest.mark.parametrize("mutation", [
    lambda data: data["competition"].update(code="PD"),
    lambda data: data["standings"][0]["table"][0].update(points=True),
    lambda data: data["standings"][0]["table"][0].update(points=float("nan")),
    lambda data: data["standings"][0]["table"].append(copy.deepcopy(data["standings"][0]["table"][0])),
])
def test_malformed_provider_standings_rejected(mutation):
    payload = standing()
    mutation(payload)
    with pytest.raises(ProviderError):
        standings_payload(payload, "PL")


def test_news_metadata_original_attribution_sort_dedupe_and_safe_publisher_links():
    older = story(date="2026-11-07T11:00:00Z")
    newer = story("football/2026/nov/08/new", title="Unchanged full headline")
    unsafe = story("football/unsafe")
    unsafe["webUrl"] = "https://attacker.example/other"
    unrelated = story("world/unrelated")
    unrelated["sectionId"] = "world"
    result = news_payload(news(older, newer, older, unsafe, unrelated))
    assert [item["title"] for item in result["articles"]] == ["Unchanged full headline", "Original headline"]
    article = result["articles"][0]
    assert article["byline"] == "Original Byline" and article["copyright"] == "Guardian News & Media Limited"
    assert article["image_url"] is article["excerpt"] is None
    assert "Never expose" not in json.dumps(result)


@pytest.mark.parametrize("date", ["yesterday", "2026-11-08T12:00:00", "", "9999999999999"])
def test_news_requires_real_timezone_aware_publication_timestamp(date):
    with pytest.raises(ProviderError):
        news_payload(news(story(date=date)))
