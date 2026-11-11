# Selected research model

Selected policy: `score_centered::temporal_gru_dense_shots3_10`. Model: `temporal_gru_dense_shots3_10`.

Model directory: `models/temporal-run-v2`; manifest/weights SHA: `d63fff64480d00835c14bc7c726e062b00278126d517e1e9eceb1abe0aafbd1e`. Temporal head: `shots3_10`. Saved normalization, all3 exact targets, model/runtime/schema hashes and training provenance live in classifier.pt for a temporal model.

Install the explicit wheel with the actual declared dependencies. For the verified Linux reference use `reference-linux-constraints.txt`; Windows NumPy pinning alone does not establish universal raw-builder equivalence. See `runtime-compatibility.json` and both fresh-wheel checks.

Use `matchpulse.research.predictor.load_selected_predictor(model_directory, expected_manifest_sha256=..., head=...)` and `Observation(match_id, half, endpoint_seconds, raw215=...)`. Raw input is float32[75,215], 15FPS over exactly the previous5 seconds. The last5 audio channels must use the v3 sample-clock contract; unchanged public from_file decoding alone does not certify this. For aligned PCM, supply `AudioInput` with exact start/end/sample rate/source-availability and decoder SHA, then call `predict_with_core_audio(observation)` to rebuild only raw[:,210:215], preserving visual210 bytes. The audited decoder is `builders/autonomous_audio.py`; see raw-input-schema.json/raw-input-provenance.json. No YOLO rerun is required.

Use `matchpulse.research.director.OnlineDirector(match_ids, policy='score_centered')` on a fixed match set with common support. Supply all simultaneous model scores at endpoints5,7,9,...; `step` selects before committing current history. It uses previous-only300s logit median, 30s finite-score warmup, staying bonus0.25 and lexical ties. Reset at each half. Missing scores remain explicit; do not substitute zero. The selected trace assumes1s prediction latency. `director-config.json` is authoritative.

The direct per-target operating thresholds stored in the checkpoint differ from the simultaneous-match director decision. Scores are ranking scores; calibration is not claimed. Four validation matches and selection among19 candidates limit statistical certainty; read the preserved evaluation and final research report before claiming generalization.

Runtime benchmarks repeat the same real window; they do not represent independent matches. Image cached-embedding checks, when present, do not execute the pixels-to-embedding encoder. No server/YOLO service is included.
