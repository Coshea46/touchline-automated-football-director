"""Opt-in paid probe: real S3/Modal/backend/WS, with a paced headless viewer.

This checks the transport and director, not HTML video playback or predictive
quality. features_mock still runs real extraction but uses labelled mock scores.
No cloud calls are made without BOTH --allow-cloud and MATCHPULSE_MONITOR=true.
"""

import argparse
import asyncio
import json
import math
import os
import statistics
import time
from collections import Counter, deque
from itertools import pairwise

import websockets

from touchline.replay import ReplaySession, SessionConfig, SourceRegistry
from touchline.replay.diagnostics import worker_diagnostics
from touchline.api.websocket import WebSocketManager


DRAIN_SECONDS = 3.25


def delivery_summary(recommendations, replay_seconds, observed_replay_elapsed, *, drain_seconds=DRAIN_SECONDS):
    """Separate lost windows from windows whose delivery deadline wasn't observed."""
    count = max(0, math.floor((replay_seconds - 5 + 1e-8) / 2) + 1)
    expected = [5 + 2 * index for index in range(count)]
    received = [record["observation_end"] for record in recommendations]
    counts = Counter(received)
    missing = [end for end in expected if end not in counts]
    censored = [end for end in missing if observed_replay_elapsed + 1e-8 < end + drain_seconds]
    cadence = [current - previous for previous, current in pairwise(received)]
    ordered = all(current > previous for previous, current in pairwise(received))
    return {"expected_endpoints": expected, "received_endpoints": received,
            "missing_endpoints": missing, "censored_endpoints": censored,
            "missing_after_drain": [end for end in missing if end not in censored],
            "undelivered_past_3s": [end for end in missing if observed_replay_elapsed >= end + 3],
            "unexpected_endpoints": sorted(set(received) - set(expected)),
            "duplicate_endpoints": sorted(end for end, total in counts.items() if total > 1),
            "cadence_seconds": cadence, "cadence_order_valid": ordered,
            "complete": received == expected, "drain_seconds": drain_seconds,
            "drain_complete": observed_replay_elapsed >= replay_seconds + drain_seconds,
            "observed_replay_elapsed_seconds": observed_replay_elapsed}


def reported_backlog(health, playback_position):
    """Lag from the latest published worker watermark, including publish/poll delay."""
    expected_samples = math.floor(max(0, playback_position) * 15 + 0.5 + 1e-8)
    streams = {}
    for match_id, value in health.items():
        if not isinstance(value, dict):
            continue
        processed, queued = value.get("next_sample_index"), value.get("queue_samples")
        if type(processed) is int and processed >= 0:
            streams[match_id] = {"backlog_samples": max(0, expected_samples - processed),
                                 "backlog_seconds": max(0, expected_samples - processed) / 15,
                                 "queue_samples": queued if type(queued) is int and queued >= 0 else None}
    return {"streams": streams,
            "maximum_backlog_seconds": max((value["backlog_seconds"] for value in streams.values()), default=0),
            "maximum_queue_samples": max((value["queue_samples"] or 0 for value in streams.values()), default=0)}


def capacity_result(recommendations, registered_ids, *, cadence_complete, errors):
    """Degraded recommendations remain valid, but cannot establish full capacity."""
    registered_ids = set(registered_ids)
    complete = [isinstance(record.get("scores"), dict) and set(record["scores"]) == registered_ids
                for record in recommendations]
    full_count = sum(complete)
    full_cohort = bool(complete) and all(complete)
    return {"registered_source_count": len(registered_ids), "recommendation_count": len(complete),
            "full_source_cohort_count": full_count, "full_source_cohort_complete": full_cohort,
            "degraded_recommendation_count": len(complete) - full_count,
            "passed": bool(complete) and cadence_complete and full_cohort and not errors}


async def probe(args):
    env = {**os.environ, "MATCHPULSE_MATCH_COUNT": str(args.matches),
           "MATCHPULSE_SESSION_SECONDS": str(args.seconds + args.startup_timeout + DRAIN_SECONDS + 10),
           "MATCHPULSE_SESSION_BUDGET_USD": str(args.budget)}
    config = SessionConfig.from_env(env)
    if not args.allow_cloud or not config.monitor:
        raise ValueError("Paid probe requires --allow-cloud and MATCHPULSE_MONITOR=true")
    registry = SourceRegistry.from_env(env)
    if len(registry.sources) != args.matches:
        raise ValueError("Configured sources must match --matches exactly")
    session = await ReplaySession(registry, config=config).initialize()
    registered_ids = {source.match_id for source in session.sources}
    manager = WebSocketManager(session, host="127.0.0.1", port=0)
    report = {"probe": "real-modal-backend-websocket-paced-headless-viewer",
              "mode": config.mode, "model_version": config.model_version,
              "score_kind": "model" if config.mode == "real" else "mock",
              "replay_transport": config.replay_transport,
              "html_video_playback_tested": False, "stream_count": args.matches,
              "replay_seconds": args.seconds, "recommendations": [], "errors": []}
    started = time.monotonic()
    ready_at = None
    current_match = None
    last_sequence = -1
    clock_epoch = 0
    heartbeat_task = None
    backlog_samples = deque(maxlen=256)
    last_backlog_sample = 0.0
    observed_replay_elapsed = 0.0
    try:
        await manager.start()
        session.start()
        port = manager.server.sockets[0].getsockname()[1]
        async with websockets.connect(f"ws://127.0.0.1:{port}", max_size=65536) as socket:
            initial = json.loads(await asyncio.wait_for(socket.recv(), 5))
            if initial.get("type") != "session" or not initial.get("controls_enabled"):
                raise RuntimeError("Missing controlling-viewer session")
            current_match = initial["current_match_id"]
            match_ids = {source["match_id"] for source in initial["matches"]}
            clock_epoch = initial["playback"]["clock_epoch"]

            def position():
                return 0.0 if ready_at is None else min(args.seconds, max(0, time.monotonic() - ready_at))

            async def heartbeat():
                while True:
                    replay_time = position()
                    await socket.send(json.dumps({"type": "playback", "session_id": session.session_id,
                        "match_id": current_match, "position": replay_time,
                        "playing": ready_at is not None and replay_time < args.seconds,
                        "rate": 1, "stalled": False, "available": True, "ended": False}))
                    await asyncio.sleep(0.2)

            heartbeat_task = asyncio.create_task(heartbeat())
            while True:
                now = time.monotonic()
                if ready_at is not None:
                    observed_replay_elapsed = now - ready_at
                    if now - last_backlog_sample >= 0.5:
                        backlog_samples.append({"replay_elapsed_seconds": observed_replay_elapsed,
                            "playback_position": position(), **reported_backlog(session.worker_health, position())})
                        last_backlog_sample = now
                if ready_at is None and time.monotonic() - started > args.startup_timeout:
                    report["errors"].append("startup_timeout")
                    break
                if ready_at is not None and observed_replay_elapsed >= args.seconds + DRAIN_SECONDS:
                    break
                try:
                    message = json.loads(await asyncio.wait_for(socket.recv(), 0.5))
                except TimeoutError:
                    continue
                received_wall, received_mono = time.time(), time.monotonic()
                if ready_at is not None:
                    observed_replay_elapsed = received_mono - ready_at
                if message.get("type") == "session":
                    clock_epoch = message["playback"]["clock_epoch"]
                    if message["state"] in ("running", "degraded") and ready_at is None:
                        ready_at = received_mono
                        report["startup_seconds"] = ready_at - started
                    elif message["state"] in ("error", "stopped", "ended"):
                        report["errors"].append(message.get("error_code") or message["state"])
                        break
                elif message.get("type") == "error":
                    report["errors"].append(message.get("code", "protocol_error"))
                elif message.get("type") == "recommendation":
                    # Mirror the identity/freshness gate without pretending this
                    # headless clock proves browser decoding or visual playback.
                    valid = (ready_at is not None and message.get("session_id") == session.session_id
                        and message.get("model_version") == config.model_version
                        and message.get("score_kind") == report["score_kind"]
                        and message.get("clock_epoch") == clock_epoch
                        and isinstance(message.get("sequence"), int) and message["sequence"] > last_sequence
                        and message.get("selected_match_id") in match_ids
                        and message.get("valid_until", 0) >= received_wall
                        and message.get("observation_end", math.inf) <= position() + 0.05)
                    if not valid:
                        report["errors"].append("rejected_recommendation")
                        continue
                    last_sequence = message["sequence"]
                    end = message["observation_end"]
                    # The strict paced viewer clock knows when its endpoint
                    # became available, including control/polling/WS transport.
                    latency = received_mono - (ready_at + end)
                    switched = current_match != message["selected_match_id"]
                    current_match = message["selected_match_id"]
                    report["recommendations"].append({"sequence": last_sequence,
                        "observation_end": end, "selected_match_id": current_match,
                        "switched": switched, "score_kind": message["score_kind"],
                        "source_cohort_complete": isinstance(message.get("scores"), dict)
                            and set(message["scores"]) == match_ids,
                        "observed_source_count": len(message["scores"]) if isinstance(message.get("scores"), dict) else 0,
                        "scores": message["scores"], "end_to_end_seconds": latency,
                        "received_replay_elapsed_seconds": received_mono - ready_at,
                        "availability_to_recommendation_seconds": message["latency_seconds"],
                        "worker_completion_to_backend_seconds": message.get("worker_completion_to_backend_seconds")})
                    await socket.send(json.dumps({"type": "ack", "session_id": session.session_id,
                        "sequence": last_sequence, "clock_epoch": clock_epoch,
                        "match_id": current_match, "received_at": received_wall}))
            await asyncio.sleep(0.05)  # Let the final ACK reach the real handler.
            try:
                latest = await asyncio.to_thread(session.bridge.latest, session.session_id)
                if isinstance(latest, dict):
                    report.update(worker_diagnostics(latest))
                    report["worker_metrics"] = latest.get("metrics", {})
                    report["worker_health"] = {key: {field: value.get(field) for field in
                        ("status", "next_sample_index", "queue_samples")} if isinstance(value, dict) else value
                        for key, value in latest.get("health", {}).items()}
                    report["final_reported_backlog"] = reported_backlog(report["worker_health"], position())
            except Exception as exc:  # noqa: BLE001 - sanitize SDK URL/credential errors
                report["errors"].append("final_metrics_" + type(exc).__name__)
    except Exception as exc:  # noqa: BLE001
        report["errors"].append(type(exc).__name__)
    finally:
        if heartbeat_task is not None:
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)
        await manager.close()
        await session.close()
        report["backend_metrics"] = session.metrics
        for field in ("state", "reason", "error_type", "error_phase", "error_code", "shard_errors"):
            key = "worker_" + field
            if report.get(key) is None:
                report[key] = session.metrics.get("worker_last_state" if field == "state" else key)
        report["websocket_metrics"] = manager.metrics
        report["wall_seconds"] = time.monotonic() - started
        report["owned_worker_handle_cleared"] = session._handle is None
        report["owned_worker_cancelled"] = session.metrics.get("worker_stop_succeeded", False)
        report["owned_worker_stop_confirmed"] = session.metrics.get("worker_stop_succeeded", False)
        report["replay_transport"] = config.replay_transport
        report["owned_stop_method"] = session.metrics.get("owned_stop_method")
    latencies = [record["end_to_end_seconds"] for record in report["recommendations"]]
    report["latency"] = ({"count": len(latencies), "minimum": min(latencies),
                          "median": statistics.median(latencies), "maximum": max(latencies),
                          "deadline_misses_over_3s": sum(value > 3 for value in latencies)}
                         if latencies else {"count": 0})
    report["delivery"] = delivery_summary(report["recommendations"], args.seconds, observed_replay_elapsed)
    report["latency"]["expected_deadline_misses_over_3s"] = (
        sum(value > 3 for value in latencies) + len(report["delivery"]["undelivered_past_3s"]))
    active_samples = [sample for sample in backlog_samples if sample["replay_elapsed_seconds"] <= args.seconds]
    report["reported_backlog"] = {"includes_publication_and_poll_delay": True,
        "peak_seconds": max((sample["maximum_backlog_seconds"] for sample in backlog_samples), default=0),
        "at_playback_stop_seconds": active_samples[-1]["maximum_backlog_seconds"] if active_samples else None,
        "after_drain_seconds": backlog_samples[-1]["maximum_backlog_seconds"] if backlog_samples else None,
        "maximum_queue_samples": max((sample["maximum_queue_samples"] for sample in backlog_samples), default=0),
        "samples": list(backlog_samples)}
    report.update(capacity_result(report["recommendations"], registered_ids,
                                  cadence_complete=report["delivery"]["complete"], errors=report["errors"]))
    report["three_second_target_met"] = report["passed"] and report["latency"]["expected_deadline_misses_over_3s"] == 0
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-cloud", action="store_true")
    parser.add_argument("--matches", type=int, choices=range(1, 8), default=1)
    parser.add_argument("--seconds", type=float, default=15)
    parser.add_argument("--startup-timeout", type=float, default=90)
    parser.add_argument("--budget", type=float, default=0.25)
    args = parser.parse_args()
    if not 7 <= args.seconds <= 120 or not 10 <= args.startup_timeout <= 240:
        parser.error("Replay duration must be 7..120s and startup timeout 10..240s")
    if not args.allow_cloud or os.environ.get("MATCHPULSE_MONITOR", "").lower() != "true":
        parser.error("Paid probe requires --allow-cloud and MATCHPULSE_MONITOR=true")
    report = asyncio.run(probe(args))
    print(json.dumps(report, allow_nan=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
