"""The canonical topic vocabulary and everything that maintains it.

Why this exists: topic tags are LLM free text, and free text drifts.
"astrophysics", "astronomy" and "space" arrive as three unrelated keys, so an
interest weight learned against them is a weight learned against noise. The
`topics` table is the closed set; this module maps whatever a model emits onto
it *in code* — the same "offer a closed set, enforce it outside the model"
pattern already used for media URLs and citations. A model that ignores the
offered vocabulary widens it deliberately (a new entry, recorded as such),
never by accident.

Resolution order per raw label, cheap -> expensive:

1. exact slug hit;
2. alias hit — a phrasing that resolved here before, so a repeat never costs an
   embed call;
3. nearest vocabulary embedding within `topic_match_threshold`, which records
   the raw slug as an alias;
4. otherwise a new vocabulary entry.

**`slug` is a topic's permanent identity.** It is assigned once, from the label
the entry was created with, and never moves again — `rename` changes only the
display label. Everything that keys anything to a topic keys it to the slug:
interest weights today (`InterestProfile.topic_weights`), and anything that
relates topics to each other later. A key derived from the current label would
silently re-key the topic — and dangle every weight and edge pointing at it —
the first time someone corrected a spelling. `slug_index` is how a stored label
gets back to that identity.

The lifecycle functions at the bottom (`propose_vocabulary` / `apply_proposal`)
are the two-phase bootstrap: the first pass only writes a proposal to
`app_state` for review, the second applies it and rewrites the existing rows.
Clustering an entire corpus of tags is exactly the kind of pass that can be
subtly wrong, so it is never applied by the job that computes it.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from datetime import UTC, datetime

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..llm import LLMError, gateway
from ..llm.observe import llm_context
from ..llm.prompts import TOPIC_NAMING_SYSTEM
from ..llm.schemas import TopicVocabulary
from ..models import AppState, Feedback, Post, Story, Topic

log = logging.getLogger("episteme.recommend.topics")

PROPOSAL_KEY = "topics_bootstrap"

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def slugify(label: str) -> str:
    """Stable key for a topic label: ascii, lowercase, hyphen-separated. Two
    phrasings that differ only in case, accent or punctuation share a slug, so
    the cheapest resolution tier already absorbs most drift."""
    normalized = unicodedata.normalize("NFKD", label)
    ascii_only = normalized.encode("ascii", "ignore").decode()
    return _SLUG_STRIP.sub("-", ascii_only.lower()).strip("-")[:80]


def clean_label(label: str) -> str:
    """Collapse whitespace and lowercase — the display form of a vocabulary entry."""
    return " ".join((label or "").split()).lower()


# --- Resolution -------------------------------------------------------------------


async def _by_slug(session: AsyncSession, slug: str) -> Topic | None:
    return (
        await session.execute(select(Topic).where(Topic.slug == slug))
    ).scalars().first()


async def _by_alias(session: AsyncSession, slug: str) -> Topic | None:
    return (
        await session.execute(select(Topic).where(Topic.aliases.contains([slug])))
    ).scalars().first()


async def _embed_labels(labels: list[str]) -> dict[str, list[float]]:
    """Embed topic labels, tolerating an unavailable embed model: a failure
    degrades resolution to slug/alias matching rather than failing the caller's
    stage. Entries created without an embedding are backfilled by
    `backfill_embeddings`."""
    if not labels:
        return {}
    vectors: dict[str, list[float]] = {}
    for start in range(0, len(labels), settings.embed_batch_size):
        batch = labels[start : start + settings.embed_batch_size]
        try:
            with llm_context(stage="topics"):
                embedded = await gateway.embed(batch)
        except LLMError as exc:
            log.warning("Topic embedding unavailable (%s); falling back to slug match", exc)
            return vectors
        vectors.update(zip(batch, embedded, strict=True))
    return vectors


async def _nearest(session: AsyncSession, vector: list[float]) -> tuple[Topic, float] | None:
    """Closest vocabulary entry by cosine similarity, or None on an empty table."""
    distance = Topic.embedding.cosine_distance(vector).label("distance")
    row = (
        await session.execute(
            select(Topic, distance)
            .where(Topic.embedding.is_not(None))
            .order_by(distance)
            .limit(1)
        )
    ).first()
    if row is None:
        return None
    return row.Topic, 1.0 - float(row.distance)


def _append(labels: list[str], label: str) -> None:
    if label not in labels:
        labels.append(label)


async def resolve_entries(
    session: AsyncSession, labels: list[str], *, allow_new: bool = True
) -> dict[str, Topic]:
    """Map each raw label onto its vocabulary row, keyed by the RAW label.

    This is the primitive; `resolve` and `resolve_slugs` are views of it. A
    caller that needs to know which topic a *particular* input became must use
    this, because the list views are not positionally aligned with their input:
    they deduplicate, and a label that resolves to a topic another label in the
    same batch already claimed simply doesn't appear again.

    *Origin: `record_nl` zipped `intent.topics` against the list, so "less
    crypto, more quantum computing" could be recorded as "less quantum
    computing, more crypto" — inverted, into the canonical `parsed_intent` that
    replay reads forever.*

    New entries are flushed as they are created so a second label in the same
    batch can match one the first just made. With `allow_new=False` an unmatched
    label is absent from the mapping rather than extending the vocabulary — used
    where the caller wants a strictly closed set.
    """
    entries: dict[str, Topic] = {}
    pending: dict[str, str] = {}  # slug -> cleaned label, deduped within the batch
    waiting: dict[str, list[str]] = {}  # slug -> the raw labels awaiting it
    for raw in labels:
        cleaned = clean_label(raw)
        slug = slugify(cleaned)
        if not slug:
            continue
        topic = await _by_slug(session, slug) or await _by_alias(session, slug)
        if topic is not None:
            entries[raw] = topic
        else:
            pending.setdefault(slug, cleaned)
            waiting.setdefault(slug, []).append(raw)
    if not pending:
        return entries

    vectors = await _embed_labels(list(pending.values()))
    for slug, cleaned in pending.items():
        topic = None
        vector = vectors.get(cleaned)
        if vector is not None:
            near = await _nearest(session, vector)
            if near is not None and near[1] >= settings.topic_match_threshold:
                topic, similarity = near
                log.info(
                    "Topic %r folded into %r (cos %.3f)", cleaned, topic.label, similarity
                )
                if slug not in (topic.aliases or []):
                    # Reassign rather than mutate in place: JSONB change tracking
                    # doesn't see list mutation.
                    topic.aliases = [*(topic.aliases or []), slug]
        if topic is None:
            if not allow_new:
                continue
            topic = Topic(slug=slug, label=cleaned, embedding=vector)
            session.add(topic)
            await session.flush()
        for raw in waiting[slug]:
            entries[raw] = topic
    return entries


async def resolve(
    session: AsyncSession, labels: list[str], *, allow_new: bool = True
) -> list[str]:
    """Canonical LABELS for a batch of raw model output — what gets stored on a
    story or post, deduplicated, in the order the raw labels arrived."""
    entries = await resolve_entries(session, labels, allow_new=allow_new)
    resolved: list[str] = []
    for raw in labels:
        topic = entries.get(raw)
        if topic is not None:
            _append(resolved, topic.label)
    return resolved


async def resolve_slugs(
    session: AsyncSession, labels: list[str], *, allow_new: bool = True
) -> list[str]:
    """Canonical SLUGS for a batch of raw labels — what gets stored on a feedback
    row, because a signal is about the topic's identity, not its current
    spelling."""
    entries = await resolve_entries(session, labels, allow_new=allow_new)
    resolved: list[str] = []
    for raw in labels:
        topic = entries.get(raw)
        if topic is not None:
            _append(resolved, topic.slug)
    return resolved


async def slug_index(session: AsyncSession) -> dict[str, str]:
    """Slugified label (and known alias) -> the topic's permanent slug.

    How a stored LABEL — which is what stories and posts hold — gets back to the
    identity its interest weight is keyed under. `slugify(label)` alone is only
    correct until someone renames a topic, at which point it points at a key
    nothing has ever learned anything about.

    Column select, not whole rows: the vocabulary is ~750 entries carrying a
    1024-dimension embedding each, and this needs three strings from each. One
    map per scoring pass, not one lookup per candidate.
    """
    rows = (
        await session.execute(select(Topic.slug, Topic.label, Topic.aliases))
    ).all()
    index: dict[str, str] = {}
    # Canonical forms first, then aliases, so a stale alias can never shadow a
    # live entry's own slug or label.
    for row in rows:
        index[row.slug] = row.slug
        index[slugify(row.label)] = row.slug
    for row in rows:
        for alias in row.aliases or []:
            index.setdefault(alias, row.slug)
    return index


async def slug_labels(session: AsyncSession) -> dict[str, str]:
    """The reverse of `slug_index`: permanent slug -> current spelling. For
    rendering a slug-keyed weight (the /tune sliders, the reader digest) in the
    words the vocabulary actually uses today."""
    rows = (await session.execute(select(Topic.slug, Topic.label))).all()
    return {row.slug: row.label for row in rows}


def slug_for(label: str, index: dict[str, str]) -> str:
    """The weight key for one stored label. A label the vocabulary doesn't claim
    yet falls back to its own slug — a story tagged since the last `resolve`
    still scores, exactly as everything did before the vocabulary existed."""
    slug = slugify(label)
    return index.get(slug, slug)


async def pending_embeddings(session: AsyncSession) -> int:
    """How many vocabulary entries are still missing an embedding."""
    return int(
        (
            await session.execute(
                select(func.count()).select_from(Topic).where(Topic.embedding.is_(None))
            )
        ).scalar()
        or 0
    )


async def backfill_embeddings(session: AsyncSession) -> int:
    """Embed vocabulary entries created while the embed model was unavailable.

    Without this they stay `embedding=NULL` forever, which is not merely
    untidy: `_nearest` skips them, so nothing can ever fold into them and every
    later phrasing of the same topic mints yet another entry. Called wherever the
    embed endpoint has just proved it is up — see `pipeline.embed_new_items` —
    and deferrable by hand as `backfill_topic_embeddings`.
    """
    topics = (
        await session.execute(select(Topic).where(Topic.embedding.is_(None)))
    ).scalars().all()
    if not topics:
        return 0
    vectors = await _embed_labels([topic.label for topic in topics])
    filled = 0
    for topic in topics:
        vector = vectors.get(topic.label)
        if vector is not None:
            topic.embedding = vector
            filled += 1
    await session.commit()
    if filled:
        log.info("Backfilled embeddings for %d vocabulary entries", filled)
    return filled


# --- Vocabulary reads -------------------------------------------------------------

# Counts every use of every topic label across both tables in one pass. The
# labels live in JSONB arrays, so they are unnested rather than joined; this is
# also what tells us which vocabulary entries are dead weight.
_USAGE_SQL = text(
    """
    SELECT label, count(*) AS uses FROM (
        SELECT jsonb_array_elements_text(topics) AS label FROM stories
        UNION ALL
        SELECT jsonb_array_elements_text(topics) AS label FROM posts
    ) AS used
    GROUP BY label
    """
)


async def usage_counts(session: AsyncSession) -> dict[str, int]:
    rows = (await session.execute(_USAGE_SQL)).all()
    return {row.label: int(row.uses) for row in rows}


async def vocabulary(session: AsyncSession) -> list[Topic]:
    return list((await session.execute(select(Topic).order_by(Topic.label))).scalars().all())


async def vocabulary_for_prompt(session: AsyncSession, limit: int | None = None) -> list[str]:
    """The vocabulary a stage shows the model, most-used first and capped — it is
    a prompt cost paid on every story. Anything the model invents outside the
    shown slice is still folded in by `resolve`, so the cap costs accuracy of
    suggestion, never correctness."""
    limit = settings.topic_vocabulary_prompt_limit if limit is None else limit
    topics = await vocabulary(session)
    counts = await usage_counts(session)
    topics.sort(key=lambda topic: (-counts.get(topic.label, 0), topic.label))
    return [topic.label for topic in topics[:limit]]


async def rename(session: AsyncSession, slug: str, new_label: str) -> int:
    """Rename a vocabulary entry and rewrite every row referencing it.

    Stories and posts store labels, not ids, so a rename is not a one-row edit.
    Doing it here — in one transaction — is what keeps that storage choice
    honest; nothing else may write topic labels directly.

    The `slug` deliberately does NOT move: it is the topic's identity, and the
    profile weight learned over months is filed under it. What the rename must
    do instead is make the new spelling *resolve back here* — `slugify(new_label)`
    is what the next model emission of it will look up, and without that alias
    `_by_slug` would miss and mint a second row for the topic that was just
    renamed.
    """
    topic = await _by_slug(session, slug)
    if topic is None:
        raise LookupError(f"No topic with slug {slug!r}")
    old_label = topic.label
    new_clean = clean_label(new_label)
    if not new_clean or new_clean == old_label:
        return 0
    topic.label = new_clean
    aliases = list(topic.aliases or [])
    # The new spelling, so it resolves here; the old one, so past phrasings still
    # do. (The old label's slug is usually the row's own slug, hence the skip.)
    for alias in (slugify(new_clean), slugify(old_label)):
        if alias and alias != topic.slug and alias not in aliases:
            aliases.append(alias)
    topic.aliases = aliases
    rewritten = await _remap_labels(session, {old_label: new_clean})
    await session.commit()
    return rewritten


async def merge(session: AsyncSession, slug: str, into_slug: str) -> int:
    """Fold one vocabulary entry into another, moving its aliases and rewriting
    referencing rows. The absorbed entry is deleted.

    Unlike a rename, this DOES change identity — one of the two slugs is about to
    stop existing — so the feedback log's references are repointed at the
    survivor. Otherwise the weight learned under the absorbed slug would be
    orphaned by the very operation whose premise is that the two were always the
    same topic. The log is rewritten rather than the derived profile, so the next
    replay folds both histories into the surviving key and stays authoritative.
    """
    source = await _by_slug(session, slug)
    target = await _by_slug(session, into_slug)
    if source is None or target is None:
        raise LookupError(f"Unknown topic slug ({slug!r} -> {into_slug!r})")
    if source.id == target.id:
        return 0
    aliases = list(target.aliases or [])
    for alias in [source.slug, slugify(source.label), *(source.aliases or [])]:
        if alias and alias != target.slug and alias not in aliases:
            aliases.append(alias)
    target.aliases = aliases
    rewritten = await _remap_labels(session, {source.label: target.label})
    await _remap_feedback_slugs(session, {source.slug: target.slug})
    await session.delete(source)
    await session.commit()
    return rewritten


async def _remap_feedback_slugs(session: AsyncSession, mapping: dict[str, str]) -> int:
    """Repoint the feedback log's topic references from one slug to another.

    Only a merge may call this. Signals name a topic in three places — `topic`,
    `topics_snapshot`, and the `topics` list inside `parsed_intent` — and all
    three are read through `slugify` by `profile.replay`, so matching on the
    slugified value catches rows written before slugs were stored as well.
    """
    if not mapping:
        return 0
    changed = 0
    events = (await session.execute(select(Feedback))).scalars().all()
    for event in events:
        touched = False
        if event.topic and slugify(event.topic) in mapping:
            event.topic = mapping[slugify(event.topic)]
            touched = True
        snapshot = [
            mapping.get(slugify(label), label) for label in event.topics_snapshot or []
        ]
        if snapshot != list(event.topics_snapshot or []):
            event.topics_snapshot = snapshot
            touched = True
        intent = event.parsed_intent
        if intent and intent.get("topics"):
            entries = [dict(entry) for entry in intent["topics"]]
            for entry in entries:
                target = mapping.get(slugify(entry.get("topic") or ""))
                if target:
                    entry["topic"] = target
            if entries != intent["topics"]:
                # Reassign the whole dict: JSONB change tracking doesn't see a
                # nested mutation.
                event.parsed_intent = {**intent, "topics": entries}
                touched = True
        changed += 1 if touched else 0
    return changed


def remap_labels(current: list[str] | None, mapping: dict[str, str]) -> list[str]:
    """One row's topic array rewritten through `mapping`: order preserved,
    duplicates a merge may have created collapsed. Labels not in the mapping are
    left untouched — a story ingested since the mapping was computed keeps its
    raw label and gets folded in by `resolve` later, rather than being dropped."""
    updated: list[str] = []
    for label in current or []:
        _append(updated, mapping.get(label, label))
    return updated


async def _remap_labels(session: AsyncSession, mapping: dict[str, str]) -> int:
    """Apply `remap_labels` to every story and post; returns rows changed."""
    if not mapping:
        return 0
    rewritten = 0
    for model in (Story, Post):
        rows = (await session.execute(select(model))).scalars().all()
        for row in rows:
            updated = remap_labels(row.topics, mapping)
            if updated != list(row.topics or []):
                row.topics = updated
                rewritten += 1
    return rewritten


# --- Two-phase bootstrap ----------------------------------------------------------


def _cluster(
    labels: list[str], vectors: dict[str, list[float]], threshold: float
) -> list[list[str]]:
    """Greedy single-pass clustering of topic labels by cosine similarity, most
    frequent first so the busiest phrasing anchors each cluster. Same shape as
    the story clusterer, in memory: the vocabulary is tens to hundreds of labels,
    not a corpus. Labels with no embedding become their own cluster — better a
    redundant entry the operator can merge than a wrong silent fold."""
    clusters: list[list[str]] = []
    centroids: list[list[float]] = []
    for label in labels:
        vector = vectors.get(label)
        if vector is None:
            clusters.append([label])
            centroids.append([])
            continue
        best_index, best_similarity = -1, 0.0
        for index, centroid in enumerate(centroids):
            if not centroid:
                continue
            similarity = sum(a * b for a, b in zip(centroid, vector, strict=True))
            if similarity > best_similarity:
                best_index, best_similarity = index, similarity
        if best_index >= 0 and best_similarity >= threshold:
            clusters[best_index].append(label)
            count = len(clusters[best_index])
            merged = [
                (c * (count - 1) + v) / count
                for c, v in zip(centroids[best_index], vector, strict=True)
            ]
            norm = sum(x * x for x in merged) ** 0.5 or 1.0
            centroids[best_index] = [x / norm for x in merged]
        else:
            clusters.append([label])
            centroids.append(list(vector))
    return clusters


NAMING_BATCH = 25


def _plausible_name(label: str, members: list[str]) -> bool:
    """Does this name belong to this cluster?

    The model answers by echoing an index, and a single wrong index silently
    renames a cluster to something unrelated. Requiring the name to share a word
    with one of its members is a cheap independent check on that index: a genuine
    canonical name almost always reuses the members' vocabulary ("conservation
    biology" over {conservation, wildlife conservation}, "artificial
    intelligence" over {ai, AI}), while a misaligned one shares nothing at all
    ("roman history" over {quantum physics, quantum mechanics}).

    It is deliberately permissive — it rejects misalignment, not infelicity."""
    words = {word for word in label.lower().replace("-", " ").split() if len(word) > 2}
    if not words:
        return False
    for member in members:
        member_words = {
            word for word in member.lower().replace("-", " ").split() if len(word) > 2
        }
        if words & member_words:
            return True
    return False


async def _name_clusters(clusters: list[list[str]]) -> dict[int, str]:
    """Ask the fast model for one canonical name per cluster that needs one.

    Two things keep this honest, both learned from a live run (2026-07-29) in
    which a single call over 734 clusters stayed aligned for 98 of them, slipped
    by one, and misnamed the remaining 633 — "quantum physics" came back as
    "roman history":

    1. Only MULTI-MEMBER clusters are sent. A singleton's canonical name is its
       one member, so naming it is pure risk with no upside — and it was the
       hundreds of singletons that made the listing long enough to lose the model.
    2. The rest go in small batches, and each returned name must be plausible for
       the cluster it claims (`_plausible_name`) before it is accepted.

    Anything not named, dropped, or rejected falls back to the cluster's most
    frequent member, so a bad response degrades the vocabulary's wording — never
    its structure."""
    targets = [
        (index, members) for index, members in enumerate(clusters) if len(members) > 1
    ]
    if not targets:
        return {}
    names: dict[int, str] = {}
    rejected = 0
    for start in range(0, len(targets), NAMING_BATCH):
        batch = targets[start : start + NAMING_BATCH]
        listing = "\n".join(
            f"{position}. {', '.join(members)}"
            for position, (_, members) in enumerate(batch)
        )
        try:
            with llm_context(stage="topics"):
                result = await gateway.complete_json(
                    "fast",
                    TOPIC_NAMING_SYSTEM,
                    f"Topic clusters:\n\n{listing}",
                    TopicVocabulary,
                )
        except LLMError as exc:
            log.warning("Topic naming failed (%s); using member names", exc)
            continue
        for entry in result.topics:
            if not 0 <= entry.index < len(batch):
                continue
            label = clean_label(entry.label)
            cluster_index, members = batch[entry.index]
            if not label:
                continue
            if not _plausible_name(label, members):
                rejected += 1
                continue
            names[cluster_index] = label
    if rejected:
        log.warning(
            "Rejected %d cluster name(s) unrelated to their members; using member names",
            rejected,
        )
    return names


async def propose_vocabulary(session: AsyncSession) -> dict:
    """Phase one: cluster every free-text topic already in the database into a
    proposed vocabulary and store it in `app_state` for review. Writes nothing
    else — no `topics` rows, no rewritten arrays."""
    counts = await usage_counts(session)
    labels = sorted(counts, key=lambda label: (-counts[label], label))
    vectors = await _embed_labels(labels)
    clusters = _cluster(labels, vectors, settings.topic_bootstrap_threshold)
    names = await _name_clusters(clusters)
    proposal = {
        "proposed_at": datetime.now(UTC).isoformat(),
        "raw_label_count": len(labels),
        "clusters": [
            {
                "label": names.get(index) or members[0],
                "members": members,
                "uses": sum(counts.get(member, 0) for member in members),
            }
            for index, members in enumerate(clusters)
        ],
    }
    proposal["clusters"].sort(key=lambda cluster: (cluster["uses"], cluster["label"]))
    await _store_proposal(session, proposal)
    log.info(
        "Proposed vocabulary: %d labels -> %d topics (review, then apply)",
        len(labels),
        len(clusters),
    )
    return proposal


async def _store_proposal(session: AsyncSession, proposal: dict | None) -> None:
    state = await session.get(AppState, PROPOSAL_KEY)
    if state is None:
        state = AppState(key=PROPOSAL_KEY)
        session.add(state)
    state.value = proposal or {}
    await session.commit()


async def discard_proposal(session: AsyncSession) -> None:
    """Throw away a proposal without applying it — the review said no."""
    await _store_proposal(session, None)


async def get_proposal(session: AsyncSession) -> dict | None:
    value = (
        await session.execute(select(AppState.value).where(AppState.key == PROPOSAL_KEY))
    ).scalar()
    return value if value and value.get("clusters") else None


async def apply_proposal(session: AsyncSession) -> dict:
    """Phase two: turn the reviewed proposal into vocabulary rows and rewrite the
    topic arrays on every story and post through it. Idempotent per label: an
    entry whose slug already exists absorbs the proposal's aliases instead of
    being duplicated."""
    proposal = await get_proposal(session)
    if proposal is None:
        raise LookupError("No topic vocabulary proposal to apply")

    canonical = [clean_label(cluster["label"]) for cluster in proposal["clusters"]]
    vectors = await _embed_labels(canonical)
    mapping: dict[str, str] = {}
    created = 0
    for cluster, label in zip(proposal["clusters"], canonical, strict=True):
        slug = slugify(label)
        if not slug:
            continue
        topic = await _by_slug(session, slug)
        if topic is None:
            topic = Topic(slug=slug, label=label, embedding=vectors.get(label))
            session.add(topic)
            created += 1
        aliases = list(topic.aliases or [])
        for member in cluster["members"]:
            member_slug = slugify(member)
            if member_slug and member_slug != slug and member_slug not in aliases:
                aliases.append(member_slug)
        topic.aliases = aliases
        for member in cluster["members"]:
            mapping[member] = label
    await session.flush()
    rewritten = await _remap_labels(session, mapping)
    await _store_proposal(
        session,
        {
            "applied_at": datetime.now(UTC).isoformat(),
            "topics_created": created,
            "rows_rewritten": rewritten,
        },
    )
    log.info("Applied vocabulary: %d topics created, %d rows rewritten", created, rewritten)
    return {"topics_created": created, "rows_rewritten": rewritten}
