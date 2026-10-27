"""Spoiler eval suite.

Offline tests check the fixtures and that the safety layers catch a model that leaks.
The `eval` tests run the real model (skipped unless EVAL_LLM_BASE_URL and
EVAL_LLM_MODEL are set):

    EVAL_LLM_BASE_URL=http://192.168.1.100:11434/v1 EVAL_LLM_MODEL=qwen2.5:14b \
        pytest -m eval tests/spoiler_eval
"""

import json
import os

import pytest
from conftest import FakeLLM

from wherewerewe.config import Settings
from wherewerewe.evaluation import find_leaks, fixture_session, load_fixtures, run_fixture
from wherewerewe.llm.recap import build_context

FIXTURES = load_fixtures()


def test_fixtures_load_and_are_self_consistent():
    assert {f.name for f in FIXTURES} >= {"breaking-bad-s1", "game-of-thrones-s1"}
    for fixture in FIXTURES:
        source = json.dumps(fixture.episodes)
        # A fixture whose own summaries trip a pattern would make the eval meaningless.
        assert find_leaks(source, fixture.forbidden) == [], fixture.name
        assert fixture.episodes[-1]["code"] == fixture.cutoff


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda f: f.name)
def test_assembled_context_contains_no_forbidden_events(fixture):
    settings = Settings()
    session, show, boundary = fixture_session(fixture, "fake-model")
    ctx = build_context(session, show, boundary, "deep", settings, 16384)
    assert find_leaks(ctx.text, fixture.forbidden) == []
    assert fixture.title not in ctx.text


LEAKY = {
    "breaking-bad-s1": (
        "Walt cooks meth with Jesse. Walt later meets Gus Fring. "
        "Tuco is dangerous. Soon Saul Goodman will help them."
    ),
    "game-of-thrones-s1": (
        "Ned is imprisoned. Robb marches south. "
        "Robb is soon proclaimed King in the North. "
        "Joffrey later orders Ned's execution."
    ),
}


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda f: f.name)
def test_safety_layers_strip_a_leaking_model(fixture):
    llm = FakeLLM(recap_text=LEAKY[fixture.name], unsupported=("King in the North", "execution"))
    report = run_fixture(fixture, Settings(), llm=llm, verify_llm=llm)
    assert report.draft_leaks, "the fake model should have leaked"
    assert report.passed, report.leaks
    assert report.removed_by_verifier == 2


live = pytest.mark.skipif(
    not (os.environ.get("EVAL_LLM_BASE_URL") and os.environ.get("EVAL_LLM_MODEL")),
    reason="set EVAL_LLM_BASE_URL and EVAL_LLM_MODEL to run against a real model",
)


@pytest.mark.eval
@live
@pytest.mark.parametrize("length", ["quick", "standard"])
@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda f: f.name)
def test_live_model_does_not_spoil(fixture, length, monkeypatch):
    settings = Settings(
        llm_base_url=os.environ["EVAL_LLM_BASE_URL"],
        llm_model=os.environ["EVAL_LLM_MODEL"],
        llm_api_key=os.environ.get("EVAL_LLM_API_KEY", ""),
        llm_context_tokens=int(os.environ.get("EVAL_LLM_CONTEXT", "16384")),
    )
    report = run_fixture(fixture, settings, length=length)
    print(f"\n[{fixture.name}/{length}] draft leaks: {report.draft_leaks}\n{report.recap}")
    assert report.passed, f"leaked: {report.leaks}\n\n{report.recap}"
    assert len(report.recap.split()) > 20, "recap is suspiciously short"
