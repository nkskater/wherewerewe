from conftest import FakeLLM

from wherewerewe.llm.verify import parse_blocks, split_sentences, unknown_names, verify_recap

SOURCE = """### S01E07
What happens:
- Walt and Jesse steal methylamine from a warehouse.
- Tuco beats No-Doze to death.
Characters:
- Walter White: supplies Tuco
- Jesse Pinkman: Walt's partner"""


def test_split_sentences_respects_abbreviations_and_quotes():
    text = 'Mr. White meets Dr. Smith. "Run!" Jesse yells. Then J. Edgar leaves? Yes.'
    assert split_sentences(text) == [
        "Mr. White meets Dr. Smith.",
        '"Run!"',
        "Jesse yells.",
        "Then J. Edgar leaves?",
        "Yes.",
    ]


def test_parse_blocks_keeps_structure():
    blocks = parse_blocks("## Season 1\nWalt cooks. Jesse sells.\n\n- One thing\n- Another")
    assert [b.kind for b in blocks] == ["heading", "prose", "bullets"]
    assert blocks[1].items == ["Walt cooks.", "Jesse sells."]


def test_unknown_names_flags_mid_sentence_names_not_in_source():
    sentences = [
        "Walt meets with Gus at the restaurant.",
        "Gus is calm.",
        "Meanwhile Jesse worries.",
        "Saul helps them.",
        "Shocked, Walt drives home.",
        '"Never again," Jesse says.',
    ]
    hard, hints = unknown_names(sentences, SOURCE)
    assert set(hard) == {0, 1, 3}  # Gus mid-sentence and at the start; Saul at the start
    assert "Gus" in hard[0] and "Saul" in hard[3]
    assert 2 not in hard and 2 not in hints  # "Meanwhile" is a common opener
    assert 4 not in hard and 4 in hints  # "Shocked" is shaped like a word; the LLM decides
    assert 5 not in hard


def test_verify_removes_flagged_and_llm_rejected_sentences():
    llm = FakeLLM(unsupported=("dies",))
    recap = (
        "## Season 1\nWalt and Jesse steal methylamine. Walt meets Gus Fring. "
        "Tuco dies in the desert.\n\n## Season 2\nHank investigates Heisenberg."
    )
    result = verify_recap(llm, SOURCE, recap)
    assert result.text == "## Season 1\n\nWalt and Jesse steal methylamine."
    reasons = {r["sentence"]: r["reason"] for r in result.removed}
    assert "never appears" in reasons["Walt meets Gus Fring."]
    assert reasons["Tuco dies in the desert."] == "not in source"
    assert result.llm_checked
    # Sentences hard-flagged by the name check are not sent to the LLM.
    verify_call = next(c for c in llm.calls if c["kind"] == "verify")
    assert "Gus Fring" not in verify_call["messages"][-1]["content"]


def test_verify_without_llm_still_runs_name_check():
    result = verify_recap(None, SOURCE, "Jesse sells. Then Jesse calls Saul.")
    assert result.text == "Jesse sells."
    assert not result.llm_checked


class SilentVerifier(FakeLLM):
    def chat(self, messages, *, schema=None, max_tokens=None):
        return '{"results": []}'


def test_verifier_that_skips_everything_is_treated_as_failed():
    result = verify_recap(SilentVerifier(), SOURCE, "Walt cooks. Jesse sells. Tuco rages.")
    assert not result.llm_checked
    assert result.text == "Walt cooks. Jesse sells. Tuco rages."
    assert "skipped" in result.error
