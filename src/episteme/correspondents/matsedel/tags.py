"""What a line is, when the code cannot tell and a human can.

`readers.is_label` reads one line and decides from its punctuation. That is all a
rule reading one line can do, and it is not enough: Vänerparken writes `BUFFÉ`,
then a number, then the dish, so `BUFFÉ` heads Monday's list - and on Friday the
numbered slots hold `GRILLBUFFÉ` and `DESSERT`, which are what is being served,
under that same `BUFFÉ`. The same word is a heading one day and content the next
(0046, 2026-08-29).

So the code keeps the one rule it can defend, and everything else is written down
here by hand. A tag is **an override on a default**, not a replacement for it: a
line nobody has looked at still gets `is_label`, which is what a restaurant added
next year needs on its first week.

**Keyed on the site and the exact line**, not on a `matsedel_dishes` row. A tag is
a fact about a phrase a kitchen reuses, so tagging `BUFFÉ` once holds for every
week, including weeks not yet read. It also survives `store.store_week`, which
deletes and rewrites a week on every re-read and would take row-keyed tags with
it. The site is `sources.config["site"]`, the same key the CSS modifier hangs on,
so renaming a restaurant in `/admin` cannot silently drop its tags.

**Applied on read, never at store time.** `matsedel_dishes` holds what the
restaurant wrote, including the lines tagged `HIDDEN`; the tag is presentation
policy, and policy that can change must not be baked into stored rows. Tagging a
line today therefore fixes every week already stored, with no re-scrape - which
is the whole reason `Meny för denna dag saknas.` can be dealt with at all, since
the week it appeared in has already replaced the real menu.

**Hardcoded rather than a table with an editing UI.** Four restaurants and one
reader: the tags are versioned, reviewed and branched with the code that reads
them, and adding one is an edit here rather than a migration, a write route and a
form. If a fifth correspondent ever needs the same thing, that is the moment to
ask whether this should be data.
"""

from __future__ import annotations

from .readers import is_label

#: Introduces the lines under it. Rendered as a heading on the week page and in
#: the Glance block, skipped by a card's summary, dropped from the most-served
#: table.
LABEL = "label"
#: Something a reader can order. The ordinary case, and what everything renders
#: unless told otherwise.
DISH = "dish"
#: Not part of the menu at all: a placeholder a site prints when it has nothing,
#: a footer line the price boundary did not catch. Stored, and rendered nowhere.
HIDDEN = "hidden"

#: `site` -> exact line -> tag. The line is matched after `str.strip()`, which is
#: what the reader already stored, so what goes in a key is the text as it appears
#: in `matsedel_dishes.text`.
#:
#: Nothing is tagged `DISH` yet, because the colon rule has no known false
#: positive. The tag exists so that the day it gets one, the fix is a line here
#: rather than an argument about the rule.
TAGS: dict[str, dict[str, str]] = {
    "vanerparken": {
        # Heads the numbered buffet slots every day, including the Friday where
        # the slots themselves are `GRILLBUFFÉ` and `DESSERT`.
        "BUFFÉ": LABEL,
        # Sentence case, no colon, and it still introduces the line under it.
        # This is the line `is_label` is on record as missing.
        "Dagens vegetariska": LABEL,
    },
    "italia": {
        # Wednesday's banner. It sits ABOVE `Från Buffé:`, which is the heading
        # that actually opens the list, so it names the day rather than a course.
        "ITALIENSK BUFFÉ": LABEL,
    },
    "kalasboden": {
        # What the site prints for a day it has not published. Read as a dish it
        # becomes the card's headline for that day.
        "Meny för denna dag saknas.": HIDDEN,
    },
}


def tag_of(site: str | None, text: str) -> str:
    """`LABEL`, `DISH` or `HIDDEN` for one line of one restaurant's menu.

    The one place that classifies a line, and every consumer goes through it: the
    week page and the Glance block render a `LABEL` as a heading and drop a
    `HIDDEN`, a card's summary quotes the first `DISH`, the most-served table
    counts only `DISH`. Four copies of the question would drift.
    """
    tagged = TAGS.get(site or "", {}).get(text.strip())
    if tagged is not None:
        return tagged
    return LABEL if is_label(text) else DISH
