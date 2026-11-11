"""Offline validation of the actual production Director; no inference or cloud calls.

Run from the application root with ``python -m touchline.ml_inference.evaluate_director``.
Research inputs are read-only. Event annotations are read only after all score-driven
selections are frozen. The original GRU and logistic control are distinct models.
"""

import argparse
import hashlib
import heapq
import json
import math
from bisect import bisect_right
from collections import Counter
from dataclasses import asdict
from pathlib import Path

import numpy as np

from touchline.replay.director import DIRECTOR_MODES, Director
from touchline.replay import director as director_module
from touchline.paths import INFERENCE_ARTIFACTS_DIR

LEGACY_SHA = {
    "baseline": "02de83095e5eaaf2f22978e89eec8515633ecd680ba0e9834cfb46d8cbea9319",
    "logistic": "36587982dd55ac018386307641be483978205788a75868c47ab2bc7a082839b2",
}
SHOT_TYPES = {"Shots on target", "Shots off target", "Goal"}
SCOPES = {"all_qualifying": None, "shot_attempts": SHOT_TYPES, "goals": {"Goal"}}


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def checked(path, expected):
    if path.stat().st_size != expected["bytes"] or digest(path) != expected["sha256"]:
        raise ValueError(f"Input checksum mismatch: {path.name}")
    return path


def validate_rows(rows, scores):
    """Keep official validation identities and exact completed-window endpoints."""
    if not rows or len(rows) != len(scores):
        raise ValueError("One score per nonempty validation row is required")
    keys = set()
    for row, score in zip(rows, scores, strict=True):
        start, end = row["observation_start_seconds"], row["observation_end_seconds"]
        key = row["match_id"], row["half"], end
        if (row.get("split") != "valid" or row["half"] not in (1, 2)
                or not math.isfinite(start) or not math.isfinite(end) or start < 0
                or abs(end - start - 5) > 1e-6 or abs((end - 5) % 2) > 1e-6 or key in keys):
            raise ValueError("Invalid, duplicate or non-validation five-second grid row")
        if not math.isfinite(float(score)) or not 0 <= score <= 1:
            raise ValueError("Finite sigmoid scores in [0,1] are required; missing is not zero")
        keys.add(key)


def load_validation(research_root):
    """Read only the release's four validation matches and hash-bound cached scores."""
    base = Path(research_root) / "artifacts/autonomous-v1"
    release = base / "delivery-v1"
    manifest = read(release / "SHA256SUMS.json")["files"]
    plan = read(checked(release / "evaluation/plan.json", manifest["evaluation/plan.json"]))
    matches = sorted(plan["cohort"]["selected"]["valid"])
    if len(matches) != 4 or plan["test_accessed"] is not False:
        raise ValueError("Expected four official validation matches with no test access")
    rows_path = checked(release / "audit/grid-valid-rows.json", manifest["audit/grid-valid-rows.json"])
    rows = read(rows_path)
    if sorted({row["match_id"] for row in rows}) != matches:
        raise ValueError("Released score rows differ from the selected validation cohort")
    score_path = checked(release / "audit/grid-valid-scores.npy", manifest["audit/grid-valid-scores.npy"])
    predictions = {"new_temporal_gru": np.load(score_path, allow_pickle=False)}
    provenance = {"new_temporal_gru": {"score_sha256": digest(score_path),
        "checkpoint_sha256": manifest["models/temporal-run-v2/classifier.pt"]["sha256"],
        "head": "shots3_10", "score_input": "released raw sigmoid; no centering/calibration"}}
    legacy = {name: {} for name in LEGACY_SHA}
    half_ends, receipts = {}, {}
    for match in matches:
        for half in (1, 2):
            key = f"{match}/{half}_720p.mkv"
            directory = base / "halves" / hashlib.sha256(key.encode()).hexdigest()[:24]
            receipt_path = directory / "receipt.json"
            if digest(receipt_path) != plan["cohort"]["receipt_sha256"][key]:
                raise ValueError("Validation preparation receipt changed")
            receipt = read(receipt_path)
            identity = receipt["identity"]
            if (not receipt["complete"] or not receipt["admitted"] or not receipt["grid_is_label_free"]
                    or identity["split"] != "valid" or identity["half_key"] != key
                    or identity["legacy_checkpoint_sha256"] != LEGACY_SHA
                    or identity["audio_revision"] != "autonomous-native-sample-clock-v1"):
                raise ValueError("Frozen original-model/validation/audio provenance mismatch")
            payloads = {p["name"]: p for p in receipt["payloads"]}
            source_rows = read(checked(directory / "grid-rows.json", payloads["grid-rows.json"]))
            for name in LEGACY_SHA:
                filename = f"grid-legacy-{name}.npy"
                values = np.load(checked(directory / filename, payloads[filename]), allow_pickle=False)
                validate_rows(source_rows, values)
                legacy[name].update({r["window_id"]: (r, float(s))
                                     for r, s in zip(source_rows, values, strict=True)})
            half_ends[(match, half)] = receipt["full_half_duration_seconds"]
            receipts[key] = digest(receipt_path)
    for name, lookup in legacy.items():
        values = []
        for row in rows:
            original, value = lookup[row["window_id"]]
            if original != row:
                raise ValueError("Cached original-model timeline identity mismatch")
            values.append(value)
        model_name = "original_deployed_gru" if name == "baseline" else "aggregate_logistic_reference"
        predictions[model_name] = np.asarray(values)
        provenance[model_name] = {"checkpoint_sha256": LEGACY_SHA[name],
            "input_audio": "same repaired native-sample-clock v3 audio as new model; frozen old weights",
            "score_sha256": hashlib.sha256(np.asarray(values, dtype="<f8").tobytes()).hexdigest()}
    for scores in predictions.values():
        validate_rows(rows, scores)
    for (match, half), duration in half_ends.items():
        ends = sorted(r["observation_end_seconds"] for r in rows
                      if r["match_id"] == match and r["half"] == half)
        if ends != list(range(5, math.floor(duration) + 1, 2)):
            raise ValueError("Complete label-free score grid is required")
    context = {"matches": matches, "independent_matches": 4, "test_accessed": False,
               "rows": len(rows), "rows_sha256": digest(rows_path), "models": provenance,
               "receipt_sha256": receipts, "yolo_frames_inferred": 0, "models_retrained": 0,
               "score_quantiles": {name: dict(zip(("min", "p25", "median", "p75", "p95", "p99", "max"),
                    np.quantile(values, [0, .25, .5, .75, .95, .99, 1]).tolist(), strict=True))
                    for name, values in predictions.items()}}
    return rows, predictions, half_ends, context


def replay_director(rows, scores, half_ends, *, mode="balanced", delay=1.0, ack_delay=0.25):
    """Freeze a displayed selection trace using real score validation and ACK commits.

    Playback is a healthy 1x half-local clock, with complete simultaneous score
    arrivals after ``delay``. ``ack_delay`` is extra backend-to-display/ACK delay.
    Cached-cohort cooldown expiry is evaluated on a 100ms timer, as in the session.
    A switch commits only on a still-current, fresh ACK, never on recommendation.
    """
    validate_rows(rows, scores)
    if any(not math.isfinite(v) or v < 0 for v in (delay, ack_delay)):
        raise ValueError("Delays must be finite and nonnegative")
    matches = sorted({row["match_id"] for row in rows})
    halves = sorted({row["half"] for row in rows})
    result = {"sessions": [], "switches": [], "rejected_records": 0, "rejected_acks": 0,
              "rejected_ack_reasons": Counter(), "recommendations": 0, "decision_reasons": Counter()}
    for half in halves:
        end = min(half_ends[(m, half)] for m in matches)
        director = Director(matches, session_id="offline-validation", model_version="frozen-score-timeline",
                            mode="real", director_mode=mode)
        timeline = [{"start": 0.0, "match_id": director.current_match_id,
                     "kind": "initial", "observation_end": None}]
        cohorts = {}
        for row, value in zip(rows, scores, strict=True):
            if row["half"] == half and row["observation_end_seconds"] + delay < end:
                cohorts.setdefault(row["observation_end_seconds"], {})[row["match_id"]] = float(value)
        events = []
        serial = 0

        def schedule(at, kind, payload=None, *, end=end, queue=events):
            nonlocal serial
            serial += 1
            if at < end:
                heapq.heappush(queue, (at, serial, kind, payload))

        for endpoint, cohort in sorted(cohorts.items()):
            if len(cohort) != len(matches):
                raise ValueError("Incomplete simultaneous validation cohort")
            schedule(endpoint + delay, "scores", (endpoint, cohort))
        latest = None
        proposal_sequence = 0
        worker_sequence = 0
        while events:
            now, _, kind, payload = heapq.heappop(events)
            if kind == "ack":
                sequence, decision, emitted_at = payload
                rejection = None
                if latest is None or sequence != latest[0]:
                    rejection = "superseded_recommendation"
                elif now > decision["valid_until"] + 1e-9:
                    rejection = "expired_score"
                elif now - decision["observation_end"] > director.maximum_playback_lag + 1e-9:
                    rejection = "stale_observation"
                if rejection is not None:
                    result["rejected_acks"] += 1
                    result["rejected_ack_reasons"][rejection] += 1
                elif decision["selected_match_id"] != director.current_match_id:
                    director.commit_selection(decision["selected_match_id"], now=now,
                        kind=decision["selection_kind"], reason=decision["selection_reason"],
                        playback_position=now, score=decision["scores"][decision["selected_match_id"]])
                    switch = {"half": half, "start": now, "match_id": director.current_match_id,
                              "kind": "automatic", "observation_end": decision["observation_end"],
                              "emitted_at": emitted_at, "observation_to_display_seconds": now - decision["observation_end"]}
                    timeline.append(switch)
                    result["switches"].append(switch)
                    expiry = now + director.minimum_switch_seconds
                    schedule(math.ceil((expiry - 1e-9) * 10) / 10, "cooldown")
            elif kind == "scores":
                endpoint, cohort = payload
                worker_sequence += 1
                director.ingest({"session_id": director.session_id, "model_version": director.model_version,
                    "clock_epoch": 0, "worker_sequence": worker_sequence, "score_kind": "model",
                    "scores": [{"match_id": match, "score": cohort[match], "sequence": worker_sequence,
                        "session_id": director.session_id, "model_version": director.model_version,
                        "clock_epoch": 0, "observation_start": endpoint - 5, "observation_end": endpoint,
                        "produced_at": now, "latency_seconds": delay} for match in matches]},
                    now=now, playback_position=now)
            # Only new cohorts, ACKs, and cooldown expiry can change this healthy
            # fixed-mode simulation; other session timer ticks are no-ops.
            decision = director.decide(now=now, playback_position=now, commit=False)
            result["decision_reasons"][director.decision_reason] += 1
            if decision is not None:
                result["recommendations"] += 1
                proposal_sequence += 1
                latest = proposal_sequence, decision
                if decision["selected_match_id"] != director.current_match_id:
                    schedule(now + ack_delay, "ack", (proposal_sequence, decision, now))
        result["rejected_records"] += director.rejected_records
        for i, segment in enumerate(timeline):
            segment["end"] = timeline[i + 1]["start"] if i + 1 < len(timeline) else end
        result["sessions"].append({"half": half, "end": end, "segments": timeline})
    result["decision_reasons"] = dict(result["decision_reasons"])
    result["rejected_ack_reasons"] = dict(result["rejected_ack_reasons"])
    return result


def reference_trace(matches, half_ends, policy):
    sessions, switches = [], []
    for half in sorted({h for _, h in half_ends}):
        end = min(half_ends[(m, half)] for m in matches)
        times = list(np.arange(0, end, 30)) if policy == "round_robin_30s" else [0.0]
        segments = []
        for i, at in enumerate(times):
            match = matches[i % len(matches)] if policy == "round_robin_30s" else policy.removeprefix("stay::")
            segment = {"start": float(at), "end": float(times[i + 1]) if i + 1 < len(times) else end,
                       "match_id": match, "kind": "reference" if i else "initial", "observation_end": None,
                       "half": half}
            segments.append(segment)
            if i:
                switches.append(segment)
        sessions.append({"half": half, "end": end, "segments": segments})
    return {"sessions": sessions, "switches": switches, "rejected_records": 0, "rejected_acks": 0}


def grouped_events(events, allowed_types=None):
    groups = {}
    for event in events:
        if event.get("split") != "valid":
            raise ValueError("Only official validation annotations are allowed")
        if not event["qualifying"] or (allowed_types is not None and event["event_type"] not in allowed_types):
            continue
        key = event["match_id"], event["half"], event["evaluation_group_id"]
        if not key[2]:
            raise ValueError("Original evaluation group identity is required")
        group = groups.setdefault(key, {"match_id": key[0], "half": key[1], "id": key[2],
                                       "timestamp": event["timestamp_seconds"], "types": []})
        group["timestamp"] = min(group["timestamp"], event["timestamp_seconds"])
        group["types"] = sorted(set(group["types"]) | {event["event_type"]})
    return list(groups.values())


def measure(trace, events, *, start=7.407):
    sessions = {session["half"]: session for session in trace["sessions"]}
    viewing_seconds = sum(max(0, session["end"] - start) for session in sessions.values())
    dwells = [max(0, segment["end"] - max(start, segment["start"]))
              for session in sessions.values() for segment in session["segments"]
              if segment["end"] > start]
    switches = [s for s in trace["switches"] if s["start"] >= start]
    output = {"switches": len(switches), "switches_per_minute": len(switches) * 60 / viewing_seconds,
              "viewing_seconds": viewing_seconds, "mean_viewing_duration_seconds": float(np.mean(dwells)),
              "median_viewing_duration_seconds": float(np.median(dwells)),
              "rejected_records": trace["rejected_records"], "rejected_acks": trace["rejected_acks"],
              "rejected_ack_reasons": trace.get("rejected_ack_reasons", {}), "scopes": {}}
    for scope, types in SCOPES.items():
        cases = []
        for event in grouped_events(events, types):
            session = sessions[event["half"]]
            at = event["timestamp"]
            if not start <= at < session["end"]:
                continue
            segments = session["segments"]
            segment = segments[bisect_right([s["start"] for s in segments], at) - 1]
            shown = event["match_id"] == segment["match_id"]
            lead = at - segment["start"] if shown else None
            is_switch = segment["kind"] != "initial"
            cases.append({**event, "shown": shown, "selected_match_id": segment["match_id"],
                          "viewing_started": segment["start"] if shown else None,
                          "new_switch_before_event": bool(shown and is_switch and lead > 0),
                          "advance_switch_within_10s": bool(shown and is_switch and 0 < lead <= 10),
                          "newly_anticipated_3_to_10s": bool(shown and is_switch and 3 <= lead <= 10),
                          "already_viewing_before_10s_warning_window": bool(shown and lead > 10),
                          "preselection_lead_seconds": lead,
                          "warning_seconds": lead if shown and is_switch and 0 < lead <= 10 else None})
        shown = [case for case in cases if case["shown"]]
        warnings = [case["warning_seconds"] for case in shown if case["warning_seconds"] is not None]
        output["scopes"][scope] = {"events": len(cases), "shown": len(shown), "missed": len(cases) - len(shown),
            "shown_fraction": len(shown) / len(cases) if cases else None,
            "advance_switches_within_10s": sum(c["advance_switch_within_10s"] for c in cases),
            "newly_anticipated_3_to_10s": sum(c["newly_anticipated_3_to_10s"] for c in cases),
            "already_viewing_before_10s_warning_window": sum(c["already_viewing_before_10s_warning_window"] for c in cases),
            "mean_warning_seconds_for_recent_advance_switches": float(np.mean(warnings)) if warnings else None,
            "cases": cases}
    return output


def write_summary(path, output):
    """Human-readable companion; full event cases and frozen traces stay in JSON."""
    assumptions = output["assumptions"]
    first_result = next(iter(output["results"].values()))
    counts = first_result["metrics"]["scopes"]
    half_ends = ", ".join(f"half {s['half']}: {s['end']:.3f}s" for s in first_result["trace"]["sessions"])
    lines = ["# Production Director validation replay", "",
        ("These are conditional results on four validation matches already used for research model selection. "
        "They are not held-out test accuracy or seven-source production validation. No policy was tuned."), "",
        ("The evaluator imports the application's actual `touchline.replay.director.Director`. "
        "Relaxed (20s, +0.25, 3 confirmations), Balanced (12s, +0.20, 2), and All Action "
        "(4s, +0.05, 1) remain unchanged. Bonuses are raw sigmoid score differences, unlike the "
        "research replay's causal median-centered log-odds. No centering or calibration is added."), "",
        ("The original deployed GRU is the frozen `02de8309…` checkpoint, separately identified "
        "from the aggregate logistic reference. Both old and new models use the same corrected "
        "v3 native-sample-clock audio and preserved cached visuals. This compares frozen weights "
        "on repaired inputs; it does not reproduce historical faulty-audio serving output."), "",
        "## Timing and support", "",
        (f"Backend-arrival delays: {assumptions['backend_arrival_delay_seconds']} seconds. "
        f"Extra display/ACK delay: {assumptions['additional_display_ack_delay_seconds']}s (assumed, not measured). "
        "Scores are validated at arrival; switches commit only after a still-current, fresh display ACK. "
        "The production 3s score-age and playback-lag limits remain active. Cached-cohort cooldown "
        "expiry is evaluated on the session's 100ms timer. Rejected ACKs never create switches or cooldowns."), "",
        (f"All runs use common half-local footage from {assumptions['common_evaluation_start_seconds']:.3f}s "
        f"to the shortest of the four source halves ({half_ends}). There are "
        f"{counts['all_qualifying']['events']} qualifying groups, {counts['shot_attempts']['events']} "
        f"shot groups and {counts['goals']['events']} exact Goal annotations on this support. Group "
        "identities and original timestamps are preserved; scopes overlap. Halves reset policy state "
        "and are not counted as independent matches. Only official validation rows are queried."), "",
        ("All score-driven runs start on the lexicographically first match, Burnley–Arsenal. "
        "This creates initial-choice sensitivity when a mode rarely switches. Every fixed-stay "
        "choice is reported below; the best retrospective fixed match is not a deployable oracle. "
        "The clock is healthy, continuous synchronized 1x playback, with simultaneous score cohorts; "
        "startup, stalls, source faults and variable network latency are outside this replay."), "",
        "## Actual production policy results", "",
        "| Model / mode / backend delay | Events shown | Shots shown | Goals shown | Switches/min | Mean dwell s | Event switches 3–10s early | Events already viewed >10s | Rejected records / ACKs |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for key, result in output["results"].items():
        if key.startswith("stay::") or key == "round_robin_30s":
            continue
        metrics = result["metrics"]
        all_events, shots, goals = [metrics["scopes"][name] for name in SCOPES]
        lines.append(f"| {key} | {all_events['shown']}/{all_events['events']} | "
            f"{shots['shown']}/{shots['events']} | {goals['shown']}/{goals['events']} | "
            f"{metrics['switches_per_minute']:.3f} | {metrics['mean_viewing_duration_seconds']:.2f} | "
            f"{all_events['newly_anticipated_3_to_10s']} | {all_events['already_viewing_before_10s_warning_window']} | "
            f"{metrics['rejected_records']} / {metrics['rejected_acks']} |")
    maximum_delay = max(assumptions["backend_arrival_delay_seconds"])
    lines.extend(["", f"## Event detail at {maximum_delay:g}s backend arrival delay", "",
        ("Goal warnings below are means only over goals reached by a new display switch 0–10s "
        "before the goal. A dash means no qualifying advance switch, not zero warning."), "",
        "| Model / mode | Missed events / shots / goals | Goals switched to 0–10s early | Goals switched 3–10s early | Goals already viewed >10s | Mean goal warning s | Mean shot warning s |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"])
    for key, result in output["results"].items():
        suffix = f"::delay-{maximum_delay:g}s"
        if not key.endswith(suffix):
            continue
        scopes = result["metrics"]["scopes"]
        all_events, shots, goals = [scopes[name] for name in SCOPES]
        goal_warning = goals["mean_warning_seconds_for_recent_advance_switches"]
        shot_warning = shots["mean_warning_seconds_for_recent_advance_switches"]
        goal_label = f"{goal_warning:.3f}" if goal_warning is not None else "—"
        shot_label = f"{shot_warning:.3f}" if shot_warning is not None else "—"
        lines.append(f"| {key.removesuffix(suffix)} | {all_events['missed']} / {shots['missed']} / {goals['missed']} | "
            f"{goals['advance_switches_within_10s']} | {goals['newly_anticipated_3_to_10s']} | "
            f"{goals['already_viewing_before_10s_warning_window']} | {goal_label} | {shot_label} |")
    lines.extend(["", "## Simple causal references", "",
        ("These bypass Director by definition and do not use event labels to choose a match. "
        "Round-robin starts at half-local 0s and changes every 30s; its phase differs from the "
        "research replay that starts after its first delayed observation."), "",
        "| Reference | Events shown | Shots shown | Goals shown | Switches/min |",
        "| --- | ---: | ---: | ---: | ---: |"])
    for key, result in output["results"].items():
        if not (key.startswith("stay::") or key == "round_robin_30s"):
            continue
        metrics = result["metrics"]
        all_events, shots, goals = [metrics["scopes"][name] for name in SCOPES]
        label = key.rsplit("/", 1)[-1]
        lines.append(f"| {label} | {all_events['shown']}/{all_events['events']} | "
            f"{shots['shown']}/{shots['events']} | {goals['shown']}/{goals['events']} | {metrics['switches_per_minute']:.3f} |")
    lines.extend(["", "## Metric interpretation", "",
        ("An event is shown only if its original source is selected at its exact annotation time. "
        "A recent advance switch occurs strictly before the event and at most 10s before it, "
        "with continuous viewing through the event. The narrower anticipation count requires "
        "3–10s. Already viewing means the continuous segment began more than 10s before the "
        "event; these cases are not credited as recent predictive switches. Mean warning is "
        "the actual display-switch-to-event lead only among recent advance switches, not "
        "a prediction probability or all-event anticipation claim. Mean dwell includes clipped "
        "first/last viewing segments on the common evaluation support. Misses, exact warning "
        "values, source-specific cases, rejection reasons and every switch are retained in JSON."), "",
        ("The new raw-score distribution has median "
        f"{output['context']['score_quantiles']['new_temporal_gru']['median']:.4f}, p99 "
        f"{output['context']['score_quantiles']['new_temporal_gru']['p99']:.4f}, and maximum "
        f"{output['context']['score_quantiles']['new_temporal_gru']['max']:.4f}. "
        "The existing +0.20/+0.25 raw-score bonuses and confirmations suppress most switches. "
        "The research result (10/12 goals under a different policy) must not be substituted for "
        "these production-mode results. Improving that behavior would be a separate documented "
        "policy/calibration study, not an invisible adjustment in this integration."), ""])
    path.write_text("\n".join(lines), encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--research-root", type=Path, required=True)
    parser.add_argument("--events", type=Path, required=True,
                        help="Explicit authorized validation events parquet file")
    parser.add_argument("--output", type=Path,
                        default=INFERENCE_ARTIFACTS_DIR / "winning-model/director-evaluation.json")
    parser.add_argument("--delays", nargs="+", type=float, default=[0, 1, 2, 2.157])
    parser.add_argument("--ack-delay", type=float, default=0.25)
    args = parser.parse_args(argv)
    rows, predictions, half_ends, context = load_validation(args.research_root)
    start = 5 + max(args.delays) + args.ack_delay
    frozen = {}
    for model, scores in predictions.items():
        for mode in DIRECTOR_MODES:
            for delay in args.delays:
                key = f"{model}::{mode}::delay-{delay:g}s"
                frozen[key] = replay_director(rows, scores, half_ends, mode=mode,
                                             delay=delay, ack_delay=args.ack_delay)
    for policy in [f"stay::{match}" for match in context["matches"]] + ["round_robin_30s"]:
        frozen[policy] = reference_trace(context["matches"], half_ends, policy)
    # Labels are queried only after all model/reference viewing traces are frozen.
    import pyarrow.parquet as pq
    columns = ["event_id", "match_id", "split", "half", "event_type", "timestamp_seconds",
               "qualifying", "evaluation_group_id"]
    events = pq.read_table(args.events, columns=columns,
        filters=[("split", "=", "valid"), ("match_id", "in", context["matches"])]).to_pylist()
    output = {"version": "matchpulse-production-director-evaluation-v1", "context": context,
        "evaluator_source_sha256": digest(Path(__file__)),
        "director_source_sha256": digest(Path(director_module.__file__)),
        "events_file_sha256": digest(args.events), "settings": {k: asdict(v) for k, v in DIRECTOR_MODES.items()},
        "assumptions": {"backend_arrival_delay_seconds": args.delays,
            "additional_display_ack_delay_seconds": args.ack_delay, "common_evaluation_start_seconds": start,
            "score_produced_at": "observation end + backend-arrival delay; latency field contains that delay",
            "playback": "healthy synchronized continuous 1x actual clock; no startup, stall, or network jitter",
            "cached_cohort_timer_seconds": 0.1, "maximum_score_age_seconds": 3,
            "maximum_playback_lag_seconds": 3, "half_end": "shortest simultaneous source; reset between halves",
            "warnings": "Actual display-switch lead; 3–10s anticipation is separate from already viewing >10s",
            "policy": "Exact production Director raw sigmoid bonuses, confirmations and actual ACK cooldown; no retuning",
            "old_gru": "Original deployed weights on repaired v3 audio, not historical faulty-audio production output",
            "reference_policies": "Fixed stays and 30s round-robin bypass Director by definition; no labels used",
            "limits": "Four validation matches used for prior research selection; not held-out production accuracy"},
        "results": {key: {"metrics": measure(trace, events, start=start), "trace": trace}
                    for key, trace in frozen.items()}}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    write_summary(args.output.with_suffix(".md"), output)
    print(str(args.output))
    for key, result in output["results"].items():
        metrics = result["metrics"]
        scopes = metrics["scopes"]
        print(key, "events", f"{scopes['all_qualifying']['shown']}/{scopes['all_qualifying']['events']}",
              "shots", f"{scopes['shot_attempts']['shown']}/{scopes['shot_attempts']['events']}",
              "goals", f"{scopes['goals']['shown']}/{scopes['goals']['events']}",
              "switch/min", round(metrics["switches_per_minute"], 3))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
