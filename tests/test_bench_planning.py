"""The pure half of the runner: planning, gating, contamination, form params.

Everything here decides WHAT gets measured. It is separated from the executor
precisely so it can be tested in milliseconds, where one real repetition of the
longctx scenario is seven minutes on the fast model and twenty-four on the slow
one.
"""

import pytest

from episteme.bench.runner import contaminated, gate, plan_items
from episteme.config import settings
from episteme.models import BenchmarkFixture
from episteme.web.bench import build_params


def _fixture() -> BenchmarkFixture:
    return BenchmarkFixture(
        name="write-max",
        kind="replay",
        stage="write",
        messages=[
            {"role": "system", "content": "s" * 400},
            {"role": "user", "content": "u" * 40000},
            {"role": "assistant", "content": "a" * 400},
            {"role": "user", "content": "write it"},
        ],
        prompt_tokens=10000,
    )


def test_a_models_work_is_contiguous():
    """`--models-max 1` evicts on every swap and a load is 10-15 s. Interleaving
    would pay for a swap per sample and fold that into whichever phase was
    running."""
    items = plan_items("quick", ["big", "small"], {"reps": 3}, None)
    assert [item.model for item in items] == ["big"] * 4 + ["small"] * 4


def test_the_warmup_is_rep_zero_and_can_be_turned_off():
    with_warmup = plan_items("quick", ["m"], {"reps": 2, "warmup": True}, None)
    assert [(item.rep, item.warmup) for item in with_warmup] == [(0, True), (1, False), (2, False)]
    without = plan_items("quick", ["m"], {"reps": 2, "warmup": False}, None)
    assert [item.warmup for item in without] == [False, False]


def test_the_ladder_generates_almost_nothing():
    """It measures how prefill scales. 256 tokens per rung would spend most of
    the GPU time on the half not being measured."""
    items = plan_items("ladder", ["m"], {"rungs": [1000, 2000]}, _fixture())
    assert [item.rung for item in items] == [1000, 2000]
    assert {item.predict for item in items} == {8}
    # Shorter rung, shorter prompt - the truncation actually ran.
    assert sum(len(m["content"]) for m in items[0].messages) < sum(
        len(m["content"]) for m in items[1].messages
    )


def test_a_sweep_repeats_every_model_under_every_variant():
    items = plan_items(
        "sweep",
        ["a", "b"],
        {"reps": 1, "warmup": False, "variants": [{"label": "x"}, {"label": "y"}]},
        _fixture(),
    )
    assert [(item.variant, item.model) for item in items] == [
        ("x", "a"), ("x", "b"), ("y", "a"), ("y", "b")
    ]


def test_scenarios_that_predict_wall_time_require_a_real_fixture():
    """A synthetic 4k prompt reports 192.5 tok/s of prefill where a real 18.7k
    writer call gets 54.4. Only `quick` is allowed to be synthetic."""
    for scenario in ("longctx", "ladder", "sweep"):
        with pytest.raises(ValueError, match="needs a fixture"):
            plan_items(scenario, ["m"], {}, None)
    assert plan_items("quick", ["m"], {}, None)


def test_gate_refuses_a_contended_card_and_shrugs_at_a_missing_agent():
    assert gate(None) is None  # optional infrastructure: no sensor, no opinion
    assert gate({"games_running": [], "foreign_gpu_percent": 2.0}) is None
    assert "Warframe" in gate({"games_running": ["Warframe"], "foreign_gpu_percent": 0})
    busy = {"games_running": [], "foreign_gpu_percent": settings.resource_gpu_busy_percent + 1}
    assert "foreign GPU load" in gate(busy)


def test_contamination_asks_only_whether_something_changed():
    """Asymmetric with `gate` on purpose. A run clean at both ends but dirty in
    between is indistinguishable from a clean one, and claiming otherwise would
    be a guess dressed as a flag."""
    quiet = {"games_running": [], "foreign_gpu_percent": 1.0}
    loud = {"games_running": ["Warframe"], "foreign_gpu_percent": 80.0}
    assert contaminated(quiet, loud) is True
    assert contaminated(quiet, quiet) is False
    assert contaminated(loud, loud) is False  # gate would have refused it anyway
    assert contaminated(None, loud) is False  # unsensed is not contaminated


def test_form_params_reach_the_planner_under_the_names_it_reads():
    """The whole contract between the launch form and `plan_items`. A key spelled
    differently does not fail: it silently produces a default run whose stored
    params claim otherwise."""
    params = build_params(
        {"scenario": "ladder", "reps": "2", "predict": "64", "rungs": "1000, 2000 4096",
         "ladder_predict": "4", "warmup": "on"}
    )
    assert params == {
        "reps": 2, "predict": 64, "warmup": True,
        "rungs": [1000, 2000, 4096], "ladder_predict": 4,
    }
    items = plan_items("ladder", ["m"], params, _fixture())
    assert [item.rung for item in items] == [1000, 2000, 4096]
    assert {item.predict for item in items} == {4}


def test_an_unchecked_warmup_box_is_absent_not_false():
    """HTML omits an unchecked checkbox entirely, so `warmup` has to be read as
    presence. Reading it as a value would make the box impossible to clear."""
    assert build_params({"scenario": "quick"})["warmup"] is False
    assert build_params({"scenario": "quick", "warmup": "on"})["warmup"] is True


def test_a_sweep_variant_is_validated_before_the_run_row_exists():
    with pytest.raises(ValueError, match="not valid JSON"):
        build_params({"scenario": "sweep", "variants": "{oops"})
    with pytest.raises(ValueError, match="needs a label"):
        build_params({"scenario": "sweep", "variants": '[{"sections": {}}]'})
    with pytest.raises(ValueError, match="Unknown variant keys"):
        build_params({"scenario": "sweep", "variants": '[{"label": "a", "secitons": {}}]'})
    ok = build_params({"scenario": "sweep", "variants": '[{"label": "fit-on", "sections": {}}]'})
    assert ok["variants"] == [{"label": "fit-on", "sections": {}}]
