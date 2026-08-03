# 0021. A hand-set topic weight is absolute, does not decay, and is an anchor rather than a lock

- Date: 2026-07-30
- Status: accepted
- Rule: `set_topic` replaces the accumulated weight. It is excluded from `TOPIC_KINDS`. Only sliders the reader actually moved are recorded.

## Context

`/tune` shows a slider per topic. Dragging one has to mean something definite, and the obvious implementations are both wrong: adding a step means the panel does not read back what you set, and locking the value means later reading cannot move it.

## Decision

A dragged value is **absolute**. `set_topic` (kind added with `feedback.value`, migration `2813f1a15b8e`) replaces whatever the log had accumulated for that topic. Zero forgets it.

Two properties differ from every other signal:

- **It does not decay.** The panel must still read 4.0 next month. A control whose value drifts on its own is a control that lies. Same reasoning as `hide_source`.
- **It is an anchor, not a lock.** Later likes and steering still move the weight from there; only the history *before* the edit is discarded.

`set_topic` is deliberately **not** in `feedback.TOPIC_KINDS`, which drives the topic-chip state on cards and articles.

## The editing session is one transaction

Dragging several sliders is one editing session, so nothing posts until Save. The whole list is one form (`POST /tune/weights`, fields `w:<slug>`). Each topic is still its own event, so undo stays per-topic, but the batch is one transaction and **one replay**: `rebuild` is a full replay, so per-slider rebuilds would replay the log N times to reach the state the last one produces anyway.

Only moved sliders are recorded (`web/feedback.py:changed_weights`). The browser posts all of them and `dirty`, from app.js which holds each slider's rendered starting value, says which changed. **That comparison must be numeric**: a browser sanitizes a range value onto its step grid and drops the trailing zero, so a slider rendered at `-2.0` reads back `"-2"`. This bit on the first load, where string comparison marked four untouched whole-numbered sliders dirty before anyone touched them.

The slider list requires JavaScript and says so in a `<noscript>`. A documented no-JS fallback comparing submitted values against the stored profile existed but was unreachable, and was also the wrong rule, since stored weights decay past the slider's rounding on their own and it would have recorded sliders nobody touched. An absent `dirty` now records nothing. The "set a topic by name" box, a datalist of the whole vocabulary with free text still allowed via `topics.resolve`, works without JavaScript and reaches every topic including those with no slider yet.

## Hard blocks, on the same page

`block_keyword` and `unblock_keyword` (migration `bed89c48b376`), plus an outlet picker and an × on every chip.

Unblocking a **keyword** is a counter-event folded in time order, because a keyword block can come from a natural-language refusal, and deleting that statement to lift the block would also delete the topic weights it set.

Unblocking a **source** instead DELETES the `hide_source` events. A source block has no other origin, so that is an exact undo, and it keeps the per-post hide button truthful: the button renders as pressed from the event's existence, so a counter-event would leave it claiming the outlet is hidden.

`profile.normalize_keyword` is the single definition of a keyword's identity. If a block from a statement and an unblock from a chip normalized differently, the × would silently do nothing.

## Verified live, 2026-07-30, in the browser with real drags

Clean dirty state on load; centre-click gives 0.0 with fill, readout and Save reacting; two sliders dragged produced `Saved 2 weights.`, exactly 2 events, the 4 untouched sliders unchanged and eventless, and one coalesced rescore job; revert restored all six with zero requests; removing the statement-imposed `crypto` block left the statement and its −crypto weight intact; an outlet blocked then unblocked deleted the `hide_source` event and was offered again in the picker.
