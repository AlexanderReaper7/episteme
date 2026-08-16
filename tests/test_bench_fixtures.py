"""Fixture capture: flattening a tool loop and truncating for a ladder rung."""

from episteme.bench.fixtures import flatten_for_replay, pick_percentile, truncate


def test_a_tool_loop_flattens_to_a_plain_conversation_ending_on_a_user_turn():
    """The research text is the context size being measured, so it stays. What
    goes is the plumbing: a completion request has to have something to answer,
    and a transcript ending on the assistant's article would ask the model to
    continue its own output - a different workload of a different length."""
    transcript = [
        {"role": "system", "content": "You are a writer."},
        {"role": "user", "content": "Write about anglerfish."},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "1"}]},
        {"role": "tool", "content": "SEARCH RESULTS: bioluminescence …"},
        {"role": "assistant", "content": "Here is the article."},
    ]
    assert flatten_for_replay(transcript) == [
        {"role": "system", "content": "You are a writer."},
        {"role": "user", "content": "Write about anglerfish.\n\nSEARCH RESULTS: bioluminescence …"},
    ]


def test_consecutive_same_role_turns_are_merged():
    """A chat template handed two user messages in a row is entitled to render
    them oddly, and the prompt has to be the same shape on every model compared."""
    flat = flatten_for_replay(
        [{"role": "tool", "content": "a"}, {"role": "tool", "content": "b"}]
    )
    assert flat == [{"role": "user", "content": "a\n\nb"}]


def test_multimodal_parts_keep_their_text_and_drop_the_image():
    """An mmproj encode is a different measurement, and base64 image bytes would
    bloat the stored fixture enormously."""
    flat = flatten_for_replay(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Does this look right?"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
                ],
            }
        ]
    )
    assert flat == [{"role": "user", "content": "Does this look right?"}]


def test_truncation_keeps_the_instruction_whole():
    """A ladder whose short rungs asked a different question than its long ones
    would be measuring two workloads."""
    messages = [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "x" * 4000},
        {"role": "user", "content": "the question"},
    ]
    cut = truncate(messages, target_tokens=100, prompt_tokens=1000)
    assert cut[0] == messages[0] and cut[-1] == messages[-1]
    assert 0 < len(cut[1]["content"]) < 4000


def test_truncation_is_a_no_op_when_the_fixture_already_fits():
    messages = [{"role": "user", "content": "short"}]
    assert truncate(messages, 8192, 12) == messages
    assert truncate(messages, 8192, None) == messages


def test_an_instruction_larger_than_the_rung_survives_intact():
    """Returning it whole means the measured `prompt_n` shows the rung was not
    met, which is a readable point. Mutilating the instruction would be a
    silently different measurement."""
    messages = [
        {"role": "system", "content": "S" * 5000},
        {"role": "user", "content": "filler" * 100},
        {"role": "user", "content": "Q" * 5000},
    ]
    assert truncate(messages, target_tokens=10, prompt_tokens=10000) == [
        messages[0], messages[-1]
    ]


def test_percentile_one_is_the_largest_conversation_on_record():
    """The nightly run's wall clock is set by its worst call, not its median."""
    candidates = [{"prompt_tokens": n, "chain_id": str(n)} for n in (500, 19000, 4000)]
    assert pick_percentile(candidates, 1.0)["prompt_tokens"] == 19000
    assert pick_percentile(candidates, 0.0)["prompt_tokens"] == 500
    assert pick_percentile(candidates, 0.5)["prompt_tokens"] == 4000
    assert pick_percentile([], 1.0) is None
