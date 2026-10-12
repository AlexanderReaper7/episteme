"""A post is written in its story's primary item's language (0063): the source
declares it as `config["language"]`, and every prompt that writes or edits the
post's text is told it explicitly, English included."""

from types import SimpleNamespace

import pytest

from episteme.llm.agent import writer_harness
from episteme.models import Source
from episteme.worker.pipeline import story_language
from episteme.worker.qa import qa_harness


def _source(**config) -> Source:
    return Source(type_name="rss", name="S", config=config)


def test_a_source_without_a_language_is_english():
    assert _source().language == "English"
    assert _source(language="sv").language == "Swedish"


def test_an_unknown_language_code_fails_loudly():
    """A typo in a row must stop the stage, not quietly write in English."""
    with pytest.raises(KeyError):
        _ = _source(language="se").language


def test_the_story_follows_its_primary_item():
    items = [SimpleNamespace(source=_source(language="sv")), SimpleNamespace(source=_source())]
    assert story_language(items) == "Swedish"
    assert story_language(items[::-1]) == "English"


@pytest.mark.parametrize("language", ["English", "Swedish"])
def test_the_writer_is_told_the_language(language):
    system = writer_harness(language=language).system
    assert f"Write in {language}:" in system


def test_qa_is_told_not_to_translate():
    system = qa_harness(
        None, None, None, language="Swedish", visual=False, on_idle=lambda _t: None
    ).system
    assert "written in Swedish" in system
    assert "never translate" in system
