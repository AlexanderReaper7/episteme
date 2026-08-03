# 0030. Topic chips inherit the rating's direction, and saving a post is only a bookmark

- Date: 2026-08-03
- Status: accepted
- Rule: no rating means no chips. `save` records an event but contributes nothing to the profile.

## Context

Two reports. The feedback controls contradicted the gesture above them: a topic chip asked the reader to choose a direction they had just chosen. And saving a post was quietly training the profile.

## Decision

**Direction is inherited, never chosen again.** The user's framing: "selecting the topic is just being more specific about what was disliked." Chips appear **only after** a like or a dislike and carry that direction. Save sets no direction.

The orphan case is deliberate: undo the rating and an already-picked chip **stays**, because its event still exists and hiding it would make a recorded signal unreachable, while the unpicked chips go with the direction that gave them meaning.

Chips are on cards too. Hide-source stays article-only, because it removes content.

**`save` no longer touches the profile.** The user: "saving a post shouldnt add a preference. its just for bookmarking". Removed from `profile._CONTENT_SIGNALS`; `feedback_save_weight` retired.

The event is still recorded **and still carries its full `_snapshot`**. The log is canonical and the profile is replayed from it (see 0017), so what a signal is worth must stay retunable. Dropping the payload for a signal worth zero *today* would make that the one irreversible decision in the system.

## The root-cause fix underneath

Card buttons sent no context and the route defaulted to the article control set, so clicking Dislike on a **feed card** swapped in the article page's hide-source button, on a card that had deliberately rendered without it.

`with_topics: bool` became `variant: Literal["card", "article"]`, and **every** `hx-post` in the fragment carries it: a fragment that replaces itself has to state its own context, or the next render guesses.

The fragment also takes one context object (`fb`), built by `post_context` for a post and `feed_context` for a whole page, so there is a single contract wherever it renders and one `slug_index` per page rather than per card.

## Verified live, 2026-08-03, with real clicks

Card Dislike sent `variant=card` in every URL, showed no hide-source, and rendered chips reading "Less of" that post only `less_topic`. A chip produced exactly 2 feedback rows. Undoing the rating left the picked chip with its own undo URL and removed the unpicked ones. Both undone gave 0 rows.
