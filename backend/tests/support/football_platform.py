"""Reusable synthetic test doubles and builders; no provider calls."""


import asyncio
import copy
import json
from touchline.football_platform.catalog import COMPETITIONS


def standing(code="PL", *, names=("Arsenal FC", "Chelsea FC")):
    return {"competition": {"code": code, "name": COMPETITIONS[code]["name"], "type": "LEAGUE"},
            "season": {"id": 123, "startDate": "2026-08-01", "endDate": "2027-05-31", "currentMatchday": 12},
            "standings": [{"type": "TOTAL", "stage": "REGULAR_SEASON", "group": None,
                           "table": [{"position": index, "team": {"id": 50 + index, "name": name,
                                                                 "shortName": name.removesuffix(" FC"), "tla": None},
                                      "playedGames": 10, "won": 6, "draw": 2, "lost": 2,
                                      "goalsFor": 20, "goalsAgainst": 10, "goalDifference": 10,
                                      "points": 20, "form": None} for index, name in enumerate(names, 1)]}]}


def story(identifier="football/2026/nov/08/example", *, date="2026-11-08T12:00:00Z", title="Original headline"):
    return {"id": identifier, "sectionId": "football", "webTitle": title,
            "webUrl": "https://www.theguardian.com/" + identifier, "webPublicationDate": date,
            "fields": {"byline": "Original Byline", "copyright": "Guardian News & Media Limited",
                       "body": "Never expose article body", "thumbnail": "https://example.com/image.jpg",
                       "trailText": "Never expose an unlicensed excerpt"}}


def news(*items):
    return {"response": {"status": "ok", "results": list(items or (story(),))}}


class Clock:
    def __init__(self):
        self.now = 1700000000.0

    def __call__(self):
        return self.now


class Transport:
    def __init__(self):
        self.calls = []
        self.failure = None
        self.block = None

    def get(self, provider, path, params, headers):
        self.calls.append((provider, path, copy.deepcopy(params), copy.deepcopy(headers)))
        if self.block:
            assert self.block.wait(2)
        if self.failure:
            raise self.failure
        if provider == "guardian":
            return news()
        return standing(path.split("/")[1])


def run(coroutine):
    return asyncio.run(coroutine)


async def http_request(port, target, *, origin="http://localhost:3030", method="GET", headers=""):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    origin_line = f"Origin: {origin}\r\n" if origin is not None else ""
    writer.write(f"{method} {target} HTTP/1.1\r\nHost: 127.0.0.1\r\n{origin_line}{headers}\r\n".encode())
    await writer.drain()
    raw = await reader.read()
    writer.close()
    await writer.wait_closed()
    head, body = raw.split(b"\r\n\r\n", 1)
    return int(head.split(b" ")[1]), head.decode(), json.loads(body) if body else None
