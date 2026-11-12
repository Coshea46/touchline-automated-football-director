"""Hosted initialization is free of inference and preserves rotating identity."""

import asyncio

from touchline.hosted.runtime import create_session


def test_custom_source_offsets_and_inherited_monitor_flags_cannot_shift_a_fresh_visitor_clock():
    async def scenario():
        env = {"S3_BUCKET_NAME": "match-switcher-poc-videos", "MATCHPULSE_SOURCE_ACCESS": "public",
            "MATCHPULSE_SOURCES_JSON": '[{"match_id":"cam-1","label":"Synthetic","key":"match-1.mp4","offset_seconds":120}]',
            "MATCHPULSE_OFFSETS_JSON": '{"cam-1":999}', "MATCHPULSE_MONITOR": "true",
            "MATCHPULSE_CAPTIONS_ENABLED": "false", "MODAL_IDENTITY_TOKEN": "synthetic-first"}
        async def guard():
            return "ai_required"
        session = await create_session(env, "synthetic-visitor", guard)
        try:
            assert session.position == 0 and session.sources[0].offset_seconds == 0
            assert not session.director.auto_enabled and not session.config.monitor
            assert session.bridge is None and not session._caption_enabled
            assert session._prepared_config.max_duration_seconds == 120
            assert session._prepared_config.max_estimated_cost_usd == .70
            assert session._prepared_config.max_restarts == 0
            assert session.registry._token_provider() == "synthetic-first"
            env["MODAL_IDENTITY_TOKEN"] = "synthetic-rotated"
            assert session.registry._token_provider() == "synthetic-rotated"
        finally:
            await session.close()
    asyncio.run(scenario())


def test_default_hosted_app_keeps_modal_identity_rotation_live_for_existing_visitors(monkeypatch):
    from touchline.hosted.app import create_app
    from tests.support.hosted import Admission, Connection, Routes
    monkeypatch.setenv("MATCHPULSE_ALLOWED_ORIGINS", "https://portfolio.invalid")
    monkeypatch.setenv("MATCHPULSE_MATCH_COUNT", "1")
    monkeypatch.setenv("MATCHPULSE_CAPTIONS_ENABLED", "false")
    monkeypatch.setenv("MODAL_IDENTITY_TOKEN", "synthetic-first")
    async def scenario():
        app = create_app(admission=Admission(), routes=Routes())
        try:
            visitor, _ = await app.sessions.attach({"type": "hello"}, "peer", Connection())
            assert visitor.session.registry._token_provider() == "synthetic-first"
            monkeypatch.setenv("MODAL_IDENTITY_TOKEN", "synthetic-rotated")
            assert visitor.session.registry._token_provider() == "synthetic-rotated"
        finally:
            await app.close()
    asyncio.run(scenario())
