"""TTS narration: the deterministic script builder, the Fish provider framing,
the tee-to-file coordinator, and the pure voice-default logic.

Stage-level behavior (DB selection, live WebSocket) isn't unit-tested here —
there is no DB or network in unit tests — but every pure piece the stage and
endpoint compose is: request framing, streaming-to-disk atomicity, and default
resolution.
"""

from types import SimpleNamespace

import pytest

from episteme.tts import (
    Voice,
    build_script,
    flatten_params,
    param_schema_for,
    parse_params,
    pick_default,
    script_hash,
    synthesize,
    synthesize_to_file,
    tee_to_client,
)
from episteme.tts import fish as fish_module
from episteme.tts import stream as stream_module
from episteme.tts.fish import build_request


def _post(title="", summary="", sections=None):
    return SimpleNamespace(title=title, summary=summary, sections=sections or [])


# --- build_script ----------------------------------------------------------------


def test_build_script_orders_title_summary_then_sections():
    post = _post(
        title="A New Look at Europa",
        summary="Ice, oceans, and maybe life.",
        sections=[
            {"type": "prose", "text": "The moon **Europa** hides a deep ocean."},
            {"type": "key_points", "items": ["Salty water", "Thin crust"]},
        ],
    )
    script = build_script(post)
    lines = script.split("\n\n")
    assert lines[0] == "A New Look at Europa."
    assert lines[1] == "Ice, oceans, and maybe life."
    assert "Europa hides a deep ocean" in script
    assert "**" not in script
    assert "Salty water." in script and "Thin crust." in script


def test_build_script_skips_quiz_and_reads_media_captions():
    post = _post(
        title="T",
        sections=[
            {"type": "image", "url": "http://x/a.jpg", "caption": "A galaxy cluster"},
            {"type": "quiz", "question": "Q?", "choices": ["a", "b"], "answer_index": 0,
             "explanation": "because"},
            {"type": "chart", "spec": {}, "caption": ""},
        ],
    )
    script = build_script(post)
    assert "A galaxy cluster." in script
    assert "Q?" not in script
    assert "because" not in script


def test_build_script_strips_markdown_links():
    post = _post(summary="See [the paper](https://example.com/paper) for details.")
    script = build_script(post)
    assert "the paper" in script
    assert "https://example.com/paper" not in script
    assert "[" not in script and "]" not in script


def test_script_hash_is_stable_and_content_sensitive():
    a = build_script(_post(title="Same", summary="text"))
    b = build_script(_post(title="Same", summary="text"))
    c = build_script(_post(title="Same", summary="different"))
    assert script_hash(a) == script_hash(b)
    assert script_hash(a) != script_hash(c)


def test_build_script_empty_when_nothing_audible():
    post = _post(sections=[{"type": "quiz", "question": "Q", "choices": ["a", "b"],
                            "answer_index": 0, "explanation": "e"}])
    assert build_script(post).strip() == ""


# --- Fish request framing --------------------------------------------------------


def test_build_request_opus_frames_params():
    req = build_request(
        text="",
        ref_id="voice-123",
        params={"temperature": 0.4, "top_p": 0.5, "prosody": {"speed": 0.9, "volume": 2}},
        audio_format="opus",
        opus_bitrate=64000,
        mp3_bitrate=128,
        latency="balanced",
    )
    assert req["format"] == "opus"
    assert req["opus_bitrate"] == 64000
    assert "mp3_bitrate" not in req
    assert req["reference_id"] == "voice-123"
    assert req["temperature"] == 0.4
    assert req["top_p"] == 0.5
    assert req["prosody"] == {"speed": 0.9, "volume": 2.0}
    assert req["latency"] == "balanced"


def test_build_request_mp3_uses_mp3_bitrate_not_opus():
    req = build_request(
        text="", ref_id=None, params={}, audio_format="mp3",
        opus_bitrate=64000, mp3_bitrate=192, latency="normal",
    )
    assert req["format"] == "mp3"
    assert req["mp3_bitrate"] == 192
    assert "opus_bitrate" not in req
    assert req["reference_id"] is None


def test_build_request_omits_prosody_when_absent():
    req = build_request(
        text="", ref_id="v", params={"temperature": 0.7}, audio_format="opus",
        opus_bitrate=64000, mp3_bitrate=128, latency="balanced",
    )
    assert "prosody" not in req


async def _fake_stream(*chunks):
    async def gen(text, **kwargs):
        for c in chunks:
            yield c
    return gen


async def test_synthesize_buffers_the_stream(monkeypatch):
    monkeypatch.setattr(fish_module, "stream_tts", await _fake_stream(b"foo", b"bar"))
    result = await synthesize("hello", api_key="k", model="s2.1-pro", ref_id="voice-123")
    assert result.audio == b"foobar"
    assert result.model == "s2.1-pro"
    assert result.voice == "voice-123"


async def test_synthesize_requires_api_key():
    # api_key is validated before any network work, so no monkeypatch needed.
    with pytest.raises(RuntimeError):
        await synthesize("hi", api_key="", model="s2.1-pro")


# --- tee-to-file (streaming synthesis + cache) -----------------------------------


async def test_synthesize_to_file_writes_atomically(monkeypatch, tmp_path):
    monkeypatch.setattr(stream_module, "stream_tts", await _fake_stream(b"ab", b"cd"))
    dest = tmp_path / "1-voice.opus"
    result = await synthesize_to_file(
        text="x", dest=dest, api_key="k", model="m", audio_format="opus"
    )
    assert dest.read_bytes() == b"abcd"
    assert result.bytes_written == 4
    assert not (tmp_path / "1-voice.opus.part").exists()


async def test_synthesize_to_file_cleans_up_partial_on_error(monkeypatch, tmp_path):
    async def failing(text, **kwargs):
        yield b"ab"
        raise RuntimeError("boom")

    monkeypatch.setattr(stream_module, "stream_tts", failing)
    dest = tmp_path / "2-voice.opus"
    with pytest.raises(RuntimeError, match="boom"):
        await synthesize_to_file(text="x", dest=dest, api_key="k", model="m")
    assert not dest.exists()
    assert not (tmp_path / "2-voice.opus.part").exists()


async def test_synthesize_to_file_empty_output_is_error(monkeypatch, tmp_path):
    monkeypatch.setattr(stream_module, "stream_tts", await _fake_stream())
    dest = tmp_path / "3-voice.opus"
    with pytest.raises(RuntimeError):
        await synthesize_to_file(text="x", dest=dest, api_key="k", model="m")
    assert not dest.exists()


async def test_tee_to_client_streams_and_caches(monkeypatch, tmp_path):
    monkeypatch.setattr(stream_module, "stream_tts", await _fake_stream(b"a", b"b", b"c"))
    dest = tmp_path / "4-voice.opus"
    successes = []

    async def on_success(result):
        successes.append(result)

    async def on_failure(exc):  # pragma: no cover — not expected here
        raise exc

    body = await tee_to_client(
        text="x", dest=dest, on_success=on_success, on_failure=on_failure,
        api_key="k", model="m", audio_format="opus",
    )
    chunks = [chunk async for chunk in body]
    assert chunks == [b"a", b"b", b"c"]
    # The sentinel arrives only after the background task ran on_success + cached.
    assert dest.read_bytes() == b"abc"
    assert len(successes) == 1
    assert successes[0].path == dest


async def test_tee_to_client_reports_failure(monkeypatch, tmp_path):
    async def failing(text, **kwargs):
        raise RuntimeError("nope")
        yield  # pragma: no cover — makes this an async generator

    monkeypatch.setattr(stream_module, "stream_tts", failing)
    dest = tmp_path / "5-voice.opus"
    failures = []

    async def on_success(result):  # pragma: no cover — not expected here
        raise AssertionError("should not succeed")

    async def on_failure(exc):
        failures.append(exc)

    body = await tee_to_client(
        text="x", dest=dest, on_success=on_success, on_failure=on_failure,
        api_key="k", model="m", audio_format="opus",
    )
    chunks = [chunk async for chunk in body]
    assert chunks == []  # nothing streamed
    assert len(failures) == 1
    assert not dest.exists()


# --- voice default resolution (pure) ---------------------------------------------


def _voice(vid, order):
    return Voice(id=vid, label=vid, sort_order=order)


def test_pick_default_prefers_configured_override():
    voices = [_voice("a", 0), _voice("b", 1)]
    assert pick_default(voices, "b") == "b"


def test_pick_default_falls_back_to_lowest_sort_order():
    voices = [_voice("b", 1), _voice("a", 0)]
    assert pick_default(voices, "") == "a"
    assert pick_default(voices, None) == "a"
    assert pick_default(voices, "bogus") == "a"


def test_pick_default_empty_catalog_is_none():
    assert pick_default([], "anything") is None
    assert pick_default([], None) is None


def test_voice_ref_id_defaults_to_id():
    assert _voice("a", 0).ref_id == "a"
    assert Voice(id="a", label="A", provider_voice_id="prov-1").ref_id == "prov-1"


# --- provider param schema: parse + flatten (pure) -------------------------------


def test_parse_params_builds_nested_bag_from_dotted_form_keys():
    form = {"temperature": "0.8", "top_p": "", "prosody.speed": "0.95", "prosody.volume": "0"}
    params = parse_params("fish", form.get)
    assert params == {"temperature": 0.8, "prosody": {"speed": 0.95, "volume": 0.0}}
    assert "top_p" not in params  # blanks omitted


def test_parse_params_ignores_keys_outside_provider_schema():
    form = {"temperature": "0.7", "bogus": "9", "prosody.pitch": "3"}
    assert parse_params("fish", form.get) == {"temperature": 0.7}


def test_parse_params_unknown_provider_is_empty():
    assert parse_params("nope", {"temperature": "0.7"}.get) == {}


def test_parse_params_skips_non_numeric_number_fields():
    assert parse_params("fish", {"temperature": "abc"}.get) == {}


def test_flatten_params_dots_nested_keys():
    flat = flatten_params({"temperature": 0.7, "prosody": {"speed": 0.95, "volume": 0}})
    assert flat == {"temperature": 0.7, "prosody.speed": 0.95, "prosody.volume": 0}


def test_flatten_params_roundtrips_with_parse():
    original = {"temperature": 0.7, "prosody": {"speed": 0.95}}
    flat = flatten_params(original)
    assert parse_params("fish", lambda k: str(flat[k]) if k in flat else "") == original


def test_param_schema_for_fish_covers_the_fish_keys():
    keys = {f.key for f in param_schema_for("fish")}
    assert {"temperature", "top_p", "prosody.speed", "prosody.volume"} <= keys
    assert param_schema_for("unknown") == []
