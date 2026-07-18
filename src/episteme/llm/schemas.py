"""Pydantic schemas for every structured LLM output.

These are the contract between prompts and the rest of the system: LLM responses
are constrained to the generated JSON schema server-side (llama.cpp grammar) and
validated here client-side. Invalid output never leaves this layer.
"""

from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field


class TriageResult(BaseModel):
    decision: Literal["write", "aggregate", "skip"]
    # Ranking signal: higher = more learning value / more interesting. Deliberately
    # unbounded above (guide the model to ~0-10 but let it exceed for standouts) so the
    # scale keeps meaning as the feed grows — the writer works candidates best-first.
    quality_score: float = Field(ge=0, description="Learning value + interest; ~0-10, higher is better")
    topics: list[str] = Field(min_length=1, max_length=3)
    reason: str = Field(max_length=500)


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


Section = Annotated[Union[ProseSection, KeyPointsSection], Field(discriminator="type")]


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
