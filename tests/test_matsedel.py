"""Matsedel's reader, its summaries and the five posts it mints (0046).

The fixtures under `tests/fixtures/matsedel/` are the four real pages as they
stood on 2026-08-28, reduced to the lines `visible_lines` extracts from them, one
per file line. That reduction is deliberate: the parser works on visible lines and
nothing else, so this is exactly what it sees, it is readable in a diff, and a
page that changes can be re-captured without committing four scraped documents.
`visible_lines` itself is pinned separately against real markup below.

What these files are evidence FOR is the two rules in `readers.py`'s docstring,
which are the two that were tuned against real pages and would otherwise be
someone's memory: what counts as a weekday heading, and the median cap on the
last block. Every one of the four pages carries at least one line that begins
with a weekday and is not a heading.

Nothing here touches a database (see conftest). `store_week` and the queries
behind `/c/matsedel` were watched live instead; 0046 records what was watched.
"""

import asyncio
import html as H
import pathlib
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
import sqlalchemy as sa

from episteme.correspondents.matsedel import posts as mposts
from episteme.correspondents.matsedel import readers
from episteme.correspondents.matsedel import views as mviews
from episteme.correspondents.matsedel import sources as msources
from episteme.correspondents.matsedel import tags as mtags
from episteme.correspondents.matsedel.readers import (
    MatsedelError,
    heading_day,
    is_terms,
    parse_week,
    visible_lines,
)
from episteme.correspondents.matsedel.tags import tag_of
from episteme.correspondents.matsedel.models import MatsedelWeek
from episteme.correspondents.matsedel.store import store_week

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "matsedel"

#: The day the four fixtures were read. Every expectation below is relative to
#: it, because `parse_week` dates a week from where "today" is.
READ_ON = date(2026, 8, 28)
THAT_MONDAY = date(2026, 8, 24)

SITES = ("koppargrillen", "italia", "vanerparken", "kalasboden")


def _page(name: str) -> str:
    """A fixture's lines back as a document. One line per element, so `itertext`
    yields them again in order: what the file holds is what the parser saw."""
    lines = [
        line
        for line in (FIXTURES / f"{name}.txt").read_text(encoding="utf-8").splitlines()
        if not line.startswith("# ")
    ]
    body = "".join(f"<p>{H.escape(line)}</p>" for line in lines)
    return f"<html><body>{body}</body></html>"


# --- the heading rule ------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        # Koppargrillen: serving hours, a Saturday price, a Sunday one.
        "Lunch Måndag-Fredag 11.30-16.00",
        "Lördagslunch 12-14",
        "Söndagsmiddag 12-16",
        # Vänerparken: the serving window and the Friday surcharge.
        "Måndag-fredag",
        "FREDAGAR 150kr",
        # Kalasboden: two sets of opening hours, one per shop.
        "Måndag - Fredag: 09:00 - 18:00",
        "Lördag: Avhämtning10:00 vid förbest",
        "Måndag - Fredag: 11:00 - 15:00",
        # And the sentence that introduces the whole menu.
        "Vi serverar dagens lunch alla vardagar mellan kl 11 - 14.",
    ],
)
def test_a_line_that_merely_mentions_a_weekday_is_not_a_heading(line):
    """Every one of these is on one of the four pages today. Each would open a
    block and swallow whatever follows it into a menu, which is why a heading is
    the weekday word ALONE."""
    assert heading_day(line) is None


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("Måndag", (0, None)),
        ("MÅNDAG", (0, None)),
        ("måndag", (0, None)),
        ("Fredag", (4, None)),
        ("Måndag 24/8", (0, (24, 8))),
        ("Fredag 28/8", (4, (28, 8))),
        ("Onsdag 3 / 12", (2, (3, 12))),
        ("Söndag", (6, None)),
    ],
)
def test_a_heading_is_the_weekday_alone_or_with_its_date(line, expected):
    assert heading_day(line) == expected


# --- the median cap --------------------------------------------------------


def test_the_last_block_is_capped_at_the_median_of_the_earlier_ones():
    """Nothing bounds the last weekday's block, so it runs to the bottom of the
    page. Kalasboden is the page this rule exists for: its Friday reads 32 lines
    of site footer without the cap, and the four earlier days are 1, 2, 2 and 2,
    so Friday keeps 2.

    The cap is a length, not an understanding of where the menu ends, so it is
    the fallback. `is_terms` is what actually finds the end when the page says
    so; this catches the pages that never mention a price."""
    week = parse_week(_page("kalasboden"), url="u", today=READ_ON)
    friday = next(d for d in week.days if d.served_on == date(2026, 8, 28))
    assert len(friday.lines) == 2
    assert not any("cookie" in line.lower() for line in friday.lines)


def test_the_menu_ends_where_the_page_starts_quoting_prices():
    """Vänerparken's FREDAG block ran on into the standing footer, and the median
    cap kept the first line of it: `Dagens ratt mandag-torsdag 140kr inkl. smor &
    brod...`, which is what lunch costs and not something anyone can order.

    Dropping such lines one at a time is what does NOT work, and this is why the
    rule is a boundary: the cap keeps a fixed number of lines, so removing one
    footer line slides the next one up into it. Ending the day at the first one
    removes the footer entirely."""
    week = parse_week(_page("vanerparken"), url="u", today=READ_ON)
    friday = next(d for d in week.days if d.served_on == date(2026, 8, 28))
    assert friday.lines == ["BUFFÉ", "GRILLBUFFÉ", "DESSERT", "Dagens vegetariska"]
    assert not any(is_terms(line) for line in friday.lines)


def test_a_boundary_ends_one_day_and_not_the_week():
    """`_blocks` stops collecting at a terms line rather than breaking out, so a
    weekday heading further down the page still opens its own block. A page that
    prices Monday's lunch must not lose Tuesday."""
    lines = [
        "MÅNDAG",
        "Pannbiff med pepparsås",
        "Dagens rätt 140kr inkl. bröd",
        "Kokt fisk med äggsås",
        "TISDAG",
        "Raggmunk med stekt fläsk",
        "ONSDAG",
        "Ost & champinjongratinerad kassler",
        "TORSDAG",
        "Fisksoppa med pannkakor",
        "FREDAG",
        "Vegetarisk biff",
    ]
    body = "".join(f"<p>{H.escape(line)}</p>" for line in lines)
    week = parse_week(f"<html><body><p>Vecka 35</p>{body}</body></html>", url="u", today=READ_ON)
    by_day = {d.served_on.weekday(): d.lines for d in week.days}
    # Monday ends at the price, losing the line under it. That is the cost of a
    # boundary, and it is the trade: a price means the menu is over.
    assert by_day[0] == ["Pannbiff med pepparsås"]
    assert by_day[1] == ["Raggmunk med stekt fläsk"]
    assert by_day[4] == ["Vegetarisk biff"]


def test_a_page_whose_last_day_is_bounded_needs_no_cap():
    """Koppargrillen writes a Lordag heading, so Friday ends where Saturday
    starts and the cap never binds. Saturday and Sunday are parsed and then
    dropped, because only Monday to Friday become posts."""
    week = parse_week(_page("koppargrillen"), url="u", today=READ_ON)
    assert [d.served_on for d in week.days] == [THAT_MONDAY + timedelta(days=n) for n in range(5)]
    assert week.days[-1].lines == [
        "Kokt fiskfilé med äggsås",
        "Fläskytterfilé med bearnaisesås",
        "Omelett",
        "Vegetarisk biff",
    ]


# --- the four pages as a whole ---------------------------------------------


@pytest.mark.parametrize("site", SITES)
def test_every_page_reads_as_the_week_it_was_read_in(site):
    """Two sites publish `Vecka 35`, one `V.35`, and Kalasboden dates its
    headings instead. All four have to arrive at the same Monday."""
    week = parse_week(_page(site), url="https://x.test/menu", today=READ_ON)
    assert week.week_key == "2026w35"
    assert week.monday == THAT_MONDAY
    assert [d.served_on for d in week.days] == [THAT_MONDAY + timedelta(days=n) for n in range(5)]
    assert all(d.lines for d in week.days)


def test_a_dated_heading_carries_a_week_with_no_number_on_it():
    """Kalasboden publishes no week number at all, so its dates are the only
    thing saying which week this is."""
    week = parse_week(_page("kalasboden"), url="u", today=READ_ON)
    assert week.monday == THAT_MONDAY
    assert week.days[0].lines == ["Pasta med rökt skinka & blue cheese"]


def test_a_page_with_no_headings_is_an_error_not_an_empty_week():
    """The failure the line reader exists to avoid is silence. A page that
    stopped carrying menus has to say so."""
    with pytest.raises(MatsedelError, match="no weekday headings"):
        parse_week(
            "<html><body><p>Stangt for semester</p></body></html>",
            url="u",
            today=READ_ON,
        )


def test_headings_with_no_date_and_no_week_number_are_refused():
    """A menu that could belong to any week is not a menu."""
    doc = "<html><body><p>Mandag</p><p>Pannbiff med peppar</p></body></html>"
    with pytest.raises(MatsedelError, match="could belong to any week"):
        parse_week(doc, url="u", today=READ_ON)


def test_a_week_number_near_new_year_lands_on_the_nearest_year():
    """Read in late December, `Vecka 1` is next year's. The reason the week
    number is resolved against today rather than against the calendar year."""
    doc = "<html><body><p>Vecka 1</p><p>Mandag</p><p>Pannbiff med peppar</p></body></html>"
    week = parse_week(doc, url="u", today=date(2025, 12, 29))
    assert week.monday == date(2025, 12, 29)  # ISO 2026w1 starts here
    assert week.week_key == "2026w1"


# --- what the parser is handed ---------------------------------------------


def test_a_line_break_separates_two_dishes():
    """`itertext` is the reason `visible_lines` splits at `<br>`, which is how
    Kalasboden separates the day's dish from the week's vegan option and how all
    four separate dishes at all."""
    doc = "<html><body><p>Jambalaya med limeyoghurt<br>VECKANS VEGAN: Gryta</p></body></html>"
    assert visible_lines(doc) == ["Jambalaya med limeyoghurt", "VECKANS VEGAN: Gryta"]


def test_script_and_style_text_is_not_a_dish():
    doc = (
        "<html><head><style>p{color:red}</style></head>"
        "<body><script>var x=1</script><p>Omelett</p></body></html>"
    )
    assert visible_lines(doc) == ["Omelett"]


@pytest.mark.parametrize("line", ["1", "---", ".", "2."])
def test_lines_with_no_content_are_dropped(line):
    """Vänerparken numbers its buffet lines `1`, `2`, `3` on their own. A number
    is not a dish, and a card that lists one is worse than one that does not."""
    doc = (
        f"<html><body><p>Mandag 24/8</p><p>{H.escape(line)}</p>"
        "<p>Pannbiff med peppar</p></body></html>"
    )
    week = parse_week(doc, url="u", today=READ_ON)
    assert week.days[0].lines == ["Pannbiff med peppar"]


def test_a_leading_bullet_is_not_part_of_the_dish():
    """Koppargrillen writes `- Pannbiff` and `-Omelett` in the same block."""
    doc = (
        "<html><body><p>Mandag 24/8</p>"
        "<p>&#8226; Pannbiff med pepparsas</p><p>&#8226;Omelett</p></body></html>"
    )
    week = parse_week(doc, url="u", today=READ_ON)
    assert week.days[0].lines == ["Pannbiff med pepparsas", "Omelett"]


# --- the summary line ------------------------------------------------------


def test_the_summary_skips_the_labels_that_introduce_dishes():
    """Italia's block opens with `Fran Buffe:` and Vänerparken's with `BUFFE`.
    Neither names a dish, and a feed card built from them would say the same
    thing every day of the year."""
    assert (
        mposts.summary_line(
            _Source("italia", "Restaurang Italia"),
            ["Från Buffé:", "Färsbiff med klyftpotatis", "Pizza slice"],
        )
        == "Restaurang Italia: Färsbiff med klyftpotatis"
    )
    assert (
        mposts.summary_line(
            _Source("vanerparken", "Restaurang Vänerparken"),
            ["Dagens rätt:", "Dansk hackebiff med löksky"],
        )
        == "Restaurang Vänerparken: Dansk hackebiff med löksky"
    )


def test_the_summary_skips_a_label_only_the_tag_table_knows_about():
    """`BUFFÉ` carries no colon, so the rule reads it as a dish and the card
    quoted it for all five days. The tag is what puts the real dish back, and
    only for the restaurant it was written for."""
    lines = ["BUFFÉ", "Dansk hackebiff med löksky"]
    assert mposts.summary_line(_Source("vanerparken", "V"), lines) == (
        "V: Dansk hackebiff med löksky"
    )
    # The same word at a kitchen nobody tagged is still a dish.
    assert mposts.summary_line(_Source("koppargrillen", "K"), lines) == "K: BUFFÉ"


def test_a_menu_of_nothing_but_labels_still_names_its_restaurant():
    """The rule is a preference, not a filter: a card that omits a kitchen that
    published something is worse than one that quotes its label."""
    kalasboden = _Source("kalasboden", "Kalasboden")
    assert mposts.summary_line(kalasboden, ["Från köket:"]) == "Kalasboden: Från köket:"
    assert mposts.summary_line(kalasboden, []) == "Kalasboden"


# --- the five posts --------------------------------------------------------


class _Source:
    def __init__(self, site, name):
        self.id = hash(site) % 1000
        self.name = name
        self.config = {"site": site, "url": f"https://{site}.test/lunch"}


class _Dish:
    def __init__(self, serve_date, text):
        self.serve_date = serve_date
        self.text = text


class _Week:
    def __init__(self, source, dishes):
        self.source = source
        self.dishes = dishes


@pytest.fixture
def filed(monkeypatch):
    """`file_week` with its three database calls replaced. What is under test is
    the loop: which days become posts, what each one is dated, and what its items
    are keyed on. Filing itself has its own tests and its own live verification."""
    calls = []

    async def _file_post(session, **kw):
        calls.append(kw)
        return object()

    monkeypatch.setattr(mposts, "file_post", _file_post)

    async def _config(_session):
        return {}

    monkeypatch.setattr(mposts, "_config", _config)

    def _with(weeks, config=None):
        async def _week_of(_session, _monday):
            return weeks

        monkeypatch.setattr(mposts, "week_of", _week_of)
        if config is not None:

            async def _c(_session):
                return config

            monkeypatch.setattr(mposts, "_config", _c)
        return calls

    return _with


def _full_week(source):
    return _Week(
        source,
        [_Dish(THAT_MONDAY + timedelta(days=n), f"dish {n}") for n in range(5)],
    )


async def test_one_read_mints_one_post_per_weekday(filed):
    """The whole reason the week is stored before it is filed: five posts come
    out of one fetch, so a site being down on Wednesday costs nothing."""
    calls = filed([_full_week(_Source("koppargrillen", "Koppargrillen"))])
    await mposts.file_week(None, THAT_MONDAY)
    assert len(calls) == 5
    assert [c["title"] for c in calls] == [
        "Lunch on Monday 24 August",
        "Lunch on Tuesday 25 August",
        "Lunch on Wednesday 26 August",
        "Lunch on Thursday 27 August",
        "Lunch on Friday 28 August",
    ]


async def test_a_day_no_kitchen_cooks_gets_no_post(filed):
    """A card that says nothing is worse than no card. Wednesday is missing here
    because no restaurant published a Wednesday, not because filing failed."""
    week = _full_week(_Source("italia", "Restaurang Italia"))
    week.dishes = [d for d in week.dishes if d.serve_date != date(2026, 8, 26)]
    calls = filed([week])
    await mposts.file_week(None, THAT_MONDAY)
    assert len(calls) == 4
    assert date(2026, 8, 26) not in [c["publish_at"].date() for c in calls]


async def test_a_kitchen_whose_whole_day_is_hidden_has_published_nothing(filed):
    """The placeholder is not a menu, so the kitchen contributes no item and no
    summary rather than a card that says `Kalasboden: Meny för denna dag
    saknas.`. A day where every kitchen is hidden gets no post at all, which is
    the same path as a day nobody cooked."""
    quiet = _Week(
        _Source("kalasboden", "Kalasboden"),
        [_Dish(THAT_MONDAY, "Meny för denna dag saknas.")],
    )
    loud = _Week(
        _Source("koppargrillen", "Koppargrillen"),
        [_Dish(THAT_MONDAY, "Pannbiff med pepparsås")],
    )
    calls = filed([quiet, loud])
    await mposts.file_week(None, THAT_MONDAY)
    assert len(calls) == 1
    assert calls[0]["summary"] == "Koppargrillen: Pannbiff med pepparsås"
    assert [i.title for i in calls[0]["items"]] == [f"Koppargrillen, {THAT_MONDAY}"]

    calls.clear()
    filed([quiet])
    await mposts.file_week(None, THAT_MONDAY)
    assert calls == []


async def test_each_post_publishes_in_the_morning_and_expires_after_lunch(filed):
    """Local hours, not UTC: the four restaurants are in one town and the reader
    is in it. `expires_at` is what takes yesterday's lunch out of the feed (0047),
    and the week stays on `/c/matsedel` either way."""
    calls = filed([_full_week(_Source("kalasboden", "Kalasboden"))])
    await mposts.file_week(None, THAT_MONDAY)
    zone = ZoneInfo("Europe/Stockholm")
    monday = calls[0]
    assert monday["publish_at"] == datetime(2026, 8, 24, 6, 0, tzinfo=zone)
    assert monday["expires_at"] == datetime(2026, 8, 24, 14, 0, tzinfo=zone)
    assert all(c["expires_at"] > c["publish_at"] for c in calls)


async def test_the_serving_hours_come_off_the_correspondent_row(filed):
    """A kitchen that serves later is a configuration change, not an edit."""
    calls = filed(
        [_full_week(_Source("kalasboden", "Kalasboden"))],
        config={"publish_hour": 4, "expire_hour": 16},
    )
    await mposts.file_week(None, THAT_MONDAY)
    assert calls[0]["publish_at"].hour == 4
    assert calls[0]["expires_at"].hour == 16


async def test_a_days_post_carries_every_kitchen_that_cooked_that_day(filed):
    """One post per weekday, not one per restaurant: four cards every morning
    would make Matsedel a source of volume."""
    calls = filed(
        [
            _full_week(_Source("koppargrillen", "Koppargrillen")),
            _full_week(_Source("italia", "Restaurang Italia")),
        ]
    )
    await mposts.file_week(None, THAT_MONDAY)
    monday = calls[0]
    assert [i.key for i in monday["items"]] == [
        "Matsedel/koppargrillen/2026-08-24",
        "Matsedel/italia/2026-08-24",
    ]
    assert monday["summary"] == "Koppargrillen: dish 0 · Restaurang Italia: dish 0"


async def test_a_filed_post_points_at_the_day_it_is_about(filed):
    """0047: a filed post has an href and no body, and Matsedel's href is the
    day's own anchor on the week's page rather than the page's top."""
    calls = filed([_full_week(_Source("italia", "Restaurang Italia"))])
    await mposts.file_week(None, THAT_MONDAY)
    assert [c["href"] for c in calls] == [
        f"/c/matsedel#day-2026-08-{day}" for day in (24, 25, 26, 27, 28)
    ]


#: A CSS identifier may not start with a digit, nor with a hyphen followed by
#: one. An `id` may, in HTML, which is the whole trap.
CSS_IDENT = re.compile(r"^-?[A-Za-z_][A-Za-z0-9_-]*$")


async def test_the_anchor_a_card_points_at_is_a_valid_css_selector(filed):
    """`id="2026-08-24"` is legal HTML and `#2026-08-24` is not a legal selector,
    so htmx threw `DOMException: is not a valid selector` scrolling to it after a
    boosted navigation. Watched in Firefox on 2026-08-28. The `day-` prefix is
    the fix, and this is what would notice it being dropped as redundant."""
    calls = filed([_full_week(_Source("italia", "Restaurang Italia"))])
    await mposts.file_week(None, THAT_MONDAY)
    for call in calls:
        fragment = call["href"].partition("#")[2]
        assert CSS_IDENT.match(fragment), fragment


def test_the_week_page_and_the_card_agree_on_the_anchor():
    """Two templates and one Python string spell this id, and a card pointing at
    an anchor the page does not carry scrolls nowhere in silence."""
    root = pathlib.Path(mposts.__file__).parent / "templates"
    week = (root / "matsedel_week.html").read_text(encoding="utf-8")
    glance = (root / "matsedel_glance.html").read_text(encoding="utf-8")
    assert 'id="day-{{ day.date }}"' in week
    assert 'href="/c/matsedel#day-{{ day.date }}"' in glance


# --- the clause that keeps ingest out --------------------------------------


def test_ingest_skips_a_source_whose_type_is_a_correspondent():
    """`ingest_all` excludes any source whose `type_name` is a correspondent
    slug, which is what stops the generic ingester from fetching these four pages
    a second time every half hour. Compiled rather than read: the clause has to
    be a subquery against `correspondents`, and a registry lookup baking today's
    slugs into the SQL would pass a source-text check and fail this one."""
    from episteme.worker.tasks import not_a_correspondents_source

    sql = " ".join(str(not_a_correspondents_source()).split())
    assert sql == ("(sources.type_name NOT IN (SELECT correspondents.slug FROM correspondents))")


def test_the_adapters_type_name_is_the_plugins_slug():
    """The clause above compares `sources.type_name` against `correspondents.slug`,
    so Matsedel's two names being equal is what actually excludes its four sources.
    Nothing else would notice them drifting apart."""
    from episteme.correspondents.matsedel import Matsedel

    assert msources.TYPE_NAME == Matsedel.slug == mposts.SLUG


# --- what a label is -------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        "Från Buffé:",  # Italia, before the buffet's dishes
        "Från köket:",  # Italia, before the kitchen's
        "VECKANS LUNCH:",  # Italia, above the whole week
    ],
)
def test_a_line_the_restaurant_wrote_as_a_heading_is_a_label(line):
    """Every one of these is on one of the four pages today, and every one
    introduces the lines under it. One signal: the restaurant's own colon."""
    assert readers.is_label(line)


@pytest.mark.parametrize(
    "line",
    ["BUFFÉ", "GRILLBUFFÉ", "DESSERT", "ITALIENSK BUFFÉ"],
)
def test_capitals_alone_do_not_make_a_heading(line):
    """The four lines the old no-lowercase half caught, and three of them were
    content. Vänerparken writes `BUFFÉ`, `1`, then the dish - so on Monday
    `BUFFÉ` heads a list, and on Friday the numbered slots hold `GRILLBUFFÉ`
    and `DESSERT`, which are what is served, under that same `BUFFÉ`.

    The same word is a heading one day and content the next, so a rule reading
    the line alone cannot tell them apart and must not pretend to. Rendering
    them as dishes is the text the restaurant wrote either way."""
    assert not readers.is_label(line)


@pytest.mark.parametrize(
    "line",
    [
        "Pannbiff med pepparsås",
        "Omelett",  # one word, and still a dish
        "Pizzor nummer 1 - 8",
        "Piccata Milanese med tomatconcassé",
        "VECKANS VEGAN: Sensommargryta med blomkål & rotsaker",  # labelled dish
    ],
)
def test_a_dish_is_not_a_label_however_short(line):
    """`Omelett` is the case the old word-count rule got wrong: it is a whole
    dish, and it was being skipped as though it introduced something."""
    assert not readers.is_label(line)


def test_the_rule_the_label_rule_is_known_to_miss():
    """Vänerparken writes `Dagens vegetariska` above the vegetarian option, in
    sentence case and with no colon, so nothing mechanical separates it from a
    dish. Pinned so the gap stays visible in the rule itself: what closes it is a
    tag written by hand (`tags.TAGS`), not a cleverer reading of the line."""
    assert not readers.is_label("Dagens vegetariska")
    assert tag_of("vanerparken", "Dagens vegetariska") == mtags.LABEL


# --- the tag table ---------------------------------------------------------


def test_a_tag_beats_the_rule_and_the_rule_still_decides_the_rest():
    """A tag is an override on a default, not a replacement for it. A restaurant
    added next year is untagged, and its first week still has to render."""
    assert tag_of("vanerparken", "BUFFÉ") == mtags.LABEL  # tagged
    assert tag_of("vanerparken", "Från köket:") == mtags.LABEL  # the rule
    assert tag_of("vanerparken", "Pannbiff") == mtags.DISH  # the rule
    assert tag_of("nykyrkan", "BUFFÉ") == mtags.DISH  # untagged site
    assert tag_of(None, "Från köket:") == mtags.LABEL  # no site at all


def test_a_tag_belongs_to_one_kitchen():
    """`BUFFÉ` heads Vänerparken's numbered slots. The same word somewhere else
    is a word, and a tag that leaked across restaurants would be a rule again,
    with the same problem the rule had."""
    assert tag_of("vanerparken", "BUFFÉ") == mtags.LABEL
    for other in ("koppargrillen", "italia", "kalasboden"):
        assert tag_of(other, "BUFFÉ") == mtags.DISH


def test_a_tag_matches_the_text_as_it_was_stored():
    """`readers` strips a line before storing it, so a key is the stored text.
    Matching is exact otherwise: a tag is a phrase a kitchen reuses verbatim, and
    a fuzzier match would silently swallow a dish that starts the same way."""
    assert tag_of("vanerparken", "  BUFFÉ  ") == mtags.LABEL
    assert tag_of("vanerparken", "BUFFÉ med sallad") == mtags.DISH
    assert tag_of("vanerparken", "buffé") == mtags.DISH


@pytest.mark.parametrize(
    ("site", "line"),
    [
        (site, line)
        for site, lines in mtags.TAGS.items()
        for line, tag in lines.items()
        if tag != mtags.HIDDEN
    ],
)
def test_every_visible_tag_names_a_line_its_site_actually_writes(site, line):
    """A tag is written by hand against a page, so a typo in one does nothing at
    all and would never be noticed. Each one has to match a line the captured
    page really contains."""
    assert line in visible_lines(_page(site))


@pytest.mark.parametrize(
    ("site", "line"),
    [
        (site, line)
        for site, lines in mtags.TAGS.items()
        for line, tag in lines.items()
        if tag == mtags.HIDDEN
    ],
)
def test_a_hidden_tag_cannot_name_a_line_a_good_week_wrote(site, line):
    """HIDDEN removes a line from every page and every card, so it is the one tag
    that can destroy content. The four fixtures are weeks where all four kitchens
    published, so a HIDDEN tag matching one of their lines is a tag eating a real
    dish. If a fixture is ever recaptured from a page that carries a placeholder,
    this fires - and the answer is to recapture that fixture, not to drop the tag.
    """
    assert line not in visible_lines(_page(site))


def test_the_summary_and_the_page_agree_on_every_line_of_every_page():
    """One rule, four readers: the week page and the Glance block render a label
    as a heading, the summary skips past it, the most-served table drops it. This
    is the check that they are the same rule - each consumer calls `tag_of`, so
    what a page reads as a heading is exactly what a card refuses to quote."""
    for site in SITES:
        source = _Source(site, "K")
        week = parse_week(_page(site), url="u", today=READ_ON)
        for day in week.days:
            dishes = [ln for ln in day.lines if tag_of(site, ln) == mtags.DISH]
            summary = mposts.summary_line(source, day.lines)
            if dishes:
                assert summary == f"K: {dishes[0]}"


# --- storing a week over a week that is already there -----------------------


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return list(self._rows)


class _StoreSession:
    """Enough of an `AsyncSession` for `store_week`, and no database.

    It answers the week lookup, answers the dish read-back with what is already
    stored, and collects what gets added. The read-back returns nothing once the
    delete has run, so a `store_week` that deleted before reading would fail
    these tests rather than quietly keep nothing.
    """

    def __init__(self, week=None, dishes=()):
        self.week = week
        #: `(serve_date, text)` in position order, the way the query returns them.
        self.dishes = list(dishes)
        self.added = []
        self.deleted = False

    async def execute(self, stmt):
        if isinstance(stmt, sa.Delete):
            self.deleted = True
            return _Result([])
        if stmt.column_descriptions[0]["entity"] is MatsedelWeek:
            return _Result([self.week] if self.week is not None else [])
        return _Result([] if self.deleted else self.dishes)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        pass

    def written(self):
        return [(d.serve_date, d.text) for d in self.added if hasattr(d, "text")]


class _StoredWeek:
    def __init__(self, monday):
        self.id = 1
        self.monday = monday


PLACEHOLDER = "Meny för denna dag saknas."
TUESDAY = THAT_MONDAY + timedelta(days=1)


def _menu(**days):
    """A `WeekMenu` from `mon=[...]`, `tue=[...]` keyword days."""
    order = ("mon", "tue", "wed", "thu", "fri")
    return readers.WeekMenu(
        week_key="2026w35",
        monday=THAT_MONDAY,
        url="u",
        days=[
            readers.DayMenu(served_on=THAT_MONDAY + timedelta(days=order.index(k)), lines=v)
            for k, v in days.items()
        ],
    )


async def test_a_day_with_no_menu_does_not_replace_a_day_that_had_one():
    """Kalasboden took week 35 down mid-week and served the placeholder for every
    day. The re-read overwrote the real menu and the five filed posts degraded
    with it. A menu that was read was true when it was read, and a site saying
    nothing today is not the site correcting itself."""
    session = _StoreSession(
        _StoredWeek(THAT_MONDAY),
        [(THAT_MONDAY, "Pasta med rökt skinka & blue cheese")],
    )
    await store_week(session, _Source("kalasboden", "Kalasboden"), _menu(mon=[PLACEHOLDER]))
    assert session.written() == [(THAT_MONDAY, "Pasta med rökt skinka & blue cheese")]


async def test_a_real_menu_still_replaces_a_real_menu():
    """The guard is about a page that says nothing, not about a page that changed
    its mind. A corrected menu is the site being right and has to win."""
    session = _StoreSession(_StoredWeek(THAT_MONDAY), [(THAT_MONDAY, "Pannbiff")])
    await store_week(session, _Source("kalasboden", "Kalasboden"), _menu(mon=["Kålpudding"]))
    assert session.written() == [(THAT_MONDAY, "Kålpudding")]


async def test_a_day_the_read_dropped_entirely_is_kept():
    """Per DAY, not per week: a site that has published Monday and not yet
    Tuesday stores Monday and keeps whatever Tuesday it already had."""
    session = _StoreSession(
        _StoredWeek(THAT_MONDAY),
        [(THAT_MONDAY, "old monday"), (TUESDAY, "real tuesday")],
    )
    await store_week(session, _Source("kalasboden", "Kalasboden"), _menu(mon=["new monday"]))
    assert session.written() == [
        (THAT_MONDAY, "new monday"),
        (TUESDAY, "real tuesday"),
    ]


async def test_a_kept_day_is_renumbered_into_the_same_sequence():
    """Positions are unique per week and are what replays a week in order, so a
    kept day is rewritten from what was just read back rather than left behind by
    the delete."""
    session = _StoreSession(
        _StoredWeek(THAT_MONDAY),
        [(THAT_MONDAY, "a"), (THAT_MONDAY, "b"), (TUESDAY, "c")],
    )
    await store_week(
        session,
        _Source("kalasboden", "Kalasboden"),
        _menu(mon=[PLACEHOLDER], tue=["d", "e"]),
    )
    assert [(d.serve_date, d.position, d.text) for d in session.added] == [
        (THAT_MONDAY, 0, "a"),
        (THAT_MONDAY, 1, "b"),
        (TUESDAY, 2, "d"),
        (TUESDAY, 3, "e"),
    ]


async def test_a_first_read_stores_exactly_what_it_read():
    """Nothing to protect on a week nobody has stored, placeholder or not: a
    kitchen that has never published gets the page it actually served."""
    session = _StoreSession()
    await store_week(session, _Source("kalasboden", "Kalasboden"), _menu(mon=[PLACEHOLDER]))
    assert session.written() == [(THAT_MONDAY, PLACEHOLDER)]


# --- the roster the sticky header names ------------------------------------


class _WeekRow:
    def __init__(self, source, dishes):
        self.source = source
        self.dishes = dishes


@pytest.fixture
def week_context(monkeypatch):
    """`_week_context` over a fabricated set of stored weeks."""

    def build(weeks):
        async def _week_of(_session, _monday):
            return weeks

        monkeypatch.setattr(mviews, "week_of", _week_of)
        return asyncio.run(mviews._week_context(None, THAT_MONDAY))

    return build


def test_every_day_keeps_a_column_for_every_kitchen(week_context):
    """The page names each restaurant ONCE, in a header that sticks under the
    site header, and the header and each day's row are the same CSS grid. That
    only lines up if a day carries a cell per kitchen in the roster's order even
    when the kitchen published nothing - an absent cell shifts every column to
    its right out from under its name."""
    quiet = _Source("italia", "Restaurang Italia")
    context = week_context(
        [
            _WeekRow(
                _Source("koppargrillen", "Koppargrillen"),
                [_Dish(THAT_MONDAY + timedelta(days=n), f"dish {n}") for n in range(5)],
            ),
            # Publishes on Monday and nothing else.
            _WeekRow(quiet, [_Dish(THAT_MONDAY, "lasagne al forno")]),
        ]
    )
    assert [k["name"] for k in context["kitchens"]] == [
        "Koppargrillen",
        "Restaurang Italia",
    ]
    for day in context["days"]:
        assert len(day["cells"]) == len(context["kitchens"])
        assert [c["kitchen"]["name"] for c in day["cells"]] == [
            k["name"] for k in context["kitchens"]
        ]
    assert context["days"][0]["cells"][1]["lines"] == [{"text": "lasagne al forno", "label": False}]
    # Tuesday: the column is still there, and it is empty.
    assert context["days"][1]["cells"][1]["lines"] == []


def test_a_hidden_line_never_reaches_the_page(week_context):
    """Kalasboden prints `Meny för denna dag saknas.` for a day it has not
    published. It is stored, because the table holds what the restaurant wrote,
    and it is rendered nowhere - so a tag added today fixes every week already
    stored, with no re-scrape."""
    context = week_context(
        [
            _WeekRow(
                _Source("kalasboden", "Kalasboden"),
                [
                    _Dish(THAT_MONDAY, "Meny för denna dag saknas."),
                    _Dish(THAT_MONDAY + timedelta(days=1), "Jambalaya med limeyoghurt"),
                ],
            )
        ]
    )
    assert context["days"][0]["cells"][0]["lines"] == []
    assert context["days"][1]["cells"][0]["lines"] == [
        {"text": "Jambalaya med limeyoghurt", "label": False}
    ]


def test_a_kitchen_carries_the_css_hook_its_typeface_hangs_on(week_context):
    """Each restaurant's name is set in the face its own site uses, and the
    modifier is the `sources` config key rather than a slugified label, so
    renaming a restaurant in /admin cannot silently drop its typography."""
    context = week_context([_WeekRow(_Source("vanerparken", "Restaurang Vänerparken"), [])])
    assert context["kitchens"][0]["site"] == "vanerparken"


def test_the_lines_arrive_already_classified(week_context):
    """The template branches on a flag rather than calling a filter, so there is
    one place that decides and the page cannot disagree with the summary."""
    context = week_context(
        [
            _WeekRow(
                _Source("italia", "Restaurang Italia"),
                [
                    _Dish(THAT_MONDAY, "Från Buffé:"),
                    _Dish(THAT_MONDAY, "Lasagne Al Forno"),
                ],
            )
        ]
    )
    assert context["days"][0]["cells"][0]["lines"] == [
        {"text": "Från Buffé:", "label": True},
        {"text": "Lasagne Al Forno", "label": False},
    ]


# --- the week arrows -------------------------------------------------------


class _NoSession:
    """`week_view` opens a session and hands it to functions this file stubs, so
    the session itself is never touched. It only has to be an async context
    manager, because that is the whole of how `week_view` uses it."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


@pytest.fixture
def week_view(monkeypatch):
    """`week_view` with storage replaced: `stored` is the set of weeks that exist.

    What is under test is the arrow decision, so the fabrication is a set of
    mondays rather than rows. Everything else `week_view` calls - the roster, the
    fallback to the latest week, the render - is stubbed to the identity so the
    context this returns is the one the template would have been handed.
    """

    def build(stored, *, monday=THAT_MONDAY):
        async def _week_of(_session, _monday):
            return []

        async def _stored_weeks(_session, mondays):
            return {m for m in mondays if m in stored}

        async def _latest_monday(_session, **_kw):
            return None

        monkeypatch.setattr(mviews, "SessionLocal", _NoSession)
        monkeypatch.setattr(mviews, "week_of", _week_of)
        monkeypatch.setattr(mviews, "stored_weeks", _stored_weeks)
        monkeypatch.setattr(mviews, "latest_monday", _latest_monday)
        monkeypatch.setattr(mviews, "correspondent_page", lambda _r, _p, _t, context: context)
        return asyncio.run(mviews.week_view(None, monday=monday.isoformat()))

    return build


def test_no_arrow_to_a_week_nothing_was_read_for(week_view):
    """An arrow to an empty week lands on a page that says nothing was read for
    it, which is a dead end the reader has to back out of. On a first run the
    only stored week is the one being looked at, so both arrows are gone."""
    context = week_view(stored={THAT_MONDAY})
    assert context["previous"] is None
    assert context["next"] is None


def test_an_arrow_appears_for_each_week_that_exists(week_view):
    """Each side is decided on its own: a history that starts here has a next
    arrow and no previous one."""
    before, after = THAT_MONDAY - timedelta(days=7), THAT_MONDAY + timedelta(days=7)

    assert week_view(stored={THAT_MONDAY, after})["previous"] is None
    assert week_view(stored={THAT_MONDAY, after})["next"] == after
    assert week_view(stored={THAT_MONDAY, before})["previous"] == before
    assert week_view(stored={THAT_MONDAY, before})["next"] is None


def test_a_gap_stops_the_arrows_rather_than_jumping_it(week_view):
    """`stored_weeks` is asked about EXACTLY the adjacent weeks, so a week the
    correspondent missed stops the walk. Jumping to the nearest stored week would
    keep the arrow alive at the cost of what it means: it would no longer be the
    week next to this one."""
    two_back = THAT_MONDAY - timedelta(days=14)
    assert week_view(stored={THAT_MONDAY, two_back})["previous"] is None


# --- the nav as it renders -------------------------------------------------


@pytest.fixture(scope="module")
def week_template():
    """The real template, loaded through the real loader.

    `add_template_dir` is what `web/correspondents.py` calls when it mounts a
    plugin, so this reaches the same template the app serves rather than a copy
    with its own path.
    """
    from episteme.correspondents.matsedel import Matsedel
    from episteme.web.templating import add_template_dir, templates

    add_template_dir(Matsedel.templates)
    return templates.env.get_template("matsedel_week.html")


def _nav(template, *, previous=None, next=None):
    return template.render(
        kitchens=[],
        kitchens_read=0,
        days=[],
        monday=THAT_MONDAY,
        previous=previous,
        next=next,
        today=READ_ON,
    )


def test_an_arrow_is_the_glyph_alone(week_template):
    """Icon-only, so the words are gone and the control carries the accessible
    name instead - the rule `_icons.html` states for a glyph with no text."""
    out = _nav(week_template, previous=THAT_MONDAY - timedelta(days=7))
    assert "previous week" not in out
    assert "next week" not in out
    assert 'aria-label="Week of 2026-08-17"' in out
    assert 'title="Week of 2026-08-17"' in out


@pytest.mark.parametrize(
    "previous,following,expected",
    [
        (None, None, 0),
        (THAT_MONDAY - timedelta(days=7), None, 1),
        (None, THAT_MONDAY + timedelta(days=7), 1),
        (THAT_MONDAY - timedelta(days=7), THAT_MONDAY + timedelta(days=7), 2),
    ],
)
def test_the_template_draws_one_arrow_per_week_that_exists(
    week_template, previous, following, expected
):
    """A None from `week_view` is an arrow that is not there at all, rather than
    one that is disabled: there is nothing for a disabled arrow to tell the
    reader that its absence does not."""
    out = _nav(week_template, previous=previous, next=following)
    assert out.count('class="matsedel-step"') == expected


# --- the Glance block ------------------------------------------------------


@pytest.fixture(scope="module")
def glance_template():
    from episteme.correspondents.matsedel import Matsedel
    from episteme.web.templating import add_template_dir, templates

    add_template_dir(Matsedel.templates)
    return templates.env.get_template("matsedel_glance.html")


def _glance(template, lines):
    return template.render(
        monday=THAT_MONDAY,
        day={
            "name": "Friday",
            "date": THAT_MONDAY + timedelta(days=4),
            "kitchens": [
                {
                    "kitchen": {"name": "Restaurang Italia", "site": "italia"},
                    "lines": [
                        {"text": t, "label": tag_of("italia", t) == mtags.LABEL}
                        for t in lines
                        if tag_of("italia", t) != mtags.HIDDEN
                    ],
                }
            ],
        },
    )


def test_a_glance_dish_gets_a_line_of_its_own(glance_template):
    """Joined with a separator the dishes ran together into a paragraph, and the
    paragraph had to be truncated - so the kitchen with the most to offer was the
    one whose menu got cut. A list has no length to trim."""
    dishes = ["Lasagne Al Forno", "Pizza slice", "Fish and chips med remouladsås"]
    out = _glance(glance_template, dishes)
    items = re.findall(r'<li class="matsedel-glance-dish">(.*?)</li>', out, re.S)
    assert [i.strip() for i in items] == [H.escape(d) for d in dishes]
    assert " · " not in out


def test_a_glance_label_is_a_heading_over_the_dishes_it_introduces(glance_template):
    """`Fran Buffe:` and `Fran koket:` are the only thing on the page saying
    which dishes are the buffet and which come from the kitchen. Dropped, the
    block is a flat list of eight that quietly asserts they are all the same."""
    out = _glance(
        glance_template,
        ["FRÅN BUFFÉ:", "Lasagne Al Forno", "Från köket:", "Pizza slice"],
    )
    labels = re.findall(r'<li class="matsedel-glance-label">(.*?)</li>', out, re.S)
    dishes = re.findall(r'<li class="matsedel-glance-dish">(.*?)</li>', out, re.S)
    assert [x.strip() for x in labels] == ["FRÅN BUFFÉ:", "Från köket:"]
    assert [x.strip() for x in dishes] == ["Lasagne Al Forno", "Pizza slice"]


def test_no_dish_is_dropped_for_being_far_down_the_menu(glance_template):
    """The truncation this replaced cut at 140 characters, which is four or five
    dishes in Swedish. Nothing here counts."""
    dishes = [f"Rätt nummer {n} med potatis och sås" for n in range(1, 13)]
    out = _glance(glance_template, dishes)
    assert out.count('class="matsedel-glance-dish"') == 12
    assert "…" not in out


# --- reading for a WEEK, not just for a week (0054) -------------------------


class _Response:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


def _served(monkeypatch, page_name):
    """Make every kitchen serve one fixture page, whatever URL is asked for."""
    body = _page(page_name)

    async def fake_get(url, **kwargs):
        return _Response(body)

    monkeypatch.setattr(readers, "polite_get", fake_get)


async def test_a_page_still_showing_last_week_is_not_this_week(monkeypatch):
    """The 2026-08-31 failure. Vanerparken's page parsed perfectly and dated
    itself week 35, so the Monday read stored it over the week-35 row it already
    had, re-filed week 35's posts, and reported success. Week 36 had no
    Vanerparken column until someone looked."""
    _served(monkeypatch, "vanerparken")
    with pytest.raises(readers.NotPublishedYet):
        await readers.read_source(
            _Source("vanerparken", "Restaurang Vänerparken"),
            today=READ_ON + timedelta(days=7),
            wanted_monday=THAT_MONDAY + timedelta(days=7),
        )


async def test_the_week_asked_for_is_read(monkeypatch):
    """The ordinary case, and the one the check must not break."""
    _served(monkeypatch, "vanerparken")
    week = await readers.read_source(
        _Source("vanerparken", "Restaurang Vänerparken"),
        today=READ_ON,
        wanted_monday=THAT_MONDAY,
    )
    assert week.monday == THAT_MONDAY


async def test_a_kitchen_that_is_ahead_is_read_not_refused(monkeypatch):
    """A restaurant that puts next week up on Friday is publishing, not failing.
    Only an EARLIER week means "has not published yet"."""
    _served(monkeypatch, "vanerparken")
    week = await readers.read_source(
        _Source("vanerparken", "Restaurang Vänerparken"),
        today=READ_ON,
        wanted_monday=THAT_MONDAY - timedelta(days=7),
    )
    assert week.monday == THAT_MONDAY


# --- which kitchen the next read may skip (0054) ----------------------------


def _stored(site, days):
    """A `_Week` whose `dishes` are `{offset: [line, ...]}` from `THAT_MONDAY`."""
    return _Week(
        _Source(site, site),
        [
            _Dish(THAT_MONDAY + timedelta(days=offset), line)
            for offset, lines in days.items()
            for line in lines
        ],
    )


def _complete(week):
    from episteme.correspondents.matsedel.store import week_is_complete

    return week_is_complete(THAT_MONDAY, (week.source.config or {}).get("site"), week.dishes)


def test_a_kitchen_with_all_five_weekdays_is_skipped():
    assert _complete(_stored("kalasboden", {n: [f"dish {n}"] for n in range(5)}))


def test_a_kitchen_missing_friday_is_read_again():
    """Published Monday to Thursday and adding Friday on Wednesday is the case
    a once-a-week read cannot see at all."""
    assert not _complete(_stored("kalasboden", {n: [f"dish {n}"] for n in range(4)}))


def test_a_week_of_placeholders_is_not_a_week():
    """Kalasboden's week 36: five stored rows over five days, every one of them
    `Meny for denna dag saknas.`, and nothing a reader can eat. Counting rows
    rather than asking `tag_of` is how that week stayed empty."""
    assert not _complete(_stored("kalasboden", {n: [PLACEHOLDER] for n in range(5)}))


def test_a_day_of_nothing_but_labels_still_counts():
    """A label is something the restaurant wrote for that day, so the day was
    published. What it is worth is the card's problem, not the scheduler's."""
    assert _complete(_stored("italia", {n: ["Från Buffé:"] for n in range(5)}))
