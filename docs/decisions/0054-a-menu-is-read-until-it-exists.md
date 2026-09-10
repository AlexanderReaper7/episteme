# 0054: A menu is read until it exists

**2026-09-05.** Matsedel reads every weekday and skips a kitchen whose week is already whole, instead of reading all four once a week. `readers.read_source` takes the week it is reading FOR and refuses a page showing an earlier one.

## What happened

Week 36 was missing two of four restaurants for the whole week. The job that produced it reported success:

```
02:00 UTC 2026-08-31
  Read Koppargrillen: 2026w36, 5 day(s)
  Read Restaurang Italia: 2026w36, 5 day(s)
  Read Restaurang Vänerparken: 2026w35, 5 day(s)
  Stored Kalasboden 2026w36: 5 line(s) over 5 day(s)
  Filed 5 lunch post(s) for the week of 2026-08-24
  Filed 5 lunch post(s) for the week of 2026-08-31
  Matsedel: read 4 kitchen(s), 0 failed
```

`matsedel_cron` was `0 2 * * 1`, which is 04:00 on Monday in Vänersborg. No kitchen has published by then, and there was no second read until the following Monday.

Kalasboden served `Meny för denna dag saknas.` five times. `tags.tag_of` correctly calls those `HIDDEN`, so they render nowhere and reach no post, and `store_week`'s rule that a dayless day must not replace a day that had a menu had nothing to protect, because there was no week-36 row yet. The result is a stored week that is entirely invisible. `tests/test_matsedel.py:test_a_first_read_stores_exactly_what_it_read` pins that behaviour, and it is right: storing what the site actually served is not the bug.

Vänerparken is the more interesting half. It still had week 35 up. The reader parsed it correctly, dated it correctly, and returned it; the scrape stored it over the week-35 row it already had and re-filed week 35's five posts. Nothing failed, nothing was logged as odd, and week 36 simply never got a Vänerparken row.

## Two defects, only one of which is about the schedule

**The reader could not tell last week from this week.** `read_source` returned whatever week the page showed and the caller believed it. This is the failure `readers.py`'s own module docstring argues against for CSS selectors, one level up: there an empty menu is indistinguishable from a kitchen that posted nothing, here last week is indistinguishable from this week. Worse than silent, it rewrote and re-filed a week that was already correct.

`read_source` now takes a required `wanted_monday` and raises `NotPublishedYet` for a page showing an earlier week. Required rather than defaulted, because a caller that does not say cannot be told. An earlier week only: a kitchen that puts next week up on Friday is ahead of the calendar, not behind it, and storing that is the point.

**There was no second read.** One shot at 04:00 Monday, and every kitchen that publishes Monday morning, which is the normal thing to do, is lost for the whole week.

## Why daily beats a self-deferring retry

The alternative considered was keeping the weekly cron and having the job re-defer itself every few hours until the week is complete, giving up Wednesday evening. It needs an attempt bound and state to track it, and it converges on the same coverage as a daily cron.

Daily is fewer moving parts and, with the skip, fewer requests. `store.kitchens_with_a_full_week` drops any kitchen whose five weekdays each already carry a line `tag_of` does not call `HIDDEN`. An ordinary week is four reads on Monday and zero for the rest of it. Only a kitchen that has not published is fetched again, which is the only kitchen there is anything to learn about. Politeness is a hard requirement (0005), and this spends strictly less than the once-a-week schedule did in a week where anything went wrong, because that schedule spent a whole week of wrong menus.

What it costs: a restaurant that is closed on Fridays is never complete, so it is read all five mornings. That is four extra page loads a week and the price of not keeping a per-restaurant table of which days each one serves.

Completeness asks `tag_of`, not the row count. Kalasboden's week 36 was five stored rows over five days and nothing anyone could eat.

## The hour, and what it does not fix

`0 6 * * 1-5` UTC is 08:00 in Vänersborg. Monday's own post publishes at 06:00 local, so on a week where a kitchen has not published by Monday 04:00 its lines appear on the card two hours after the card does. That is the deliberate trade against reading before the kitchens are awake and being a full day late every time.

A kitchen that publishes after 08:00 on Monday is still not on Monday's card. It is picked up by Tuesday morning's read, which fixes the week page and the remaining four posts but not Monday's. Reading twice a day would close that, at the cost of one more read of only the late kitchens; that is the next thing to change if Monday keeps being wrong.

## What a failure means now

`NotPublishedYet` is not a failure. It is the expected state of a Monday morning, and counting it as one would put an alarm in the job history every week. It is logged and counted separately. The job raises only when something actually failed, nothing was read, and nothing was already complete — that is, when the week has nothing and a site is broken.

## Left alone

`matsedel_weeks.fetched_at` is written on insert and never updated on a re-read, and nothing reads it. With daily reads it will say "Monday 08:00" for a week last confirmed on Thursday. Changing it to mean "last read" is a one-line edit and a change to what a stored column means, so it waits for a reason to care.
