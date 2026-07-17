import pytest

from episteme.config import settings


@pytest.fixture(autouse=True)
def _disable_llm_call_log():
    """Unit tests must never write llm_calls rows (no DB in tests; and the dev
    DATABASE_URL may point at the real compose database)."""
    original = settings.llm_log_enabled
    settings.llm_log_enabled = False
    yield
    settings.llm_log_enabled = original
