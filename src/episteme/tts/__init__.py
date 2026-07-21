"""Text-to-speech narration for posts (Fish Audio).

`build_script` turns a post into the spoken text; `stream_tts` / `synthesize`
turn that text into audio via the Fish Audio live WebSocket, and `stream.py`
tees the stream to the on-disk cache. The `voices` module is the DB-backed voice
catalog (each voice carries provider-agnostic generation params).

The script/synthesis split is a deliberate seam. Today `build_script` is
deterministic. The planned "preprocessing" step is a local-LLM pass that will
emit an emotion/pronunciation-marked script instead; it slots in ahead of the
provider without it changing. `stream_tts` is likewise the seam for a future
local synthesis provider and for read-along (Fish's timestamps endpoint).
"""

from .fish import Narration, stream_tts, synthesize
from .script import build_script, script_hash
from .stream import (
    StreamResult,
    media_type_for,
    synthesize_to_file,
    tee_to_client,
)
from .voices import (
    Voice,
    default_voice_id,
    delete_voice,
    get_voice,
    list_voices,
    pick_default,
    set_voice_enabled,
    upsert_voice,
)

__all__ = [
    "Narration",
    "stream_tts",
    "synthesize",
    "build_script",
    "script_hash",
    "StreamResult",
    "media_type_for",
    "synthesize_to_file",
    "tee_to_client",
    "Voice",
    "default_voice_id",
    "delete_voice",
    "get_voice",
    "list_voices",
    "pick_default",
    "set_voice_enabled",
    "upsert_voice",
]
