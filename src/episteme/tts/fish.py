"""Fish Audio TTS provider — streaming over the live WebSocket endpoint.

Audio is streamed from Fish's `/v1/tts/live` WebSocket so playback can start
before the whole post is synthesized. We frame the protocol directly over
`httpx_ws` + `ormsgpack` (both already present as fish-audio-sdk deps) rather
than going through the pinned SDK, because that SDK's `TTSRequest` can't request
opus (`format` is Literal["wav","pcm","mp3"]) and lacks the newer knobs. Framing
it ourselves is also the honest provider seam: a future local model won't use
this SDK either.

Protocol (see the SDK's websocket.py): connect with an `Authorization: Bearer`
client header and a per-connection `model: <backend>` header, then send
`{event:"start", request:{…config…}}` → `{event:"text", text:…}` (one frame now;
many later is the LLM-streamed-script seam) → `{event:"stop"}`, and receive
`{event:"audio", audio:<bytes>}` chunks until `{event:"finish", reason:…}`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass

# Guarded so importing this module (e.g. the web app pulling in the voice
# catalog / stream helpers) doesn't hard-require the WS stack in a stripped
# environment — only actual synthesis does. These are real project deps, so this
# is None only when they're genuinely absent; stream_tts raises clearly then.
try:
    import httpx
    import ormsgpack
    from httpx_ws import aconnect_ws
except ModuleNotFoundError:  # pragma: no cover
    httpx = None
    ormsgpack = None
    aconnect_ws = None


@dataclass(slots=True)
class Narration:
    audio: bytes
    audio_format: str
    model: str
    voice: str | None


def build_request(
    *,
    text: str,
    ref_id: str | None,
    params: dict | None,
    audio_format: str,
    opus_bitrate: int,
    mp3_bitrate: int,
    latency: str,
) -> dict:
    """The TTSRequest payload sent in the live `start` frame. `params` is the
    provider-agnostic generation bag from the voice catalog; we read the keys
    Fish understands (temperature / top_p / prosody). Text is streamed as a
    separate `text` frame, so the request carries an empty `text`."""
    params = params or {}
    request: dict = {
        "text": text,
        "reference_id": ref_id or None,
        "format": audio_format,
        "normalize": True,
        "latency": latency,
        "temperature": float(params.get("temperature", 0.7)),
        "top_p": float(params.get("top_p", 0.7)),
    }
    if audio_format == "opus":
        request["opus_bitrate"] = opus_bitrate
    elif audio_format == "mp3":
        request["mp3_bitrate"] = mp3_bitrate
    prosody = params.get("prosody")
    if prosody:
        request["prosody"] = {
            "speed": float(prosody.get("speed", 1.0)),
            "volume": float(prosody.get("volume", 0.0)),
        }
    return request


async def stream_tts(
    text: str,
    *,
    api_key: str,
    model: str,
    ref_id: str | None = None,
    params: dict | None = None,
    audio_format: str = "opus",
    opus_bitrate: int = 64000,
    mp3_bitrate: int = 128,
    latency: str = "balanced",
    base_url: str = "https://api.fish.audio",
) -> AsyncIterator[bytes]:
    """Yield audio chunks for `text` as Fish synthesizes them. Raises RuntimeError
    on a missing key / stack or a server-signalled error; the caller (narrate
    stage, web stream endpoint) handles failures."""
    if not api_key:
        raise RuntimeError("fish_api_key is not configured")
    if aconnect_ws is None:  # pragma: no cover
        raise RuntimeError("httpx-ws / ormsgpack are not installed")

    request = build_request(
        text="",
        ref_id=ref_id,
        params=params,
        audio_format=audio_format,
        opus_bitrate=opus_bitrate,
        mp3_bitrate=mp3_bitrate,
        latency=latency,
    )
    async with httpx.AsyncClient(
        base_url=base_url, headers={"Authorization": f"Bearer {api_key}"}
    ) as client:
        async with aconnect_ws(
            "/v1/tts/live", client=client, headers={"model": model}
        ) as ws:
            await ws.send_bytes(ormsgpack.packb({"event": "start", "request": request}))
            await ws.send_bytes(ormsgpack.packb({"event": "text", "text": text}))
            await ws.send_bytes(ormsgpack.packb({"event": "stop"}))
            while True:
                data = ormsgpack.unpackb(await ws.receive_bytes())
                event = data.get("event")
                if event == "audio":
                    yield data["audio"]
                elif event == "finish":
                    if data.get("reason") == "error":
                        raise RuntimeError("Fish TTS reported a synthesis error")
                    return


async def synthesize(
    text: str,
    *,
    api_key: str,
    model: str,
    ref_id: str | None = None,
    params: dict | None = None,
    audio_format: str = "opus",
    opus_bitrate: int = 64000,
    mp3_bitrate: int = 128,
    latency: str = "balanced",
    base_url: str = "https://api.fish.audio",
) -> Narration:
    """Buffer the whole stream into one `Narration` — for callers/tests that want
    the complete blob rather than incremental chunks."""
    chunks = [
        chunk
        async for chunk in stream_tts(
            text,
            api_key=api_key,
            model=model,
            ref_id=ref_id,
            params=params,
            audio_format=audio_format,
            opus_bitrate=opus_bitrate,
            mp3_bitrate=mp3_bitrate,
            latency=latency,
            base_url=base_url,
        )
    ]
    return Narration(
        audio=b"".join(chunks), audio_format=audio_format, model=model, voice=ref_id or None
    )
