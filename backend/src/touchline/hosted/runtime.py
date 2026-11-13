"""Create a fresh bounded replay without inheriting a shared demo clock."""

import math
from dataclasses import replace

from touchline.ml_inference.models.release import NEW_MODEL_VERSION
from touchline.ml_inference.streaming.contract import MATCHES_PER_GPU
from touchline.replay import ReplaySession, SessionConfig, SourceRegistry
from touchline.storage import S3Access


def create_source_access(env):
    """Construct one app-owned credential resource without contacting AWS."""
    configuration = SourceRegistry.from_env(env)
    if configuration.access != "presigned" or not any(source.key for source in configuration.sources):
        return None
    return S3Access(configuration._aws_env, region=configuration.region,
        token_provider=(lambda: env.get("MODAL_IDENTITY_TOKEN")) if env.get("MODAL_IDENTITY_TOKEN") else None)


async def create_session(env, session_id, activation_guard, *, source_access=None):
    source_env = {**env, "MATCHPULSE_OFFSETS_JSON": "{}"}
    registry = SourceRegistry.from_env(source_env, s3_access=source_access, owns_access=source_access is None)
    if env.get("MODAL_IDENTITY_TOKEN"):
        # Modal rotates its identity token in the live process environment.
        # Source configuration is copied, but credential refresh must not pin
        # that first token for a twenty-minute visitor or a warm container.
        registry._token_provider = lambda: env.get("MODAL_IDENTITY_TOKEN")
    # Custom explicit sources can carry offsets too. Hosted visitors always
    # begin at the start of each recording, independently of local demo flags.
    registry.sources = tuple(replace(source, offset_seconds=0) for source in registry.sources)
    prepared = SessionConfig(monitor=True, mode="real", model_version=NEW_MODEL_VERSION,
        checkpoint=NEW_MODEL_VERSION, max_duration_seconds=1200,
        max_estimated_cost_usd=2.60, max_restarts=0, director_mode="all_action",
        gpu_workers=math.ceil(len(registry.sources) / MATCHES_PER_GPU))
    session = ReplaySession(registry, config=SessionConfig(max_restarts=0, director_mode="all_action"),
        session_id=session_id, prepared_config=prepared, activation_guard=activation_guard,
        caption_max_audio_seconds=1200, caption_max_cost_usd=0.14)
    await session.initialize()
    session.director.set_auto(False)
    session._caption_user_enabled = False
    if env.get("MATCHPULSE_CAPTIONS_ENABLED", "true").lower() == "true" and env.get("ELEVENLABS_API_KEY"):
        try:
            from touchline.captions.live.adapter import RealtimeCaptionWorker
            session._hosted_caption_factory = lambda: RealtimeCaptionWorker(session.caption_event, env=env,
                max_audio_seconds=1200, max_cost_usd=0.14)
            worker = session._hosted_caption_factory()
            session.configure_captions(worker)
        except Exception:  # Provider/runtime details never enter a public response.
            session.configure_captions(None, configured=True, reason="runtime_unavailable")
    else:
        session.configure_captions(None, reason="missing_key")
    return session
