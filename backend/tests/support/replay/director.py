"""Reusable synthetic test doubles and builders; no provider calls."""


from touchline.replay import Director, ReplaySession
from tests.support.replay.session import Clock, FakeBridge, configuration, playback, registry, scores


def stable(*, ids=("cam-1", "cam-2"), **kwargs):
    return Director(ids, session_id="session", model_version="mock-v1", mode="features_mock", **kwargs)


def evaluate(director, end, now, values=None, *, sequence=None, commit=False):
    snapshot = scores(values, end=end, now=now, sequence=sequence or int((end - 3) / 2), epoch=director.clock_epoch)
    assert director.ingest(snapshot, now=now, playback_position=end)
    return director.decide(now=now, playback_position=end, commit=commit)


async def session_with_proposal():
    clock, bridge, sent = Clock(), FakeBridge(), []
    session = await ReplaySession(registry(), config=configuration(director_mode="balanced"), bridge=bridge,
                                   publish=sent.append, now=clock, monotonic=clock, session_id="session").initialize()
    session.connect("owner")
    session.receive("owner", playback(session, position=5))
    session.accept_snapshot(scores())
    clock.value += 2
    session.receive("owner", playback(session, position=7))
    assert session.accept_snapshot(scores(end=7, sequence=2, now=clock.value))
    return session, clock, bridge, sent[-1]
