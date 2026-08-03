# 0035. Narration streams over a WebSocket and tees to disk, with the voice catalog in the database

- Date: 2026-07-21
- Status: accepted
- Rule: playback starts before synthesis finishes, and a disconnect still finishes the cache. Voices are rows, not constants.

## Context

Post narration through Fish Audio TTS. The first build was generate, then poll, then play: a full article's free-tier synthesis takes multiple minutes, so the reader pressed play and waited.

## Decision

**Stream from the WebSocket.** Audio comes from Fish's `/v1/tts/live` so playback starts in 1 to 2 seconds. We frame the protocol directly over `httpx_ws` plus `ormsgpack`, both now direct dependencies, because the pinned `fish-audio-sdk` `TTSRequest` cannot request opus.

**Tee the stream to disk.** `<file>.part` then an atomic `os.replace`, so one play both hears it live and caches it. A cache hit is served as a seekable `FileResponse`, opus as `audio/ogg`. The web tee runs synthesis as an independent task, so a client disconnect still finishes the cache and no credits are wasted.

Cache validity requires ready, plus a matching `script_hash`, `audio_format` and `params`. `post_audio` carries `provider` and `params` for provenance.

**The voice catalog is a database table.** `voices`, seeded by `seeds.seed_voices`, migration `cac5689addb7`: `id` (the catalog key, which is the Fish reference id), `label`, `provider` (default "fish"), `provider_voice_id`, `params` JSONB, `sort_order`, `enabled`.

`params` is deliberately provider-agnostic (`{temperature, top_p, prosody: {speed, volume}}`), so a future **local** TTS provider carries its own parameters with no schema change. That matters here: a local model on the same GPU is the obvious next step, and a Fish-shaped column set would have to be migrated to reach it.

`tts.voices` exposes async accessors plus a pure `pick_default`, which is unit-tested; the DB queries are not, since unit tests have no database.

**Podcast mode.** The post page has a "Continuous" toggle in localStorage. On `ended` it fetches `/api/posts/{id}/next`, the next feature in feed order, and navigates to `/post/{next}?autoplay=1&continuous=1&voice=`, which auto-plays. `static/narrate.js` drives it.

## Structure

`src/episteme/tts/`: `script.build_script` produces a deterministic spoken script from a post, and is **the seam a future LLM preprocessing pass replaces** (the TODO item about emotional tagging and pronunciation lands exactly there). `fish.stream_tts` is the provider, `stream.py` the tee coordinator, `store.upsert_post_audio` the shared write, `voices.py` the catalog.

## Fish model keys, verified 2026-07-21

The free tier is a **distinct key**, `s2.1-pro-free`. The paid `s2.1-pro`, `s2-pro` and `speech-1.6` return 402 without a paid balance. `tts_model` defaults to the free key and is sent verbatim as the `model` header. Free-tier synthesis of a full article is slow, multiple minutes, which is why `tts_enabled` defaults false and the nightly batch is off while on-demand streaming works regardless.

## Not built

**Read-along.** Swap `stream_tts` to Fish's text-to-speech-stream-with-timestamps, add `post_audio.timestamps` JSONB, and drive client word highlighting. The alignment source is `build_script`'s spoken text.
