"""Application backend: source registration, replay clock, director and WebSocket."""

import asyncio
import logging
import os

from touchline.captions import CaptionService
from touchline.captions.limits import DEFAULT_AUDIO_SECONDS, DEFAULT_COST_USD
from touchline.api.http import HTTPServer
from touchline.config.local import load_local_environment
from touchline.match_metadata import METADATA_PATH
from touchline.replay import ReplaySession, SessionConfig, SourceRegistry
from touchline.api.websocket import WebSocketManager


class AppController:
    def __init__(self, *, registry=None, config=None, bridge=None, host=None, port=None,
                 api_enabled=None, api_host=None, api_port=None, caption_worker=None,
                 prepared_config=None, bridge_factory=None, preparation_error=None):
        load_local_environment()
        captions_enabled = os.environ.get("MATCHPULSE_CAPTIONS_ENABLED", "true").lower()
        if captions_enabled not in ("true", "false"):
            raise ValueError("MATCHPULSE_CAPTIONS_ENABLED must be true or false")
        self._captions_enabled = captions_enabled == "true"
        self._caption_configured = bool(os.environ.get("ELEVENLABS_API_KEY"))
        self._caption_max_seconds = float(os.environ.get("MATCHPULSE_CAPTION_MAX_AUDIO_SECONDS", str(DEFAULT_AUDIO_SECONDS)))
        self._caption_budget_usd = float(os.environ.get("MATCHPULSE_CAPTION_BUDGET_USD", str(DEFAULT_COST_USD)))
        self.session = ReplaySession(registry or SourceRegistry.from_env(),
                                     config=config or SessionConfig.from_env(), bridge=bridge,
                                     caption_worker=caption_worker,
                                     prepared_config=prepared_config, bridge_factory=bridge_factory,
                                     preparation_error=preparation_error,
                                     caption_max_audio_seconds=self._caption_max_seconds,
                                     caption_max_cost_usd=self._caption_budget_usd)
        if caption_worker is None:
            self.session.configure_captions(None, configured=self._caption_configured,
                reason="feature_disabled" if not self._captions_enabled else "missing_key")
        self.ws_manager = WebSocketManager(self.session,
                                           host=host or os.environ.get("MATCHPULSE_HOST", "0.0.0.0"),
                                           port=int(os.environ.get("MATCHPULSE_PORT", "8000")) if port is None else port)
        enabled = os.environ.get("MATCHPULSE_API_ENABLED", "true").lower()
        if enabled not in ("true", "false"):
            raise ValueError("MATCHPULSE_API_ENABLED must be true or false")
        self.api_enabled = enabled == "true" if api_enabled is None else api_enabled
        if type(self.api_enabled) is not bool:
            raise TypeError("api_enabled must be boolean")
        self.api_host = api_host or os.environ.get("MATCHPULSE_API_HOST") or self.ws_manager.host
        self.api_port = (0 if port == 0 else int(os.environ.get("MATCHPULSE_API_PORT", "8031"))) if api_port is None else api_port
        self.api_manager = None
        self.caption_service = None

    def _captions(self, query):
        if set(query) - {"match_id", "source_key", "source_identity"}:
            raise ValueError("invalid_caption_query")
        return self.caption_service.get(query.get("match_id", ""), query.get("source_key"),
                                        query.get("source_identity"))

    async def start(self):
        await self.session.initialize()
        if (self._captions_enabled and self._caption_configured
                and self.session.caption_worker is None):
            try:
                from touchline.captions.live.adapter import RealtimeCaptionWorker
                worker = RealtimeCaptionWorker(self.session.caption_event,
                    max_audio_seconds=self._caption_max_seconds, max_cost_usd=self._caption_budget_usd)
                self.session.configure_captions(worker)
            except Exception:  # noqa: BLE001 - provider/runtime errors must not disclose credentials
                self.session.configure_captions(None, configured=True, reason="runtime_unavailable")
        self.session.start()
        try:
            if self.api_enabled:
                # Only confirmed demo bucket/object bindings may use the demo's
                # source-bound transcripts. Custom cam-1 recordings stay isolated.
                sources = tuple(source for source in self.session.sources if source.metadata)
                self.caption_service = CaptionService.from_metadata(METADATA_PATH, sources=sources)
                self.api_manager = HTTPServer.from_env(extra_routes={"/api/captions": self._captions})
                self.api_manager.host, self.api_manager.port = self.api_host, self.api_port
                await self.api_manager.start()
            await self.ws_manager.serve()
        finally:
            try:
                await self.session.close()
            finally:
                if self.api_manager is not None:
                    await self.api_manager.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    # SDK debug logs can contain signed URLs. Application errors report classes,
    # never media payloads or source credentials.
    for name in ("boto3", "botocore", "urllib3", "websockets"):
        logging.getLogger(name).setLevel(logging.WARNING)
    try:
        asyncio.run(AppController().start())
    except KeyboardInterrupt:
        pass
