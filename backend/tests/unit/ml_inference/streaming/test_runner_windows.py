"""Offline tests for unit ml_inference streaming test_runner_windows."""


from tests.support.ml_inference.runner import Harness


def test_shared_batches_preserve_match_state_and_never_read_past_viewer_watermark(monkeypatch):
    harness = Harness(monkeypatch)
    harness.control_override = lambda h: {
        'active': not (len(h.features) == 2 and all(len(f.assembled) == 3 for f in h.features)),
    }
    result = harness.run()
    assert result['reason'] == 'control_stopped'
    assert all(f.assembled == [75, 105, 135] for f in harness.features)
    assert len(harness.detected) == 270
    assert len(set(harness.detected)) == len(harness.detected)
    assert result['metrics']['detector_frames'] == 270
    assert result['metrics']['max_queue_samples'] <= 32
    assert all(c.closed for c in harness.cursors)
    assert all(score['score_kind'] == 'mock' for score in result['scores'])
    assert all(score['observation_start'] == 4 and score['observation_end'] == 9 for score in result['scores'])
    for score in result['scores']:
        offset = next(m['offset_seconds'] for m in harness.spec['matches'] if m['match_id'] == score['match_id'])
        assert score['source_timestamp'] == offset + 9
    assert any(p['state'] == 'ready' for p in harness.published)


def test_eof_closes_streams_and_stops_after_the_last_complete_window(monkeypatch):
    harness = Harness(monkeypatch)
    harness.end_index = 75
    result = harness.run()
    assert result['reason'] == 'all_sources_ended'
    assert all(f.assembled == [75] for f in harness.features)
    assert result['metrics']['detector_frames'] == 150
    assert all(c.closed for c in harness.cursors)
    assert all(h['status'] == 'eof' for h in result['health'].values())


def test_partial_eof_cannot_emit_a_short_window_or_fake_audio_score(monkeypatch):
    harness = Harness(monkeypatch, matches=1)
    harness.end_index = 60
    result = harness.run()
    assert result['reason'] == 'all_sources_ended'
    assert not harness.features[0].assembled
    assert result['scores'] == [] and result['metrics']['windows'] == 0


def test_unavailable_audio_never_uses_silence_or_emits_a_score(monkeypatch):
    harness = Harness(monkeypatch, matches=1, seconds=40)
    harness.fail_audio = True
    result = harness.run()
    assert result['reason'] == 'all_sources_ended'
    assert result['scores'] == []
    assert not harness.features[0].assembled
    assert all(c.closed for c in harness.cursors)
    assert result['health']['match-0']['status'] == 'error'
