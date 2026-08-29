"""Reading a week of lunch off a restaurant's own page.

Four restaurants, one reader. The alternative was four bespoke DOM parsers keyed
on each site's markup, which is more precise and fails the wrong way: a CSS
selector that stops matching after a redesign yields an EMPTY menu, and an empty
menu is indistinguishable from a kitchen that posted nothing. This reader works
on the page's visible lines instead, so a redesign has to remove the weekday
headings themselves before it stops working, and when it degrades it degrades
into a line of the site's footer rather than into silence.

**What a weekday heading is.** A line that is a weekday word and nothing else,
optionally followed by a date (`Mandag 24/8`). The strictness is the whole trick:
every one of the four pages carries lines that begin with a weekday and are not
headings - opening hours (`Mandag 11:00 - 22:00`), a serving window
(`Mandag-fredag`), a price (`FREDAGAR 150kr`), a Saturday lunch
(`Lordagslunch 12-14`) - and each of them would otherwise open a block and
swallow the page's footer into a menu.

**The last block is capped.** Every block but the last is bounded by the next
heading; the last one runs to the end of the document. It is truncated to the
median length of the earlier blocks, which is mechanical and stated once rather
than four site-specific stop patterns. Measured against the four pages as they
stood on 2026-08-28: exact on Kalasboden and Italia, unnecessary on Koppargrillen
(whose Saturday heading bounds Friday), and one footer line too generous on
Vänerparken. A visible extra line is the failure this is willing to have.

Politeness is not optional here (0005, 0006): every fetch goes through
`ingest/http.polite_get`, which is the reason a scraping correspondent has to be
an in-process plugin at all (0046).
"""

from __future__ import annotations

import logging
import re
import statistics
from dataclasses import dataclass, field
from datetime import date, timedelta

import lxml.html

from ...ingest.http import FetchError, polite_get
from ...models import Source

log = logging.getLogger("episteme.correspondents.matsedel")

#: Monday first, matching `date.weekday()`. Folded, so the matcher compares
#: against ASCII and the page's own diacritics and case do not matter.
WEEKDAYS = ("mandag", "tisdag", "onsdag", "torsdag", "fredag", "lordag", "sondag")

#: Only Monday to Friday become posts. A Saturday or Sunday heading still ends
#: the block above it, which is why the whole week is parsed and then narrowed.
SERVED_DAYS = 5

_FOLD = str.maketrans("åäöÅÄÖéèÉÈüÜ", "aaoAAOeeEEuU")

#: A heading: the weekday alone, or the weekday and a `24/8`-style date.
_HEADING = re.compile(r"^(?P<day>[a-z]+)(?:\s+(?P<d>\d{1,2})\s*/\s*(?P<m>\d{1,2}))?$")
#: `Vecka 35`, `VECKA 35`, `V.35`. Matched against the whole page's text, because
#: Vänerparken puts the word and the number in separate elements.
_WEEK_NO = re.compile(r"\bv(?:ecka|\.)\s*:?\s*(\d{1,2})\b")
#: Nothing but punctuation, digits and spaces: a rule of dashes, a bare `1`
#: numbering a buffet line, a lone full stop. No content to lose.
_EMPTY = re.compile(r"^[\W\d_]*$")
#: Leading bullets. Not digits: a dish may legitimately start with one.
_BULLET = re.compile(r"^[\s•·●▪∙*–—-]+")
#: A sum of money, written the way all four sites write one: `140kr`, `150 kr`,
#: `65kr st.`. Deliberately not a bare number - `Pizzor nummer 1 - 8` is a dish.
_PRICE = re.compile(r"\d+\s*kr\b", re.I)


def is_label(line: str) -> bool:
    """Does this line introduce the lines under it rather than name a dish?

    ONE signal: a trailing colon. It is the restaurants' own punctuation, and
    Italia's `Från Buffé:` and `Från köket:` are what it is for.

    It also used to treat a line with no lowercase letter as a heading, and that
    half is gone (2026-08-29). It caught four lines across the four pages and
    three of them were wrong. Vänerparken writes `BUFFÉ`, then `1`, then the
    dish, so on Monday `BUFFÉ` heads a list - but on Friday the numbered slots
    hold `GRILLBUFFÉ` and `DESSERT`, which are what is being served, in capitals,
    under that same `BUFFÉ`. The same word is a heading one day and content the
    next, and no rule reading the line ALONE can tell those two apart.

    What could is the `1`/`2`/`3` numbering, which says the line after it fills
    a slot. `_blocks` drops those as empty, and using them would mean deciding at
    parse time and storing the answer per line, because everything downstream
    classifies from `MatsedelDish.text` and nothing else. That is a column, so it
    is not a decision this function gets to make on its own.

    **`tags.tag_of` is the only caller**, and everything that renders or counts a
    line goes through that rather than through here. This is the default it falls
    back to for a line nobody has written a tag for.

    What it misses, measured against the four pages: `Dagens vegetariska`
    introduces the line under it and carries no colon, and `BUFFÉ` on Monday to
    Thursday genuinely does head its day. Both are tagged by hand, which is the
    answer to a line no rule can read - not a cleverer rule here.
    """
    return line.strip().endswith(":")


def is_terms(line: str) -> bool:
    """Is this line what lunch COSTS rather than what lunch is?

    `Dagens ratt mandag-torsdag 140kr inkl. smor & brod...` is the standing
    footer every one of these pages carries under its menu: opening prices,
    takeaway rates, coupon booklets. A menu line names food; a terms line names
    what it costs, and the price is the restaurants' own signal for which is
    which.

    Used as a BOUNDARY rather than a filter, in `_blocks`: the day's lines end at
    the first one of these. Dropping them individually is what does not work -
    the last block runs to the bottom of the page, `_cap_last` keeps a fixed
    number of lines, and removing one footer line just slides the next one up
    into the cap. Watched doing exactly that on 2026-08-29.

    Measured against the four pages as they stood: 1 line of 99 matches, and it
    is the one this exists for. What it would cost is a restaurant that prices
    each dish on its own line - the menu would end at the first dish. None of
    the four does, and the day that changes, this is the rule to argue with.
    """
    return bool(_PRICE.search(line))


class MatsedelError(Exception):
    """A page that could not be read as a week of lunch."""


@dataclass
class DayMenu:
    """One weekday's lines, in the order the restaurant wrote them."""

    served_on: date
    lines: list[str]


@dataclass
class WeekMenu:
    """One restaurant's week, as read from its page."""

    week_key: str
    monday: date
    url: str
    days: list[DayMenu] = field(default_factory=list)


def fold(text: str) -> str:
    return text.translate(_FOLD).lower()


def visible_lines(html: str) -> list[str]:
    """The page's text, one line per text node, empties dropped.

    `itertext` is deliberate: it puts the text before a `<br>` and the text after
    it on separate lines, which is how Kalasboden separates a dish from the
    week's vegan option, and how every one of the four separates dishes at all.
    """
    doc = lxml.html.fromstring(html)
    for bad in doc.xpath("//script|//style|//noscript"):
        bad.getparent().remove(bad)
    lines = []
    for chunk in doc.itertext():
        for piece in chunk.replace("\xa0", " ").split("\n"):
            piece = piece.strip()
            if piece:
                lines.append(piece)
    return lines


def heading_day(line: str) -> tuple[int, tuple[int, int] | None] | None:
    """`(weekday index, (day, month) or None)` if this line is a weekday heading."""
    if len(line) > 30:
        return None
    match = _HEADING.match(fold(line).replace(" ", " ").strip())
    if match is None:
        return None
    try:
        index = WEEKDAYS.index(match.group("day"))
    except ValueError:
        return None
    if match.group("d") is None:
        return index, None
    return index, (int(match.group("d")), int(match.group("m")))


def _blocks(lines: list[str]) -> list[tuple[int, tuple[int, int] | None, list[str]]]:
    """Split the page's lines into `(weekday, date or None, lines)` blocks."""
    found: list[tuple[int, tuple[int, int] | None, list[str]]] = []
    current: list[str] | None = None
    for line in lines:
        heading = heading_day(line)
        if heading is not None:
            index, when = heading
            current = []
            found.append((index, when, current))
        elif current is not None:
            cleaned = _BULLET.sub("", line).strip()
            if not cleaned or _EMPTY.match(cleaned):
                continue
            if is_terms(cleaned):
                # The menu is over: what follows is the page's standing footer.
                # `current = None` rather than `break`, so a later weekday
                # heading still opens a block - only THIS day stops here.
                current = None
                continue
            current.append(cleaned)
    return found


def _cap_last(found: list[tuple[int, tuple[int, int] | None, list[str]]]) -> None:
    """Truncate the last block to the median length of the earlier ones."""
    if len(found) < 2:
        return
    earlier = [len(lines) for _, _, lines in found[:-1] if lines]
    if not earlier:
        return
    cap = int(statistics.median(earlier))
    del found[-1][2][cap:]


def _year_for(month: int, day: int, today: date) -> int:
    """The year that puts `day/month` closest to today.

    A menu read in late December for the first week of January is the case this
    exists for, and it is the only one: a restaurant does not publish a menu six
    months out.
    """
    return min(
        (y for y in (today.year - 1, today.year, today.year + 1)),
        key=lambda y: abs((_safe_date(y, month, day) - today).days),
    )


def _safe_date(year: int, month: int, day: int) -> date:
    try:
        return date(year, month, day)
    except ValueError:
        # 29 February in a non-leap year. Nothing else can miss.
        return date(year, month, 28)


def _monday_from_week_number(number: int, today: date) -> date:
    candidates = [
        date.fromisocalendar(y, number, 1)
        for y in (today.year - 1, today.year, today.year + 1)
        if number <= date(y, 12, 28).isocalendar().week
    ]
    if not candidates:
        raise MatsedelError(f"week {number} is not a week of any nearby year")
    return min(candidates, key=lambda d: abs((d - today).days))


def parse_week(html: str, *, url: str, today: date) -> WeekMenu:
    """One restaurant's page, read as a week. Raises `MatsedelError` if it is not one.

    The week is dated from the headings when they carry dates, and from the page's
    week number otherwise. Dates win because they say which week without anyone
    having to agree on what week numbering means; the number is what three of the
    four sites publish instead.
    """
    lines = visible_lines(html)
    found = _blocks(lines)
    if not found:
        raise MatsedelError("no weekday headings on the page")
    _cap_last(found)

    dated = next(((index, when) for index, when, _ in found if when is not None), None)
    if dated is not None:
        index, (day, month) = dated
        anchor = _safe_date(_year_for(month, day, today), month, day)
        monday = anchor - timedelta(days=index)
    else:
        match = _WEEK_NO.search(fold(" ".join(lines)))
        if match is None:
            raise MatsedelError(
                "no week number and no dated heading, so the menu could belong to "
                "any week"
            )
        monday = _monday_from_week_number(int(match.group(1)), today)

    iso = monday.isocalendar()
    week = WeekMenu(week_key=f"{iso.year}w{iso.week}", monday=monday, url=url)
    seen: set[int] = set()
    for index, _, day_lines in found:
        if index >= SERVED_DAYS or index in seen or not day_lines:
            continue
        seen.add(index)
        week.days.append(
            DayMenu(served_on=monday + timedelta(days=index), lines=list(day_lines))
        )
    if not week.days:
        raise MatsedelError("weekday headings, but no lines under any of them")
    return week


async def read_source(source: Source, *, today: date) -> WeekMenu:
    """Fetch and parse one restaurant. Its URL is `source.config["url"]`."""
    url = (source.config or {}).get("url")
    if not url:
        raise MatsedelError(f"source {source.id} ({source.name}) has no url in config")
    try:
        response = await polite_get(
            url,
            mode=(source.config or {}).get("http_mode") or "polite",
            host_gap_seconds=float((source.config or {}).get("min_request_gap_seconds") or 0),
        )
        response.raise_for_status()
    except FetchError as exc:
        raise MatsedelError(f"{source.name}: HTTP {exc.status_code}") from exc
    except Exception as exc:  # httpx/curl transport failures
        raise MatsedelError(f"{source.name}: {type(exc).__name__}: {exc}") from exc
    week = parse_week(response.text or "", url=url, today=today)
    log.info(
        "Read %s: %s, %d day(s)", source.name, week.week_key, len(week.days)
    )
    return week
