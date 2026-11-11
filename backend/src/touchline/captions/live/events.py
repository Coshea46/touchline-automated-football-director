"""Compact real provider transcripts to the bounded browser/IPC contract."""

from touchline.captions.identity import canonical_json

MAX_CAPTION_TEXT_CHARS = 4000
MAX_CAPTION_WORDS = 400
MAX_CAPTION_WORD_CHARS = 100
MAX_CAPTION_EVENT_BYTES = 65536


def latest_text(text, limit=MAX_CAPTION_TEXT_CHARS):
    """Keep an actual suffix, preferably starting at a word boundary.

    Count UTF-16 units too so the browser's JavaScript length check agrees for
    non-BMP characters. Ordinary provider text is returned completely unchanged.
    """
    if len(text) <= limit and sum(2 if ord(char) > 0xffff else 1 for char in text) <= limit:
        return text
    start, units = len(text), 0
    while start:
        size = 2 if ord(text[start - 1]) > 0xffff else 1
        if units + size > limit:
            break
        units += size
        start -= 1
    if start and not text[start - 1].isspace() and start < len(text) and not text[start].isspace():
        boundary = next((index for index in range(start, len(text)) if text[index].isspace()), None)
        if boundary is not None:
            start = boundary
    return text[start:].lstrip()


def compact_transcript(value):
    """Carry original-size numbers privately, never the discarded old prefix."""
    text = value.get('text', '')
    words = value.get('words')
    result = {'text': latest_text(text)}
    if isinstance(words, list):
        result['words'] = words[-MAX_CAPTION_WORDS:]
    original_chars, original_words = value.get('_provider_text_chars'), value.get('_provider_word_count')
    result['_provider_text_chars'] = (original_chars if type(original_chars) is int and original_chars >= len(text)
                                      else len(text))
    result['_provider_word_count'] = (original_words if type(original_words) is int and original_words >= 0
                                     else len(words) if isinstance(words, list) else 0)
    result['_transcript_compacted'] = bool(value.get('_transcript_compacted') is True
        or result['text'] != text or isinstance(words, list) and len(words) > MAX_CAPTION_WORDS)
    return result


def bounded_caption_event(event):
    """Keep latest words within the byte cap without shifting any timestamps."""
    result = dict(event)
    if event.get('state') in ('partial', 'committed'):
        result['text'] = latest_text(result.get('text', ''))
        if isinstance(result.get('words'), list):
            result['words'] = result['words'][-MAX_CAPTION_WORDS:]

    def fits(candidate):
        return len(canonical_json(candidate).encode('utf-8')) + 1 < MAX_CAPTION_EVENT_BYTES

    if fits(result):
        return result
    words = result.get('words')
    if isinstance(words, list) and words:
        # Find the largest suffix that fits; unusual multi-byte words may need
        # fewer than 400 entries. Text/anchor/word times remain provider values.
        low, high = 0, len(words)
        while low < high:
            count = (low + high + 1) // 2
            if fits({**result, 'words': words[-count:]}):
                low = count
            else:
                high = count - 1
        result['words'] = words[-low:] if low else []
    return result if fits(result) else None
