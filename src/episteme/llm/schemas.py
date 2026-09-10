"""Pydantic schemas for every structured LLM output.

These are the contract between prompts and the rest of the system: LLM responses
are constrained to the generated JSON schema server-side (llama.cpp grammar) and
validated here client-side. Invalid output never leaves this layer.
"""

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field, TypeAdapter, model_validator


class TriageResult(BaseModel):
    decision: Literal["write", "aggregate", "skip"]
    # Ranking signal: higher = more learning value / more interesting. Deliberately
    # unbounded above (guide the model to ~0-10 but let it exceed for standouts) so the
    # scale keeps meaning as the feed grows — the writer works candidates best-first.
    quality_score: float = Field(ge=0, description="Learning value + interest; ~0-10, higher is better")
    topics: list[str] = Field(min_length=1, max_length=3)
    reason: str = Field(max_length=500)


class TopicIntent(BaseModel):
    topic: str = Field(max_length=60, description="Lowercase topic name")
    direction: Literal["more", "less"]
    strength: float = Field(
        default=1.0, ge=0.0, le=2.0, description="1.0 = a normal request, 2.0 = emphatic"
    )


class ProfileIntent(BaseModel):
    """A free-text statement about what the reader wants, turned into profile
    changes. Persisted on the feedback row (`parsed_intent`), which makes it the
    canonical record: the profile is replayed from feedback, and replaying must
    never need a second LLM call to reinterpret the same sentence."""

    topics: list[TopicIntent] = Field(default_factory=list, max_length=12)
    blocked_keywords: list[str] = Field(
        default_factory=list,
        max_length=10,
        description="Only for an explicit hard refusal ('never show me X')",
    )
    difficulty: Literal["introductory", "intermediate", "technical"] | None = Field(
        default=None, description="Only if the reader stated a depth preference"
    )
    echo: str = Field(
        max_length=300,
        description="One sentence back to the reader confirming what was understood",
    )


class TopicName(BaseModel):
    index: int = Field(ge=0, description="Index of the cluster being named")
    label: str = Field(max_length=60, description="Lowercase canonical topic name")


class TopicVocabulary(BaseModel):
    """Names for the clusters of existing free-text topic tags found in the
    database (recommend.topics.propose_vocabulary). Indices the model omits or
    invents are ignored — the cluster's most frequent member is the fallback —
    so this output shapes wording, never structure."""

    topics: list[TopicName]


class TopicMatch(BaseModel):
    """One proposed tag's verdict against the existing vocabulary."""

    proposed: str = Field(description="The proposed tag, copied exactly")
    existing: str | None = Field(
        default=None,
        description="An offered existing tag meaning the same subject area, copied "
        "exactly, or null if none does",
    )


class TopicMatches(BaseModel):
    """The dedup turn's answer (recommend.topics._review_matches).

    Matched by the STRINGS the model echoes, never by position: `proposed` must
    be one of the tags actually asked about and `existing` one of the candidates
    offered for that tag, both checked in code afterwards. An echo that fails
    either check is dropped, which degrades to "no match" — a new vocabulary
    entry, i.e. exactly the behaviour before this tier existed. *A positional
    zip is what misnamed 86% of the vocabulary during the Phase 4 bootstrap.*
    """

    matches: list[TopicMatch]


class CardSummary(BaseModel):
    """The `summarize` stage's whole output: the text of one feed card (0050).

    **`max_length` is a runaway guard, never a length control.** llama.cpp turns
    it into a grammar rule, so the string stops at exactly that many characters
    with the model mid-word and pydantic accepting it - a silent guillotine, not
    a retry. Measured 2026-08-30: at `max_length=800`, two of the first three
    summaries came back 800 and 799 characters long, both ending mid-sentence.
    The length the card wants is asked for in the PROMPT, as a sentence count.
    A character budget was tried first and did nothing: told to "aim for 400-500
    characters and never exceed 600", the model returned 743, 750 and 815. The
    card clamps at nine lines, which is sized to what the model actually writes
    rather than to what it was asked for. Measured over all 688 summaries once
    the backfill had run: median 695 characters, p90 1009, max 1188, and nine
    lines holds about 930 - so roughly one card in six is clamped and carries an
    expand control, and five in six show the whole thing.

    `_reads_as_finished` is what makes the failure loud if it happens anyway. A
    guillotined string ends mid-word; a written one ends on punctuation. Failing
    validation sends it back through `complete_json`'s repair retry instead of
    storing half a sentence on a card.
    """

    summary: str = Field(
        description="3-4 sentences that stand alone: what happened, the numbers "
        "that matter, and why it is worth knowing",
        max_length=1200,
    )

    @model_validator(mode="after")
    def _reads_as_finished(self) -> "CardSummary":
        if not self.summary.strip().endswith((".", "!", "?", '"', "'", ")", "…")):
            raise ValueError(
                "summary ends mid-sentence - write a shorter one that finishes"
            )
        return self


class ProseSection(BaseModel):
    type: Literal["prose"]
    text: str = Field(description="Markdown body text")


class KeyPointsSection(BaseModel):
    type: Literal["key_points"]
    items: list[str] = Field(min_length=2, max_length=8)


# --- Rich/media sections (spec §6, Phase 3) ---------------------------------------
# Media sections are closed-set like citations: the writer may only use URLs offered
# in its "Available media" list — pipeline.sanitize_media_sections drops anything
# else and stamps attribution from the DB, so media can't be hallucinated.


class ImageSection(BaseModel):
    type: Literal["image"]
    url: str = Field(description="EXACT URL from this story's 'Available media' list")
    caption: str = Field(max_length=500)


class VideoSection(BaseModel):
    type: Literal["video"]
    url: str = Field(description="EXACT URL from this story's 'Available media' list")
    caption: str = Field(default="", max_length=500)


class QuizQuestion(BaseModel):
    """One multiple-choice question inside a quiz section."""

    question: str = Field(max_length=500)
    choices: list[str] = Field(min_length=2, max_length=6)
    answer_index: int = Field(ge=0, description="0-based index into choices")
    explanation: str = Field(max_length=1000)

    @model_validator(mode="after")
    def _answer_in_range(self) -> "QuizQuestion":
        if self.answer_index >= len(self.choices):
            raise ValueError(
                f"answer_index {self.answer_index} out of range for {len(self.choices)} choices"
            )
        return self


class QuizSection(BaseModel):
    """A short comprehension check: one or a few multiple-choice questions.

    Storage is always the `questions` list, including for a single question — the
    pre-2026-08-02 flat shape (question/choices/answer_index/explanation on the
    section itself) was migrated, so nothing downstream carries a compatibility
    branch.
    """

    type: Literal["quiz"]
    questions: list[QuizQuestion] = Field(min_length=1, max_length=5)


class ChartAxis(BaseModel):
    """One Vega-Lite encoding channel: which key in the data rows it reads, and how
    to treat those values."""

    field: str = Field(max_length=100, description="A key present in every data row")
    type: Literal["quantitative", "nominal", "ordinal", "temporal"] = Field(
        description="quantitative = numbers, nominal = unordered categories, "
        "ordinal = ranked categories, temporal = dates"
    )
    title: str = Field(default="", max_length=100, description="Axis label; defaults to the field name")


class ChartEncoding(BaseModel):
    x: ChartAxis
    y: ChartAxis
    color: ChartAxis | None = Field(
        default=None, description="Optional third channel splitting the marks by category"
    )


class ChartData(BaseModel):
    values: list[dict[str, str | float]] = Field(
        min_length=2,
        max_length=60,
        description="The rows to plot, each a flat object of the same keys",
    )


class ChartSpec(BaseModel):
    """The Vega-Lite subset the renderer accepts.

    Typed rather than `dict[str, Any]` because an untyped dict is the one field in
    the whole `Section` union that escapes the grammar — every shape-constrained
    field came back well-formed while this one came back as invented syntax. Post
    427's stored spec was `{"data": [...], "x_axis": "City", "y_axis": "%"}`: no
    `mark`, no `encoding`, `data` a bare list. Vega-Lite rejected it, the figure
    collapsed to its (empty) caption, and the post served a blank box at
    quality_score 9. A model cannot emit that shape against this schema.

    Deliberately a subset: four marks and two-to-three channels cover the charts a
    science post needs, and every construct left out is one the renderer would have
    had to be trusted with untested.
    """

    mark: Literal["bar", "line", "point", "area"]
    data: ChartData
    encoding: ChartEncoding
    title: str = Field(default="", max_length=200, description="Chart title")

    @model_validator(mode="after")
    def _channels_match_the_data(self) -> "ChartSpec":
        """Every encoded field must exist in the rows. This is the failure the shape
        constraint alone cannot catch: a spec can be flawless Vega-Lite and still draw
        an empty frame because `encoding.x.field` names a key no row has."""
        rows = self.data.values
        # EVERY row, not any row: a field one row happens to carry is enough for the
        # spec to be well-formed and still draws one bar out of N, which is the same
        # class of silent half-failure this validator exists to catch.
        every_row = set(rows[0]).intersection(*(set(row) for row in rows[1:]))
        any_row: set[str] = set().union(*(set(row) for row in rows))
        channels = [("x", self.encoding.x), ("y", self.encoding.y), ("color", self.encoding.color)]
        for name, axis in channels:
            if axis is None or axis.field in every_row:
                continue
            where = "only some rows" if axis.field in any_row else "no row"
            raise ValueError(
                f"encoding.{name}.field {axis.field!r} is in {where} of data.values "
                f"(keys every row has: {sorted(every_row)}) — the chart would render "
                "incomplete or empty"
            )
        return self


class ChartSection(BaseModel):
    type: Literal["chart"]
    spec: ChartSpec = Field(
        description="The chart. Only plot real numbers taken from the sources — "
        "never invented ones."
    )
    caption: str = Field(default="", max_length=500)


# Mermaid's opening keyword selects the parser; without a recognised one the whole
# source is a syntax error and the figure renders blank. This is the cheap half of
# diagram validation — the grammar cannot constrain a DSL held in a string, so the
# rest is what QA's screenshot still exists for.
MERMAID_DIAGRAM_TYPES = (
    "flowchart", "graph", "sequenceDiagram", "classDiagram", "stateDiagram",
    "stateDiagram-v2", "erDiagram", "journey", "gantt", "pie", "quadrantChart",
    "requirementDiagram", "gitGraph", "mindmap", "timeline", "sankey-beta",
    "xychart-beta", "block-beta",
)


class DiagramSection(BaseModel):
    type: Literal["diagram"]
    mermaid: str = Field(
        min_length=1,
        description="Mermaid source. MUST open with a diagram type, e.g. "
        "'flowchart LR\\n  A[Input] --> B[Output]'",
    )
    caption: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def _declares_a_diagram_type(self) -> "DiagramSection":
        lines = self.mermaid.splitlines()
        # Frontmatter is a YAML block fenced by --- at the very top, and its BODY is
        # arbitrary keys. Skipping only the fences left `title: X` reading as the
        # declaration, so valid Mermaid was rejected and burned a repair retry.
        first = next((i for i, line in enumerate(lines) if line.strip()), None)
        if first is not None and lines[first].strip() == "---":
            close = next(
                (i for i in range(first + 1, len(lines)) if lines[i].strip() == "---"), None
            )
            if close is None:
                raise ValueError("mermaid frontmatter opens with '---' and is never closed")
            lines = lines[close + 1 :]
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("%%"):
                continue  # directives precede the declaration
            word = stripped.split()[0].rstrip(":")
            if word not in MERMAID_DIAGRAM_TYPES:
                raise ValueError(
                    f"mermaid source opens with {word!r}, which is not a diagram type; "
                    f"it must start with one of: {', '.join(MERMAID_DIAGRAM_TYPES)}"
                )
            return self
        raise ValueError("mermaid source is empty")


class TimelineEvent(BaseModel):
    date: str = Field(max_length=100, description="Free-form: '1969', 'March 2026', '4.5 Gya'")
    label: str = Field(max_length=500)


class TimelineSection(BaseModel):
    type: Literal["timeline"]
    events: list[TimelineEvent] = Field(min_length=2, max_length=15)


class GlossaryTerm(BaseModel):
    term: str = Field(max_length=100)
    definition: str = Field(max_length=500)


class GlossarySection(BaseModel):
    type: Literal["glossary"]
    terms: list[GlossaryTerm] = Field(min_length=1, max_length=15)


def _section_type(section) -> str | None:
    """The `type` of a section held either as a dict (stored JSONB) or as a model
    instance (fresh from validation) — both shapes reach the quiz invariant."""
    if isinstance(section, dict):
        return section.get("type")
    return getattr(section, "type", None)


def has_quiz(sections) -> bool:
    return any(_section_type(section) == "quiz" for section in sections or [])


NO_QUIZ_MESSAGE = (
    "every post must include a 'quiz' section with at least one multiple-choice "
    "question checking the reader understood it"
)


def _require_quiz(sections: list) -> None:
    """Every article carries a comprehension check (user decision 2026-08-02).

    The JSON-schema grammar constrains each section's *shape* but cannot demand the
    presence of a member in a list, so this is where "all posts have a quiz" is
    actually enforced for the writer's draft. A failure raises, and the repair-retry
    loop (`agent.request_validated`) puts this message straight back in front of the
    model; guidance in the prompt alone left it optional in practice.

    The QA stage enforces the same invariant somewhere else on purpose: it edits the
    body one section at a time, so the check belongs on the action that could break
    it (`worker.qa` refuses a delete/replace that would remove the last quiz) rather
    than on a whole-body payload QA no longer submits.
    """
    if not has_quiz(sections):
        raise ValueError(NO_QUIZ_MESSAGE + " — add one")


Section = Annotated[
    Union[
        ProseSection,
        KeyPointsSection,
        ImageSection,
        VideoSection,
        QuizSection,
        ChartSection,
        DiagramSection,
        TimelineSection,
        GlossarySection,
    ],
    Field(discriminator="type"),
]

section_adapter: TypeAdapter[Any] = TypeAdapter(Section)


def section_param_schema() -> tuple[dict, dict]:
    """The JSON schema for ONE section, split into (schema, defs).

    Tool-call arguments are grammar-constrained by llama.cpp exactly like a
    `response_format` schema, so the QA editor's `section` parameter can be the real
    Section union rather than a free-form object — the same double enforcement
    (grammar + pydantic) the rest of the LLM boundary gets.

    Pydantic emits `$ref: "#/$defs/…"` against the document ROOT, and the root the
    converter sees is the tool's `parameters` object — so the caller must hoist these
    defs to that level for the refs to resolve. Returning them separately makes that
    unmissable.
    """
    schema = section_adapter.json_schema()
    return schema, schema.pop("$defs", {})


class PostDraft(BaseModel):
    # No `summary`, deliberately (0050): the card summary is written by the
    # `summarize` stage from the FINISHED body, after QA has edited it. Asking
    # the writer for one produced a hook that described a post it had not written
    # yet, and that a later stage would overwrite regardless.
    title: str = Field(max_length=300)
    difficulty: Literal["introductory", "intermediate", "technical"]
    topics: list[str] = Field(min_length=1, max_length=4)
    sections: list[Section] = Field(min_length=1, max_length=12)
    # Closed-set selection, not free text: code intersects these with the research
    # loop's fetch log, so only pages the model actually fetched can appear in the
    # post's further-reading section — the model contributes judgment (which fetched
    # pages were relevant), never URLs. Required so an empty list is a deliberate
    # "none were worth recommending", not an omission.
    further_reading_urls: list[str] = Field(
        max_length=8,
        description="Exact 'Fetched:' URLs of pages you fetched this conversation that "
        "a reader would genuinely benefit from — omit dead ends, irrelevant pages, and "
        "the original source items. Empty list if none qualify.",
    )

    @model_validator(mode="after")
    def _has_quiz(self) -> "PostDraft":
        _require_quiz(self.sections)
        return self


class QAReview(BaseModel):
    """The qa stage's closing verdict on a post (spec §7 stage 5).

    Verdict ONLY — it carries no body. QA revises through the editor tools in
    `worker.qa`, section by section, so by the time this is asked for the changes are
    already applied. Keeping a replacement body here coupled two independent things:
    a `demote` that happened to carry sections used to fail validation outright,
    which discarded the demotion and left the post to be reviewed forever.
    """

    verdict: Literal["approve", "revise", "demote"]
    # Same open-ended scale as TriageResult.quality_score: ~0-10, honest ranking signal.
    quality_score: float = Field(ge=0, description="~0-10, higher is better")
    critique: str = Field(max_length=2000)
