"""Competition identities and deterministic search over real provider clubs.

These eight competitions are listed in football-data.org's free coverage. Club
membership is intentionally learned from real standings, never historical replay
labels or a hard-coded season roster.
"""

import re
import unicodedata
from difflib import SequenceMatcher

_COMPETITIONS = (
    ("PL", "Premier League", "England", ("epl", "english premier league", "english top division", "prem")),
    ("PD", "La Liga", "Spain", ("laliga", "primera division", "spanish league")),
    ("BL1", "Bundesliga", "Germany", ("german league", "bundesliga 1")),
    ("SA", "Serie A", "Italy", ("seriea", "italian league")),
    ("FL1", "Ligue 1", "France", ("ligue1", "french league")),
    ("ELC", "Championship", "England", ("efl championship", "english championship")),
    ("DED", "Eredivisie", "Netherlands", ("dutch league", "erediv")),
    ("PPL", "Primeira Liga", "Portugal", ("liga portugal", "portuguese league", "primeira")),
)

DEFAULT_COMPETITION_CODES = tuple(row[0] for row in _COMPETITIONS)
COMPETITIONS = {code: {"code": code, "name": name, "country": country, "default": True,
                       "emblem": None, "aliases": [code.lower(), name.lower(), *aliases]}
                for code, name, country, aliases in _COMPETITIONS}
COMPETITIONS["CL"] = {"code": "CL", "name": "Champions League", "country": "Europe", "default": False,
                      "emblem": None, "aliases": ["cl", "ucl", "uefa champions league", "champions league"]}

_CLUB_ALIASES = {
    "manchester united": ("man united", "man utd", "mufc"),
    "manchester city": ("man city", "mcfc"),
    "tottenham hotspur": ("tottenham", "spurs"),
    "wolverhampton wanderers": ("wolves",),
    "west bromwich albion": ("west brom", "wba"),
    "west ham united": ("west ham",),
    "newcastle united": ("newcastle", "nufc"),
    "paris saint germain": ("psg", "paris sg"),
    "bayern munchen": ("bayern munich", "bayern"),
    "internazionale milano": ("inter", "inter milan"),
    "sporting clube de portugal": ("sporting", "sporting lisbon"),
    "borussia monchengladbach": ("gladbach",),
}


def normalize(value):
    value = str(value).replace("’", "'")
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().lower()
    value = re.sub(r"['’]s\b", "", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def search_phrase(value):
    value = normalize(value)
    value = re.sub(r"\b(where|is|are|the|show|me|for|please|table|tables|standings|position|of|league)\b", " ", value)
    return " ".join(value.split())


def team_aliases(team):
    names = [team.get("name", ""), team.get("short_name", ""), team.get("tla", "")]
    base = normalize(team.get("name", ""))
    base = re.sub(r"\b(fc|cf|afc|sc|ac|ssc|sv|vfl)\b", " ", base)
    base = " ".join(base.split())
    return sorted({name for name in [*names, base, *_CLUB_ALIASES.get(base, ())] if name})


def search_catalog(query, competitions, teams):
    phrase = search_phrase(query)
    if not phrase:
        return [], False
    results = []
    for item, kind in [*((item, "competition") for item in competitions),
                       *((item, "team") for item in teams)]:
        aliases = [operation(alias) for alias in [item["name"], *item.get("aliases", [])]
                   for operation in (search_phrase, normalize)]
        exact = phrase in aliases
        partial = any(phrase in alias.split() or (len(phrase) >= 3 and phrase in alias) for alias in aliases)
        confidence = 1.0 if exact else (0.9 if partial else max(
            (SequenceMatcher(None, phrase, alias).ratio() for alias in aliases), default=0))
        if confidence < 0.73:
            continue
        result = {"kind": kind, "competition_code": item.get("competition_code", item.get("code")),
                  "confidence": round(confidence, 3)}
        if kind == "team":
            result.update(team_id=item["id"], team_name=item["name"])
        else:
            result["name"] = item["name"]
        results.append(result)
    results.sort(key=lambda row: (-row["confidence"], row.get("team_name", row.get("name", "")),
                                  row["competition_code"]))
    if not results:
        return [], False
    best = results[0]["confidence"]
    candidates = [result for result in results if result["confidence"] >= best - 0.06][:12]
    return candidates, len(candidates) > 1
