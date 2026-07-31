"""Pydantic schemas for every structured LLM output.

These are the contract between prompts and the rest of the system: LLM responses
are constrained to the generated JSON schema server-side (llama.cpp grammar) and
validated here client-side. Invalid output never leaves this layer.
"""

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field, model_validator


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


class SourceSummary(BaseModel):
    summary: str = Field(
        description="3-5 sentences preserving key facts, numbers, and named entities"
    )


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


class QuizSection(BaseModel):
    """One multiple-choice question testing whether the reader understood the post."""

    type: Literal["quiz"]
    question: str = Field(max_length=500)
    choices: list[str] = Field(min_length=2, max_length=6)
    answer_index: int = Field(ge=0, description="0-based index into choices")
    explanation: str = Field(max_length=1000)

    @model_validator(mode="after")
    def _answer_in_range(self) -> "QuizSection":
        if self.answer_index >= len(self.choices):
            raise ValueError(
                f"answer_index {self.answer_index} out of range for {len(self.choices)} choices"
            )
        return self


class ChartSection(BaseModel):
    type: Literal["chart"]
    spec: dict[str, Any] = Field(
        description="Vega-Lite spec with inline data (data.values). Only chart real "
        "numbers taken from the sources — never invented ones."
    )
    caption: str = Field(default="", max_length=500)


class DiagramSection(BaseModel):
    type: Literal["diagram"]
    mermaid: str = Field(description="Mermaid source, e.g. 'flowchart LR; A --> B'")
    caption: str = Field(default="", max_length=500)


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


class PostDraft(BaseModel):
    title: str = Field(max_length=300)
    summary: str = Field(description="One-paragraph hook", max_length=1000)
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


class QAReview(BaseModel):
    """The qa stage's verdict on a rendered post (spec §7 stage 5)."""

    verdict: Literal["approve", "revise", "demote"]
    # Same open-ended scale as TriageResult.quality_score: ~0-10, honest ranking signal.
    quality_score: float = Field(ge=0, description="~0-10, higher is better")
    critique: str = Field(max_length=2000)
    # Set only for verdict="revise": complete replacement body (sources /
    # further_reading are rebuilt from the database, never revised by the model).
    revised_title: str | None = None
    revised_summary: str | None = None
    revised_sections: list[Section] | None = None
