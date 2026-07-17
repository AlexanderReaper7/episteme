"""Research loop: it drives tools, assembles the dossier, and enforces budgets."""

import episteme.llm.agent as agent
from episteme.config import settings


def _assistant_toolcall(name, args, call_id="c1"):
    return {"role": "assistant", "content": None,
            "tool_calls": [{"id": call_id, "function": {"name": name, "arguments": args}}]}


def _assistant_final(text):
    return {"role": "assistant", "content": text}


class ScriptedGateway:
    """Returns pre-scripted assistant messages, one per loop turn."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    async def chat_messages(self, role, messages, tools=None, temperature=0.3):
        if self.calls >= len(self.script):
            return _assistant_final("done")  # exhausted -> stop gathering
        msg = self.script[self.calls]
        self.calls += 1
        return msg


async def test_loop_gathers_dossier_and_stops_on_final(monkeypatch):
    # Two fetches (>= the stop threshold) then a final message -> clean stop, no nudge.
    monkeypatch.setattr(
        agent, "gateway",
        ScriptedGateway([
            _assistant_toolcall("fetch_page", '{"url": "https://esawebb.org/images/potm2606a/"}', "c1"),
            _assistant_toolcall("fetch_page", '{"url": "https://ui.adsabs.harvard.edu/abs/paper"}', "c2"),
            _assistant_final("Fetched the ESA Webb page and the paper; those are the strongest sources."),
        ]),
    )

    async def fake_fetch(url):
        return {"url": url, "title": f"title of {url}",
                "text": f"A far richer description from {url}.", "links": []}

    monkeypatch.setattr(agent, "fetch_page", fake_fetch)

    result = await agent.run_research_loop("sys", "seed")

    assert "far richer description" in result.dossier
    assert [e["url"] for e in result.fetch_log] == [
        "https://esawebb.org/images/potm2606a/",
        "https://ui.adsabs.harvard.edu/abs/paper",
    ]
    assert "strongest sources" in result.agent_notes


async def test_loop_nudges_when_model_narrates_instead_of_acting(monkeypatch):
    # Model returns a plan (no tool_calls) before fetching anything -> gets nudged,
    # then actually fetches on the next turn.
    monkeypatch.setattr(
        agent, "gateway",
        ScriptedGateway([
            _assistant_final("Next I will fetch the ESA Webb page and the paper."),
            _assistant_toolcall("fetch_page", '{"url": "https://esawebb.org/x"}', "c1"),
            _assistant_toolcall("fetch_page", '{"url": "https://ads/y"}', "c2"),
            _assistant_final("Done — fetched both."),
        ]),
    )

    async def fake_fetch(url):
        return {"url": url, "title": url, "text": f"rich text {url}", "links": []}

    monkeypatch.setattr(agent, "fetch_page", fake_fetch)

    result = await agent.run_research_loop("sys", "seed")
    assert len(result.fetch_log) == 2  # the nudge got it to act


async def test_fetch_budget_is_enforced(monkeypatch):
    monkeypatch.setattr(settings, "enrich_max_fetches", 1)
    calls = []

    monkeypatch.setattr(
        agent, "gateway",
        ScriptedGateway([
            _assistant_toolcall("fetch_page", '{"url": "https://a.org"}', "c1"),
            _assistant_toolcall("fetch_page", '{"url": "https://b.org"}', "c2"),
            _assistant_final("done"),
        ]),
    )

    async def fake_fetch(url):
        calls.append(url)
        return {"url": url, "title": url, "text": "text", "links": []}

    monkeypatch.setattr(agent, "fetch_page", fake_fetch)

    result = await agent.run_research_loop("sys", "seed")

    assert calls == ["https://a.org"]  # second fetch refused by the budget
    assert len(result.fetch_log) == 1


async def test_loop_stops_at_max_steps(monkeypatch):
    monkeypatch.setattr(settings, "enrich_max_steps", 3)
    monkeypatch.setattr(settings, "enrich_max_searches", 99)

    # Always asks to search — never volunteers a final message.
    class AlwaysSearch:
        calls = 0

        async def chat_messages(self, role, messages, tools=None, temperature=0.3):
            self.calls += 1
            return _assistant_toolcall("web_search", '{"query": "x"}', f"c{self.calls}")

    gw = AlwaysSearch()
    monkeypatch.setattr(agent, "gateway", gw)

    async def fake_search(q):
        return [{"title": "t", "url": "https://x.org", "snippet": "s"}]

    monkeypatch.setattr(agent, "web_search", fake_search)

    await agent.run_research_loop("sys", "seed")
    assert gw.calls == 3  # capped at enrich_max_steps, no runaway
