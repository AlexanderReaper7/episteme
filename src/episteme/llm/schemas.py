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
