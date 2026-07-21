"""Stream a synthesis to disk while (optionally) tee-ing chunks to a live client.

Two entry points share one guarantee — one synthesis produces both the streamed
bytes and the stored cache file, and a half-written file is never observable as
`ready` (we write `<dest>.part` and atomically `os.replace` on success):

- `synthesize_to_file` — the worker/batch path: stream straight to the cache
  file, no client.
- `tee_to_client` — the web/on-demand path: run `synthesize_to_file` as an
  independent background task (so a client disconnect can't abort it — the cache
  and the credits spent are never wasted) that also feeds an `asyncio.Queue`; the
  returned async iterator drains that queue for a StreamingResponse. Completion
  callbacks (the DB `ready`/`failed` upsert) run inside the background task, so
  they fire whether or not the client is still listening.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from .fish import stream_tts

log = logging.getLogger("episteme.tts.stream")

# Content types for the audio we serve (cache FileResponse + live StreamingResponse).
MEDIA_TYPES = {
    "opus": "audio/ogg",
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
    "pcm": "application/octet-stream",
}


def media_type_for(audio_format: str) -> str:
    return MEDIA_TYPES.get(audio_format, "application/octet-stream")


@dataclass(slots=True)
class StreamResult:
    path: Path
    bytes_written: int
    audio_format: str


async def synthesize_to_file(
    *,
    text: str,
    dest: Path,
    sink: Callable[[bytes], Awaitable[None]] | None = None,
    **synth_kwargs,
) -> StreamResult:
    """Stream `text` into `dest` (atomic via a `.part` temp file). If `sink` is
    given, each chunk is forwarded to it too (the web tee's queue feeder). Raises
    on failure after removing the partial file; `**synth_kwargs` go to
    `fish.stream_tts` (api_key, model, ref_id, params, audio_format, …)."""
    audio_format = synth_kwargs.get("audio_format", "opus")
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Unique temp name so concurrent syntheses of the same dest (e.g. two players
    # hitting the stream endpoint before either caches) don't clobber each other's
    # partial file.
    tmp = dest.with_name(f"{dest.name}.{uuid.uuid4().hex}.part")
    total = 0
    try:
        with tmp.open("wb") as f:
            async for chunk in stream_tts(text, **synth_kwargs):
                f.write(chunk)
                total += len(chunk)
                if sink is not None:
                    await sink(chunk)
        if total == 0:
            raise RuntimeError("Fish TTS produced no audio")
        _atomic_replace(tmp, dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return StreamResult(path=dest, bytes_written=total, audio_format=audio_format)


def _atomic_replace(tmp: Path, dest: Path) -> None:
    """`os.replace(tmp, dest)` is atomic and overwrites on POSIX and native
    Windows, but the Docker Desktop bind-mount FS (virtiofs/9p) doesn't support
    rename-over-existing and returns EACCES even as root — so on failure we drop
    the stale destination and rename into the gap. The `.part` guarantee (a
    half-written file is never observable at `dest`) still holds; only the
    overwrite of an already-complete cache file is briefly non-atomic."""
    try:
        os.replace(tmp, dest)
    except OSError:
        dest.unlink(missing_ok=True)
        os.replace(tmp, dest)


# Keep strong refs to in-flight background syntheses — asyncio only weakly
# references tasks, so without this a tee task could be GC'd mid-synthesis.
_INFLIGHT: set[asyncio.Task] = set()


async def tee_to_client(
    *,
    text: str,
    dest: Path,
    on_success: Callable[[StreamResult], Awaitable[None]],
    on_failure: Callable[[BaseException], Awaitable[None]],
    **synth_kwargs,
) -> AsyncIterator[bytes]:
    """Return an async iterator of audio chunks for a StreamingResponse while the
    synthesis+cache runs as an independent background task. The task survives a
    client disconnect (the iterator just stops being drained); `on_success` /
    `on_failure` run inside it, so the DB row is updated either way."""
    queue: asyncio.Queue[bytes | None] = asyncio.Queue()  # unbounded: put never blocks

    async def run() -> None:
        try:
            result = await synthesize_to_file(
                text=text, dest=dest, sink=queue.put, **synth_kwargs
            )
            await on_success(result)
        except BaseException as exc:  # noqa: BLE001 — reported via on_failure
            log.warning("tee synthesis for %s failed: %s", dest.name, exc)
            try:
                await on_failure(exc)
            except Exception:  # pragma: no cover — callback best-effort
                log.debug("on_failure callback raised", exc_info=True)
        finally:
            await queue.put(None)  # sentinel: stop the client iterator

    task = asyncio.ensure_future(run())
    _INFLIGHT.add(task)
    task.add_done_callback(_INFLIGHT.discard)

    async def body() -> AsyncIterator[bytes]:
        while True:
            chunk = await queue.get()
            if chunk is None:
                return
            yield chunk

    return body()
