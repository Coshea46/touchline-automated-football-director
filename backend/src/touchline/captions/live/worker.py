"""Live caption worker, retaining the bounded focused-audio contract."""

import asyncio
import json
import os
import queue
import sys
from contextlib import suppress
from threading import Lock, Thread
from touchline.captions.limits import DEFAULT_AUDIO_SECONDS, DEFAULT_COST_USD
from touchline.captions.identity import canonical_json


from .engine import RealtimeEngine

def main():
    from touchline.config.local import load_local_environment

    load_local_environment()
    # SDK diagnostics must never corrupt IPC or expose credentials to browsers.
    output, sys.stdout = sys.stdout, sys.stderr
    state, lock, pending = {"control": None, "eof": False}, Lock(), queue.Queue(maxsize=1)

    def publish(value):
        with suppress(queue.Empty):
            if pending.full():
                pending.get_nowait()
        with suppress(queue.Full):
            pending.put_nowait(canonical_json(value))

    def write_events():
        while True:
            try:
                output.write(pending.get() + "\n")
                output.flush()
            except OSError:
                return

    def read_controls():
        try:
            while True:
                line = sys.stdin.readline(24577)
                if not line or len(line.encode()) > 24576:
                    break
                value = json.loads(line)
                if not isinstance(value, dict):
                    break
                with lock:
                    state["control"] = value
        except (OSError, ValueError):
            pass
        finally:
            with lock:
                state["eof"] = True
                if state["control"]:
                    state["control"]["active"] = False

    def current():
        with lock:
            return dict(state["control"]) if state["control"] else None

    async def run():
        engine = RealtimeEngine(current, publish,
                                max_audio_seconds=float(os.environ.get("MATCHPULSE_CAPTION_MAX_AUDIO_SECONDS", str(DEFAULT_AUDIO_SECONDS))),
                                max_cost_usd=float(os.environ.get("MATCHPULSE_CAPTION_BUDGET_USD", str(DEFAULT_COST_USD))),
                                api_key_present=bool(os.environ.get("ELEVENLABS_API_KEY")))
        task = asyncio.create_task(engine.run())
        while not state["eof"]:
            await asyncio.sleep(.03)
        engine.closed = True
        await task
        return 1 if engine.metrics["forced_provider_closes"] else 0

    Thread(target=write_events, daemon=True).start()
    Thread(target=read_controls, daemon=True).start()
    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())
