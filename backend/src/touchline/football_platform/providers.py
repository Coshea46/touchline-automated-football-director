"""Fixed-origin provider transport and strict public-response projections."""

import json
import math
from datetime import UTC, datetime
from urllib import error, parse, request

from touchline.football_platform.catalog import COMPETITIONS

FOOTBALL_ATTRIBUTION = {"text": "Football data provided by football-data.org",
                        "url": "https://www.football-data.org/"}
GUARDIAN_ATTRIBUTION = {
    "text": "Powered by The Guardian", "url": "https://www.theguardian.com/",
    "logo_url": "https://static.guim.co.uk/sys-images/Guardian/Pix/pictures/2010/03/01/poweredbyguardianBLACK.png",
    "terms_url": "https://www.theguardian.com/open-platform/terms-and-conditions",
}
NEWS_TOPICS = {
    "football": {},
    "premier-league": {"tag": "football/premierleague"},
    "champions-league": {"tag": "football/championsleague"},
    "european": {"tag": "football/europeanfootball"},
    "transfers": {"tag": "football/transfer-window"},
}


class ProviderError(Exception):
    """Contains only bounded application codes; never credentials or URLs."""

    def __init__(self, reason="unavailable", retry_after=30):
        self.reason = reason if reason in ("unavailable", "rate_limited", "access_denied") else "unavailable"
        self.retry_after = max(1, min(3600, retry_after))
        super().__init__(self.reason)


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class JSONTransport:
    MAX_BYTES = 1024 * 1024

    def get(self, provider, path, params, headers):
        bases = {"football-data.org": "https://api.football-data.org/v4/",
                 "guardian": "https://content.guardianapis.com/"}
        # Only callers in this module/service construct path; users never choose
        # a remote host. Redirects cannot forward the provider credentials.
        if provider not in bases or not path or path.startswith("/") or ".." in path:
            raise ProviderError()
        url = bases[provider] + path + ("?" + parse.urlencode(params) if params else "")
        req = request.Request(url, headers={"Accept": "application/json", "User-Agent": "Touchline/1.0", **headers})
        try:
            with request.build_opener(_NoRedirect()).open(req, timeout=6) as response:
                raw = response.read(self.MAX_BYTES + 1)
                if len(raw) > self.MAX_BYTES:
                    raise ProviderError()
                data = json.loads(raw)
                if not isinstance(data, dict):
                    raise ProviderError()
                return data
        except error.HTTPError as exc:
            retry = exc.headers.get("Retry-After", "30") if exc.headers else "30"
            retry = int(retry) if str(retry).isdigit() else 30
            if exc.code == 429:
                raise ProviderError("rate_limited", retry) from None
            if exc.code in (401, 403):
                raise ProviderError("access_denied", 300) from None
            raise ProviderError() from None
        except (OSError, ValueError, error.URLError):
            raise ProviderError() from None


def text(value, limit=240, *, optional=False):
    if value is None and optional:
        return None
    if not isinstance(value, str) or len(value) > limit or any(ord(char) < 32 for char in value):
        raise ProviderError()
    return value


def integer(value, *, optional=False, negative=False):
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or abs(value) > 100000 or (value < 0 and not negative):
        raise ProviderError()
    return value


def safe_url(value, *, hosts=None):
    if not isinstance(value, str) or len(value) > 2000:
        return None
    parts = parse.urlsplit(value)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        return None
    if hosts and parts.hostname not in hosts:
        return None
    return value


def standings_payload(data, code):
    competition, season, tables = data.get("competition"), data.get("season"), data.get("standings")
    if not isinstance(competition, dict) or competition.get("code") != code or not isinstance(season, dict):
        raise ProviderError()
    if not isinstance(tables, list) or len(tables) > 64:
        raise ProviderError()
    result = []
    for table in tables:
        if not isinstance(table, dict) or not isinstance(table.get("table"), list) or len(table["table"]) > 64:
            raise ProviderError()
        rows, seen = [], set()
        for row in table["table"]:
            if not isinstance(row, dict) or not isinstance(row.get("team"), dict):
                raise ProviderError()
            team = row["team"]
            team_id = integer(team.get("id"))
            if team_id in seen:
                raise ProviderError()
            seen.add(team_id)
            projected = {"position": integer(row.get("position"), optional=True),
                         "team": {"id": team_id, "name": text(team.get("name")),
                                  "short_name": text(team.get("shortName"), optional=True),
                                  "tla": text(team.get("tla"), 10, optional=True),
                                  "crest": safe_url(team.get("crest"), hosts={"crests.football-data.org"})},
                         "form": text(row.get("form"), 100, optional=True)}
            for output, original in (("played", "playedGames"), ("won", "won"), ("drawn", "draw"),
                                     ("lost", "lost"), ("goals_for", "goalsFor"),
                                     ("goals_against", "goalsAgainst"), ("goal_difference", "goalDifference"),
                                     ("points", "points")):
                projected[output] = integer(row.get(original), optional=True,
                                            negative=output in ("goal_difference", "points"))
            rows.append(projected)
        result.append({"type": text(table.get("type"), 40), "stage": text(table.get("stage"), 80, optional=True),
                       "group": text(table.get("group"), 100, optional=True), "rows": rows})
    return {"competition": {**COMPETITIONS[code], "name": text(competition.get("name")),
                             "type": text(competition.get("type"), 40, optional=True),
                             "emblem": safe_url(competition.get("emblem"), hosts={"crests.football-data.org"})},
            "season": {"id": integer(season.get("id"), optional=True),
                       "start_date": text(season.get("startDate"), 30, optional=True),
                       "end_date": text(season.get("endDate"), 30, optional=True),
                       "current_matchday": integer(season.get("currentMatchday"), optional=True)},
            "tables": result}


def news_payload(data):
    response = data.get("response")
    if not isinstance(response, dict) or response.get("status") != "ok":
        raise ProviderError()
    results = response.get("results")
    if not isinstance(results, list) or len(results) > 50:
        raise ProviderError()
    articles, seen = [], set()
    for item in results:
        if not isinstance(item, dict) or item.get("sectionId") != "football":
            continue
        url = safe_url(item.get("webUrl"), hosts={"www.theguardian.com", "theguardian.com"})
        identifier = text(item.get("id"), 500)
        if not url or identifier in seen or url in seen:
            continue
        published = text(item.get("webPublicationDate"), 40)
        try:
            stamp = datetime.fromisoformat(published)
            if stamp.tzinfo is None or not math.isfinite(stamp.timestamp()):
                raise ValueError
        except (ValueError, OverflowError):
            raise ProviderError() from None
        fields = item.get("fields", {})
        if not isinstance(fields, dict):
            raise ProviderError()
        articles.append({"id": identifier, "title": text(item.get("webTitle"), 1000), "url": url,
                         "published_at": stamp.astimezone(UTC).isoformat(),
                         "publisher": "The Guardian", "byline": text(fields.get("byline"), 1000, optional=True),
                         "copyright": text(fields.get("copyright"), 1000, optional=True),
                         "image_url": None, "excerpt": None})
        seen.update((identifier, url))
    articles.sort(key=lambda article: article["published_at"], reverse=True)
    return {"articles": articles[:12]}
