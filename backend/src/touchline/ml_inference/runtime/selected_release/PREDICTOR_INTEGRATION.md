# Selected research model runtime

`matchpulse.research.predictor` composes an already selected model. It does no
decoding, YOLO, image encoding, training, networking or policy selection. It
imports no sklearn or training scripts. Temporal checkpoints additionally need
PyTorch; the other classifier/composition paths use NumPy and the verified pure
audio/image helper source supplied with the release.

```python
from matchpulse.research.predictor import load_selected_predictor, Observation

model = load_selected_predictor(
    model_directory,
    expected_manifest_sha256=release_inventory_manifest_sha256,
    # Required only for image/ensemble models, using the exact release snapshot:
    # builder_paths={"embeddings": release_embedding_helper_path},
    # Required only for temporal models:
    # head="shots0_5",
    # For ensembles, preserve the sealed sibling component tree:
    # release_root=release_bundle_root,
)
observation = Observation(
    match_id=original_match_id, half=half, endpoint_seconds=endpoint,
    # Already-v3-corrected raw; shape alone does not prove decoder compatibility:
    raw215=raw_float32_75_by_215,
)
score = model.predict(observation)
```

Every NEW autonomous model consuming raw215 was trained after replacing its five
audio channels210:215 using the exact sample-clock decoder
`e7772f027ddb2c66f3801329995c8c8a1c9a36e23e0c44d92aeaa7023c75c8a4`
and the public `audio_features(pcm,rate,allow_gaps=True,duration_seconds=5)`.
This includes compact/relative, history, temporal and fused image/audio models.
Visual channels0:210 are preserved. The public `from_file` decoder is unchanged;
an array with the correct shape does not establish this audio compatibility.
Ordinary `predict` intentionally continues to accept already-corrected raw
without an extra PCM argument. It never silently replaces channels.

For a raw array whose visual210 is correct but whose audio source contract is
different or unknown, explicitly use the copied decoder and repair API:

```python
from matchpulse.research.core_audio import decode_aligned_audio, repair_core_audio
from matchpulse.research.predictor import Observation, with_core_audio

# video_origin_seconds is original video stream.start_time * stream.time_base,
# not an audio offset. Native samples are cropped BEFORE endpoint resampling.
audio = decode_aligned_audio(
    original_video_path, start_seconds=endpoint-5, end_seconds=endpoint,
    video_origin_seconds=original_video_origin,
    decoder_path=release_root / 'builders/autonomous_audio.py',
)
current = Observation(original_match_id, half, endpoint,
    raw215=existing_visual_raw215, audio=audio, image=causal_image_input_if_needed)
fixed = with_core_audio(current)  # Copies raw; leaves visual210 bytes unchanged.
score = model.predict(fixed)
model.commit(fixed)  # Only for an actual2s grid decision, after every query.

# Explicit convenience APIs (compute once above when scoring AND committing):
# score = model.predict_with_core_audio(current)
# model.commit_with_core_audio(current)
# Or work directly with the raw copy:
# fixed_raw = repair_core_audio(existing_visual_raw215,audio,endpoint_seconds=endpoint)
```

No YOLO pass or visual re-extraction is needed for this repair. Aligned PCM may
also be supplied directly as `AudioInput`, with exact interval,16kHz rate and
decoder SHA; absence is explicit. Preserve NaN gaps and stereo channels. The
release includes `raw-input-schema.json`, `raw-input-provenance.json`, the exact
decoder source and a real Burnley PCM→core5→raw→published-score proof under
`audit/runtime-check.json`→`smoke.core_audio_rebuild`. The predeclared numeric
comparison allows core5 atol2e-6/rtol1e-5 across platforms while requiring exact
availability masks, original IDs/timestamps, decoder/audio-source SHA and visual
bytes. Bitwise status/max difference are reported. Tabular score parity remains
atol1e-10; temporal parity uses its tested bound. Material prediction changes
block release. Independent waveform admission gates and training data are
unchanged. Legacy reference checkpoints retain their separately recorded input
contract.

The expected SHA is the independently recorded release hash of `model.json`,
`ensemble.json`, or temporal `classifier.pt`. The loader never substitutes a
different input family or fills in missing modalities. Scores are uncalibrated.
The saved threshold is a validation operating point for original eligible
five-second clip-union coverage; it is not a future-stream coverage guarantee.

* Raw tabular and selected-head temporal models consume raw float32 `(75,215)`.
  Temporal loading requires an explicit head and byte-identical packaged
  `temporal.py` and `representation.py` source to their saved SHA contracts.
* Audio162 requires `audio=AudioInput(pcm,16000,start,end,True,decoder_sha256)`;
  relative+audio332 also requires raw215. PCM is exactly80,000 float mono/stereo
  samples spanning `[endpoint-5,endpoint)`, with NaN for genuine unavailable
  intervals. Supply PCM from the release's sample-clock decoder, cropped before
  resampling; its SHA is explicit. An absent source is represented explicitly by
  `AudioInput(None,None,start,end,False,decoder_sha256)`. The exported causal
  descriptor helper is checked by SHA before loading. No Mel features are
  inferred from the old215-dimensional array.
* Image128 requires `image=ImageInput(embeddings,target_times,source_times,
  encoder_weights_sha256)`; relative+image298 also requires raw215. Embeddings
  are online float32 `(samples,512)` from the exact frozen encoder. The runtime
  reproduces cached float16 quantization before float32 pooling. Targets follow
  half-local centres0.5,1.5,...; source PTS must be causal and within the recorded
  frame-age limit, with full five-second support. Targets at/after the endpoint
  are rejected. The release must provide the exact `autonomous_embeddings.py`
  path matching the model's saved helper SHA. Saved PCA is SHA-checked and must
  belong to the same training cohort. Only the mean512 is PCA-centred; the delta
  is projected without subtracting the PCA mean.
* History511 uses the current raw215 plus `CausalHistoryBuffer`. `predict` and
  `descriptor` are read-only. After all queries at an actual fixed-grid endpoint
  5,7,9,..., call `model.commit(observation)`. Never commit off-grid eligible
  queries. Half transitions reset causal context; `model.reset(match_id)` starts
  a fresh playback. Model metadata verifies the exact history builder SHA.
* The fixed ensemble requires raw215, PCM and causal image embeddings together.
  Preserve its sealed relative component paths. It verifies model/weights,
  schemas, target and cohort identities; clips the three scores to
  `[1e-6,1-1e-6]`; and takes the sigmoid of their equal-weight mean logits.

The separate `OnlineDirector` accepts only synchronized scalar match scores,
chooses before committing its current score histories, and reports when the
selection may take effect. Its fixed-match primary replay contract is distinct
from supplemental dynamic-source availability evaluation. Stateful predictor
and director calls should each be serialized within their own session.

Verification includes exact saved pilot raw scoring,
synthetic audio/image/history/temporal round trips, nonrepresentable float32
embedding quantization, fixed ensemble composition, missing/future input
rejection, source/weight SHA rejection, and a fresh process with no sklearn or
training-script imports, plus explicit core-audio copy/alignment/source checks.
Synthetic exports validate implementation, not model
quality. Final selected real-model release smoke remains a packaging gate.
