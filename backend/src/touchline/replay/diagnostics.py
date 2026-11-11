"""Bounded worker health labels and transport-failure classification."""

import re
from touchline.ml_inference.streaming.contract import MAX_GPU_WORKERS


def worker_diagnostics(snapshot):
    """Retain bounded protocol labels, never exception messages or source URLs."""
    states = {"starting", "ready", "running", "error", "stopped"}
    reasons = {"checkpoint_unavailable", "processing_error", "shard_error", "shard_stopped",
               "session_limit", "lease_expired", "control_stopped", "all_sources_ended", "paused"}
    phases = {"checkpoint", "resolve", "volume_reload", "validate", "load", "reader", "open", "decode", "audio", "audio_window", "detector",
              "visual", "assembly", "classifier", "scoring", "publication", "control"}
    codes = {"checkpoint_path_escape", "checkpoint_file_invalid", "checkpoint_file_too_large",
             "checkpoint_metadata_invalid", "checkpoint_schema_mismatch", "checkpoint_architecture_mismatch",
             "checkpoint_model_structure", "checkpoint_normalization_provenance", "checkpoint_normalization_invalid",
             "checkpoint_dataset_provenance", "checkpoint_threshold_invalid", "checkpoint_version_invalid",
             "checkpoint_audio_schema", "checkpoint_resolve_failed", "checkpoint_volume_reload_failed",
             "checkpoint_load_failed", "checkpoint_validate_failed"}

    def error_type(value):
        return (value if isinstance(value, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", value)
                and (value.endswith(("Error", "Exception"))
                     or value in ("KeyboardInterrupt", "InvalidStatusCode", "InvalidHandshake")) else None)

    def labels(value):
        if not isinstance(value, dict):
            value = {}
        reason = value.get("reason")
        state, phase, code = value.get("state"), value.get("error_phase"), value.get("error_code")
        return {"state": state if isinstance(state, str) and state in states else None,
                "reason": reason if isinstance(reason, str) and reason in reasons else error_type(reason),
                "error_type": error_type(value.get("error_type")),
                "error_phase": phase if isinstance(phase, str) and phase in phases else None,
                "error_code": code if isinstance(code, str) and code in codes else None}

    if not isinstance(snapshot, dict):
        snapshot = {}
    result = {"worker_" + key: value for key, value in labels(snapshot).items()}
    shards = snapshot.get("shard_errors", {})
    result["worker_shard_errors"] = {key: labels(shards[key]) for key in map(str, range(MAX_GPU_WORKERS)) if key in shards
                                      and isinstance(shards[key], dict)} if isinstance(shards, dict) else {}
    return result

def transient_transport_error(snapshot):
    """Only explicitly transport-labelled failures may restart a worker group."""
    def transport(record):
        return (isinstance(record, dict) and record.get("reason") == "processing_error"
                and record.get("error_phase") in ("control", "publication"))

    if transport(snapshot):
        return True
    shards = snapshot.get("shard_errors")
    return (snapshot.get("reason") == "shard_error" and isinstance(shards, dict)
            and 1 <= len(shards) <= MAX_GPU_WORKERS and set(shards) <= set(map(str, range(MAX_GPU_WORKERS)))
            and all(transport(record) for record in shards.values()))
