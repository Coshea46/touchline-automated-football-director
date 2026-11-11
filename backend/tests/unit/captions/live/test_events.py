"""Offline tests for unit captions live test_events."""


import asyncio
import json
import pytest
from touchline.captions.live.events import MAX_CAPTION_EVENT_BYTES, compact_transcript, latest_text
from touchline.captions.live.connection import BufferedRealtimeConnection
from touchline.captions.live.engine import RealtimeEngine
from touchline.captions.identity import canonical_json
from tests.support.captions.provider import RawConnection
from tests.support.captions.realtime import Audio, until
from tests.support.captions.session import event, setup
from tests.support.replay.session import playback


def test_normal_provider_text_and_word_timestamps_remain_unmodified():
    words = [{'text': 'A real phrase', 'start': .125, 'end': .875}]
    result = compact_transcript({'text': '  A real phrase.  ', 'words': words})
    assert result['text'] == '  A real phrase.  ' and result['words'] == words
    assert not result['_transcript_compacted']
    assert result['_provider_text_chars'] == 18 and result['_provider_word_count'] == 1


@pytest.mark.parametrize('length', [4001, 15001])
def test_adapter_and_engine_latest_transcript_and_words_reach_session_without_changing_anchor(length):
    async def scenario():
        session, worker, clock, _ = await setup()
        session.receive('owner', playback(session, position=5))
        latest, published, accepted, connections = [worker.updates[-1]], [], [], []
        suffix = ' LATEST_PROVIDER_WORD'
        text = ('old actual words ' * (length // 10))[:length - len(suffix)] + suffix
        words = [{'text': f'w{i}', 'start': i / 10000, 'end': (i + 1) / 10000}
                 for i in range(401)]

        class Provider(RawConnection):
            async def send(self, value):
                self.sends.append(value)
                self.handlers['partial_transcript']({'text': text})
                self.handlers['committed_transcript_with_timestamps']({'text': text, 'words': words})

        async def connect(_):
            raw = Provider(connections)
            wrapped = BufferedRealtimeConnection(raw)
            raw.handlers['session_started']({})
            return wrapped

        def publish(value):
            wire = canonical_json(value) + '\n'
            assert len(wire.encode('utf-8')) < MAX_CAPTION_EVENT_BYTES
            published.append(json.loads(wire))
            accepted.append(session._accept_caption_event(published[-1]))

        engine = RealtimeEngine(lambda: latest[0], publish, api_key_present=True,
            audio_factory=Audio, connect_factory=connect,
            identity_checker=lambda *args: latest[0]['source_identity'], wall_now=clock)
        task = asyncio.create_task(engine.run())
        try:
            await until(lambda: connections)
            session.receive('owner', playback(session, position=5.5))
            latest[0] = worker.updates[-1]
            await until(lambda: any(value['state'] == 'committed' for value in published))
            partial = next(value for value in published if value['state'] == 'partial')
            committed = next(value for value in published if value['state'] == 'committed')
            assert all(accepted)
            assert partial['text'] == committed['text'] == latest_text(text)
            assert text.endswith(committed['text']) and committed['text'].endswith('LATEST_PROVIDER_WORD')
            assert len(committed['text']) <= 4000 and len(committed['words']) == 400
            assert partial['source_anchor_seconds'] == committed['source_anchor_seconds'] == 5
            for actual, original in zip(committed['words'], words[-400:], strict=True):
                assert actual['text'] == original['text']
                assert actual['start_seconds'] == 5 + original['start']
                assert actual['end_seconds'] == 5 + original['end']
            assert engine.metrics['last_provider_text_chars'] == length
            assert engine.metrics['last_provider_word_count'] == 401
            assert engine.metrics['transcript_compactions'] >= 2
            assert engine.metrics['provider_sessions'] == 1
            assert engine.metrics['transcript_events_dropped'] == 0
        finally:
            engine.closed = True
            await task
            await session.close()

    asyncio.run(scenario())


def test_multibyte_transcript_and_word_payload_fits_ipc_and_retains_latest_absolute_times():
    async def scenario():
        session, worker, _, _ = await setup()
        session.receive('owner', playback(session, position=5))
        view, emitted = worker.updates[-1], []
        words = [{'text': '\u754c' * 97 + f'{i:03}', 'start_seconds': i / 1000,
                  'end_seconds': (i + 1) / 1000} for i in range(400)]
        text = '\u754c' * 4000
        engine = RealtimeEngine(lambda: view, emitted.append, audio_factory=object)
        try:
            engine.emit(view, 'committed', text=text, words=words, source_anchor_seconds=0)
            actual = emitted[-1]
            assert len((canonical_json(actual) + '\n').encode()) < MAX_CAPTION_EVENT_BYTES
            assert 0 < len(actual['words']) < 400 and actual['words'] == words[-len(actual['words']):]
            assert actual['text'] == text and actual['source_anchor_seconds'] == 0
            assert session._accept_caption_event(actual)
            assert engine.metrics['transcript_compactions'] == 1
        finally:
            engine._executor.shutdown(wait=False)
            await session.close()

    asyncio.run(scenario())


def test_utf16_browser_text_bound_and_sanitized_actual_payload_rejection_diagnostics():
    assert len(latest_text('\U0001f3c6' * 4000).encode('utf-16-le')) // 2 <= 4000

    async def scenario():
        session, _, _, _ = await setup()
        try:
            session.receive('owner', playback(session, position=5))
            assert not session._accept_caption_event(event(session, text='PRIVATE_TRANSCRIPT' * 400))
            assert session.metrics['caption_rejection_counts'] == {'invalid_text_size': 1}
            assert session.metrics['caption_last_rejection_reason'] == 'invalid_text_size'
            assert 'PRIVATE_TRANSCRIPT' not in canonical_json(session.metrics)
        finally:
            await session.close()

    asyncio.run(scenario())
