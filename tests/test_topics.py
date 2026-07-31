"""Canonical topic vocabulary: slug normalization, the resolution tiers that
enforce the closed set in code, bootstrap clustering, and label remapping.

Resolution's database lookups are monkeypatched module functions, so the tiers
(exact slug -> alias -> embedding fold -> new entry) are exercised without a
database, the same way the agent tests stub the gateway.
"""

import pytest

from episteme.config import settings
from episteme.llm import LLMError
from episteme.models import Feedback, Story, Topic
from episteme.recommend import topics


class _FakeSession:
    """Only what `resolve` touches when the lookups are stubbed."""

    def __init__(self):
        self.added: list[Topic] = []

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        return None


def _stub_lookups(monkeypatch, *, by_slug=None, by_alias=None, nearest=None, vectors=None):
    async def fake_by_slug(session, slug):
        return (by_slug or {}).get(slug)

    async def fake_by_alias(session, slug):
        return (by_alias or {}).get(slug)

    async def fake_nearest(session, vector):
        return nearest

    async def fake_embed(labels):
        return {label: [1.0, 0.0] for label in labels} if vectors is None else vectors

    monkeypatch.setattr(topics, "_by_slug", fake_by_slug)
    monkeypatch.setattr(topics, "_by_alias", fake_by_alias)
    monkeypatch.setattr(topics, "_nearest", fake_nearest)
    monkeypatch.setattr(topics, "_embed_labels", fake_embed)


# --- Normalization ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Machine Learning", "machine-learning"),
        ("  deep-sea   biology ", "deep-sea-biology"),
        ("Astronomí­a", "astronomia"),  # accents folded, not dropped as a whole
        ("C++ / systems", "c-systems"),
        ("!!!", ""),
    ],
)
def test_slugify_normalizes_drift(raw, expected):
    assert topics.slugify(raw) == expected


def test_clean_label_lowercases_and_collapses_whitespace():
    assert topics.clean_label("  Marine   Biology\n") == "marine biology"


# --- Resolution tiers -------------------------------------------------------------


async def test_exact_slug_hit_never_embeds(monkeypatch):
    """The cheapest tier must short-circuit: a known phrasing costs no embed call."""
    existing = Topic(slug="astronomy", label="astronomy", aliases=[])
    calls: list[list[str]] = []

    async def fake_embed(labels):
        calls.append(labels)
        return {}

    _stub_lookups(monkeypatch, by_slug={"astronomy": existing})
    monkeypatch.setattr(topics, "_embed_labels", fake_embed)

    # Different spelling, same slug — case and spacing drift is absorbed for free.
    result = await topics.resolve(_FakeSession(), ["  Astronomy "])
    assert result == ["astronomy"]
    assert calls == []


async def test_alias_hit_returns_the_canonical_spelling(monkeypatch):
    existing = Topic(slug="astronomy", label="astronomy", aliases=["astrophysics"])
    _stub_lookups(monkeypatch, by_alias={"astrophysics": existing})
    assert await topics.resolve(_FakeSession(), ["astrophysics"]) == ["astronomy"]


async def test_near_match_folds_and_records_the_alias(monkeypatch):
    """A new phrasing close enough to an existing entry joins it — and is recorded,
    so the next occurrence resolves at the alias tier instead of re-embedding."""
    existing = Topic(slug="astronomy", label="astronomy", aliases=[])
    _stub_lookups(
        monkeypatch, nearest=(existing, settings.topic_match_threshold + 0.01)
    )
    session = _FakeSession()
    assert await topics.resolve(session, ["stellar astronomy"]) == ["astronomy"]
    assert existing.aliases == ["stellar-astronomy"]
    assert session.added == []  # nothing new was created


async def test_distant_label_creates_a_new_entry(monkeypatch):
    existing = Topic(slug="astronomy", label="astronomy", aliases=[])
    _stub_lookups(
        monkeypatch, nearest=(existing, settings.topic_match_threshold - 0.01)
    )
    session = _FakeSession()
    assert await topics.resolve(session, ["Marine Biology"]) == ["marine biology"]
    assert [(t.slug, t.label) for t in session.added] == [
        ("marine-biology", "marine biology")
    ]


async def test_closed_set_mode_drops_unknown_labels(monkeypatch):
    """allow_new=False is the strictly-closed variant: an unmatched label is
    dropped rather than silently widening the vocabulary."""
    existing = Topic(slug="astronomy", label="astronomy", aliases=[])
    _stub_lookups(monkeypatch, nearest=(existing, 0.1))
    session = _FakeSession()
    assert await topics.resolve(session, ["marine biology"], allow_new=False) == []
    assert session.added == []


async def test_embedding_outage_degrades_to_slug_matching(monkeypatch):
    """The embed model being down must not fail the caller's stage: the entry is
    created without a vector (backfilled later), not skipped or raised on."""
    _stub_lookups(monkeypatch, vectors={})
    session = _FakeSession()
    assert await topics.resolve(session, ["quantum computing"]) == ["quantum computing"]
    assert session.added[0].embedding is None


async def test_duplicate_labels_resolve_once(monkeypatch):
    """Two spellings of the same unknown topic in one batch must not race to
    create two rows with the same slug (a unique-constraint abort)."""
    _stub_lookups(monkeypatch, nearest=None)
    session = _FakeSession()
    result = await topics.resolve(session, ["Marine Biology", "marine  biology"])
    assert result == ["marine biology"]
    assert len(session.added) == 1


async def test_empty_and_punctuation_only_labels_are_ignored(monkeypatch):
    _stub_lookups(monkeypatch, nearest=None)
    session = _FakeSession()
    assert await topics.resolve(session, ["", "   ", "!!!"]) == []
    assert session.added == []


# --- Per-input resolution ---------------------------------------------------------


async def test_resolve_entries_answers_per_input_not_by_position(monkeypatch):
    """Regression (code review, 2026-07-30): `record_nl` zipped `intent.topics`
    against `resolve`'s list. That list is deduplicated and drops anything
    unresolvable, so it is NOT positionally aligned with its input — pairing them
    by position hands one statement's direction to another statement's topic.
    "less crypto, more quantum computing" could be recorded as its own inverse,
    into the canonical `parsed_intent` that replay reads for as long as the
    statement stands.

    Here the first label is dropped (punctuation) and the last two collapse to one
    entry, so every positional assumption that could hold is broken at once."""
    known = Topic(slug="astronomy", label="astronomy", aliases=[])
    _stub_lookups(monkeypatch, by_slug={"astronomy": known}, nearest=None)
    session = _FakeSession()

    entries = await topics.resolve_entries(
        session, ["!!!", "astronomy", "Marine Biology", "marine  biology"]
    )
    assert "!!!" not in entries
    assert entries["astronomy"].slug == "astronomy"
    # Both spellings land on the SAME row, each under its own raw key.
    assert entries["Marine Biology"] is entries["marine  biology"]
    assert entries["Marine Biology"].slug == "marine-biology"


async def test_resolve_slugs_returns_identities_not_spellings(monkeypatch):
    """Signals are filed under the slug: a feedback row must not be able to lose
    its weight because someone corrected a topic's wording."""
    renamed = Topic(slug="blue", label="the colour blue", aliases=["the-colour-blue"])
    _stub_lookups(monkeypatch, by_alias={"the-colour-blue": renamed})
    session = _FakeSession()
    assert await topics.resolve_slugs(session, ["The Colour Blue"]) == ["blue"]
    assert await topics.resolve(session, ["The Colour Blue"]) == ["the colour blue"]


# --- Identity across a rename -----------------------------------------------------


def test_slug_index_maps_a_renamed_topics_label_back_to_its_identity():
    """The reason `slugify(label)` is not a weight key: after a rename it points
    at a slug nothing was ever learned against."""
    index = {"blue": "blue", "the-colour-blue": "blue"}
    assert topics.slug_for("the colour blue", index) == "blue"
    # A label the vocabulary doesn't claim yet still scores, under its own slug.
    assert topics.slug_for("brand new topic", index) == "brand-new-topic"


# --- Bootstrap clustering ---------------------------------------------------------


def test_cluster_groups_by_similarity():
    vectors = {
        "astronomy": [1.0, 0.0],
        "astrophysics": [0.99, 0.14],  # cos ~0.99 with astronomy
        "marine biology": [0.0, 1.0],
    }
    clusters = topics._cluster(list(vectors), vectors, threshold=0.9)
    assert sorted(sorted(c) for c in clusters) == [
        ["astronomy", "astrophysics"],
        ["marine biology"],
    ]


def test_cluster_isolates_unembedded_labels():
    """A label with no vector becomes its own cluster: a redundant entry the
    operator can merge beats a wrong silent fold."""
    vectors = {"astronomy": [1.0, 0.0]}
    clusters = topics._cluster(["astronomy", "unembeddable"], vectors, threshold=0.5)
    assert ["unembeddable"] in clusters


async def test_cluster_naming_falls_back_to_most_frequent_member(monkeypatch):
    """A failed or partial naming call may degrade the vocabulary's wording, never
    its structure — every cluster still gets exactly one name."""

    class _Gateway:
        async def complete_json(self, *args, **kwargs):
            raise LLMError("model down")

    monkeypatch.setattr(topics, "gateway", _Gateway())
    assert await topics._name_clusters([["astronomy", "astrophysics"]]) == {}


async def test_cluster_naming_ignores_out_of_range_indices(monkeypatch):
    from episteme.llm.schemas import TopicVocabulary

    class _Gateway:
        async def complete_json(self, *args, **kwargs):
            return TopicVocabulary.model_validate(
                {"topics": [{"index": 0, "label": "Astronomy"}, {"index": 9, "label": "ghost"}]}
            )

    monkeypatch.setattr(topics, "gateway", _Gateway())
    clusters = [["astronomy", "astrophysics"]]
    assert await topics._name_clusters(clusters) == {0: "astronomy"}


async def test_singleton_clusters_are_never_sent_for_naming(monkeypatch):
    """A one-member cluster's canonical name IS that member, so naming it is pure
    risk. It is also what made the live listing long enough (734 entries) for the
    model to lose index alignment — see the regression below."""
    calls = []

    class _Gateway:
        async def complete_json(self, *args, **kwargs):
            calls.append(args)
            raise AssertionError("singletons must not reach the model")

    monkeypatch.setattr(topics, "gateway", _Gateway())
    assert await topics._name_clusters([["astronomy"], ["geology"]]) == {}
    assert calls == []


async def test_a_name_unrelated_to_its_cluster_is_rejected(monkeypatch):
    """Regression (found live, 2026-07-29): asked to name 734 clusters in one
    call, the fast model stayed aligned for 98, slipped one index, and misnamed
    the remaining 633 — the {quantum physics, quantum mechanics} cluster came
    back named "roman history". Nothing detected it, because a wrong index is a
    perfectly well-formed response.

    The name must now share a word with a member, so a misaligned answer is
    dropped and the cluster keeps its own most-frequent member as its name."""
    from episteme.llm.schemas import TopicVocabulary

    class _Gateway:
        async def complete_json(self, *args, **kwargs):
            return TopicVocabulary.model_validate(
                {"topics": [{"index": 0, "label": "roman history"}]}
            )

    monkeypatch.setattr(topics, "gateway", _Gateway())
    named = await topics._name_clusters([["quantum physics", "quantum mechanics"]])
    assert named == {}


async def test_naming_is_batched_so_alignment_stays_tractable(monkeypatch):
    """The single 734-entry call is what broke; batches bound how far a slipped
    index can propagate."""
    from episteme.llm.schemas import TopicVocabulary

    batch_sizes = []

    class _Gateway:
        async def complete_json(self, role, system, user, schema):
            batch_sizes.append(len(user.strip().splitlines()) - 2)
            return TopicVocabulary.model_validate({"topics": []})

    monkeypatch.setattr(topics, "gateway", _Gateway())
    clusters = [[f"topic {i}", f"topic {i} variant"] for i in range(topics.NAMING_BATCH * 2 + 3)]
    await topics._name_clusters(clusters)
    assert len(batch_sizes) == 3
    assert max(batch_sizes) <= topics.NAMING_BATCH


@pytest.mark.parametrize(
    ("label", "members", "ok"),
    [
        ("conservation biology", ["conservation", "wildlife conservation"], True),
        ("artificial intelligence", ["ai", "artificial intelligence"], True),
        ("deep sea biology", ["deep-sea biology"], True),
        ("roman history", ["quantum physics", "quantum mechanics"], False),
        ("sports", ["space medicine", "space biology"], False),
        ("", ["astronomy"], False),
    ],
)
def test_name_plausibility_accepts_coined_names_but_rejects_misalignment(
    label, members, ok
):
    """Permissive by design: it must not reject a legitimately coined canonical
    name, only one that cannot belong to this cluster at all."""
    assert topics._plausible_name(label, members) is ok


# --- Label remapping --------------------------------------------------------------


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _TableSession:
    """Serves each `select(Model)` from its own list, so a rewrite of the label
    columns and a rewrite of the feedback log can be observed separately."""

    def __init__(self, **rows_by_model):
        self.rows = rows_by_model
        self.deleted: list = []

    async def execute(self, stmt):
        entity = stmt.column_descriptions[0]["entity"]
        return _Result(self.rows.get(entity.__name__, []))

    async def commit(self):
        return None

    async def delete(self, row):
        self.deleted.append(row)


async def test_rename_keeps_the_slug_and_teaches_the_new_spelling(monkeypatch):
    """A rename is a display change. The slug is the topic's permanent identity —
    the interest weight learned over months is filed under it, and so is anything
    that will later relate topics to each other — so moving it would orphan all of
    that. What the rename must do is make the NEW wording resolve back here;
    without that alias the next emission of it slips past `_by_slug` and mints a
    duplicate row for the topic that was just renamed."""
    topic = Topic(id=1, slug="blue", label="blue", aliases=[])
    _stub_lookups(monkeypatch, by_slug={"blue": topic})
    story = Story(topics=["blue"])

    await topics.rename(_TableSession(Story=[story]), "blue", "The Colour Blue")

    assert topic.slug == "blue"
    assert topic.label == "the colour blue"
    assert "the-colour-blue" in topic.aliases
    assert story.topics == ["the colour blue"]


async def test_merge_carries_the_absorbed_topics_history_to_the_survivor(monkeypatch):
    """Unlike a rename, a merge DOES end an identity — one of the two slugs is
    about to stop existing. Its weight has to follow it, or the operation whose
    premise is "these were always the same topic" is also the one that throws away
    what was learned about half of it. The log is repointed rather than the derived
    profile, so the next replay folds both histories into the surviving key."""
    source = Topic(id=1, slug="quantum-mechanics", label="quantum mechanics", aliases=["qm"])
    target = Topic(id=2, slug="quantum-physics", label="quantum physics", aliases=[])
    _stub_lookups(
        monkeypatch,
        by_slug={"quantum-mechanics": source, "quantum-physics": target},
    )
    steered = Feedback(
        id=1, kind="more_topic", topic="quantum-mechanics",
        topics_snapshot=["quantum-mechanics"], source_ids=[],
    )
    liked = Feedback(
        id=2, kind="like", topic=None,
        topics_snapshot=["quantum-mechanics", "astronomy"], source_ids=[],
    )
    stated = Feedback(
        id=3, kind="nl_feedback", topics_snapshot=[], source_ids=[],
        parsed_intent={
            "topics": [{"topic": "quantum-mechanics", "direction": "more", "strength": 2.0}]
        },
    )
    session = _TableSession(Feedback=[steered, liked, stated])

    await topics.merge(session, "quantum-mechanics", "quantum-physics")

    assert steered.topic == "quantum-physics"
    assert liked.topics_snapshot == ["quantum-physics", "astronomy"]
    assert stated.parsed_intent["topics"][0]["topic"] == "quantum-physics"
    assert stated.parsed_intent["topics"][0]["strength"] == 2.0  # only the key moved
    assert session.deleted == [source]
    # The absorbed slug and its aliases keep resolving, now to the survivor.
    assert "quantum-mechanics" in target.aliases and "qm" in target.aliases


async def test_merge_leaves_other_topics_signals_alone(monkeypatch):
    source = Topic(id=1, slug="qm", label="qm", aliases=[])
    target = Topic(id=2, slug="quantum-physics", label="quantum physics", aliases=[])
    _stub_lookups(monkeypatch, by_slug={"qm": source, "quantum-physics": target})
    other = Feedback(id=1, kind="more_topic", topic="astronomy", topics_snapshot=[], source_ids=[])

    await topics.merge(_TableSession(Feedback=[other]), "qm", "quantum-physics")

    assert other.topic == "astronomy"


async def test_backfill_does_nothing_and_costs_nothing_when_there_is_nothing_to_do():
    """The healthy steady state: called from the embed stage on every run, so it
    must not embed or commit when every entry already has a vector."""
    assert await topics.backfill_embeddings(_TableSession(Topic=[])) == 0


def test_remap_preserves_order_and_collapses_merge_duplicates():
    mapping = {"astrophysics": "astronomy", "space": "astronomy"}
    assert topics.remap_labels(
        ["astrophysics", "genetics", "space"], mapping
    ) == ["astronomy", "genetics"]


def test_remap_leaves_unmapped_labels_alone():
    """A story ingested after the mapping was computed keeps its raw label rather
    than losing it — `resolve` folds it in on the next pass."""
    assert topics.remap_labels(["brand new topic"], {"old": "new"}) == ["brand new topic"]


def test_remap_handles_missing_topics():
    assert topics.remap_labels(None, {"a": "b"}) == []
