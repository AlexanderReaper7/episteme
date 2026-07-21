---
name: tts-narration
description: Fish Audio TTS narration — DB voice catalog, streamed opus, podcast mode. Rebuilt 2026-07-21; free tier model key "s2.1-pro-free".
metadata:
  type: project
---

Post narration via Fish Audio TTS. Built 2026-07-21, reworked same day into a
streaming system. `src/episteme/tts/`: `script.build_script` (deterministic
spoken script from a post — the seam a future LLM preprocessing pass replaces),
`fish.stream_tts` (streaming provider), `stream.py` (tee-to-file coordinator),
`store.upsert_post_audio` (shared `post_audio` write), `voices.py` (DB-backed
catalog). See [[tts-streaming-architecture]] for the streaming internals.

**Voice catalog is a DB table** (`voices`, seeded by `seeds.seed_voices`, migration
`cac5689addb7`): `id` (catalog key = Fish reference_id) / label / provider
(default "fish") / provider_voice_id / `params` JSONB / sort_order / enabled.
`params` is provider-agnostic (`{temperature, top_p, prosody:{speed,volume}}`) so
a future LOCAL TTS provider carries its own params with no schema change. Voices:
David Attenborough `c39a76f685cf4f8fb41cd5d3d66b497d` (sort_order 0 = default),
Girl `ca3007f96ae7499ab87d27ea3599956a`. `tts.voices` exposes async accessors
(`list_voices`/`get_voice`/`default_voice_id`) + a pure `pick_default` (unit-
tested; the DB queries aren't, no DB in unit tests). Manage voices as DB rows.

**Streaming (opus @ 64 kbps):** audio streams from Fish's **WebSocket**
`/v1/tts/live` so playback starts in ~1–2 s. We frame the protocol directly over
`httpx_ws`+`ormsgpack` (now direct deps) — the pinned `fish-audio-sdk` TTSRequest
can't request opus. `POST /api/posts/{id}/narrate` (worker batch, nightly) and
`GET /api/posts/{id}/audio/stream?voice=` (web on-demand — the post page's primary
playback URL) both stream; the stream is **tee'd to disk** (`<file>.part` →
atomic `os.replace`) so a play both hears it live AND caches it. A cache hit is
served as a seekable `FileResponse` (opus → `audio/ogg`). The web tee runs the
synthesis as an independent task so a client disconnect still finishes the cache
(no wasted credits). `post_audio` gained `provider`/`params` (provenance); cache
validity requires ready + matching script_hash + audio_format + params.

**Podcast mode (auto-advance navigation):** post page has a "Continuous" toggle
(localStorage). On `ended` it `GET /api/posts/{id}/next` (next feature in feed
order) and navigates to `/post/{next}?autoplay=1&continuous=1&voice=` which
auto-plays. `static/narrate.js` drives it; the old generate→poll flow is gone.

**Read-along (NOT built):** future — swap `stream_tts` to Fish's
text-to-speech-stream-with-timestamps, add `post_audio.timestamps` JSONB, drive
client word highlighting. Alignment source is `build_script`'s spoken text.

**Fish model keys (verified 2026-07-21):** free tier is a DISTINCT key
`s2.1-pro-free`; paid `s2.1-pro`/`s2-pro`/`speech-1.6` return 402 without a paid
balance. `tts_model` defaults to `s2.1-pro-free`, sent verbatim as the `model`
header. Free-tier synth of a full article is SLOW (multi-minute).

**Config:** `FISH_API_KEY` in `.env`; `TTS_ENABLED=false` (nightly batch off;
on-demand streaming works regardless); `TTS_OPUS_BITRATE=64000`;
`TTS_DEFAULT_VOICE` overrides the catalog default.

**Rebuild to run the WORKER stage in the stack:** `docker compose up --build -d`
— worker uses baked-in src (no bind mount, unlike web). The web streaming path
runs on the bind-mounted src without a rebuild once deps are present (httpx_ws /
ormsgpack ship transitively with fish-audio-sdk).
