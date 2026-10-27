"""Verification pass: drop recap sentences the source summaries don't support.

Two layers:
1. A deterministic check: a capitalized word that never appears in the source
   material is almost always a name the model brought in from its own knowledge of
   the show. Those sentences are removed outright. At the start of a sentence every
   word is capitalized, so there a word only counts as a name if it isn't a common
   sentence opener or shaped like an ordinary word (-ly, -ing, -ed...).
2. An LLM fact-check of every sentence against the source material.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from . import prompts
from .client import LLM, LLMError, chat_json

log = logging.getLogger(__name__)

_ABBREVIATIONS = {
    "Mr",
    "Mrs",
    "Ms",
    "Dr",
    "St",
    "Jr",
    "Sr",
    "Lt",
    "Sgt",
    "Capt",
    "Gen",
    "Col",
    "Det",
    "Prof",
    "vs",
    "etc",
    "U.S",
    "e.g",
    "i.e",
}
_BOUNDARY = re.compile(r"[.!?][\"”’)\]]*(?=\s+[\"“(\[]?[A-Z0-9])")
_WORD = re.compile(r"[A-Za-z][A-Za-z'’\-]*")
_ALWAYS_OK = {
    "i",
    "i'm",
    "i've",
    "i'll",
    "i'd",
    "season",
    "episode",
    "episodes",
    "seasons",
    "previously",
    "god",
    "ok",
    "okay",
    "tv",
    "mr",
    "mrs",
    "ms",
    "dr",
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
    "christmas",
    "thanksgiving",
    "where",
    "things",
    "stand",
    "american",
    "americans",
    "english",
    "america",
    "usa",
}
# Common ways a recap sentence starts. Unknown sentence-initial words outside this list
# are treated as names.
_STARTERS = set(
    """
a about above across after afterward afterwards again against all almost alone along already
also although always amid among an and another any anyone anything around as at back based
because before behind being below beneath beside besides between beyond both but by can
clearly convinced could curious dead deep desperate despite determined during each earlier
either elsewhere even eventually ever every everyone everything except facing far few finally
first following for from further furious given gradually he her here hers herself him himself
his how however if in inside instead into it its itself just last later least left less like
little long many meanwhile more moreover most much must my naturally neither never
nevertheless next no nobody none nor not nothing now nowhere of off often on once one only
onto or other others otherwise our out outside over overall part perhaps privately publicly
quickly rather really recently refusing right same secretly several she should shortly
similarly since so some someone something sometime soon still such suddenly that the their
theirs them themselves then there therefore these they this those though three through
throughout thus to today together tomorrow tonight too toward towards two under unable unaware
unbeknownst unlike until up upon us using very was we well were what whatever when whenever
where whereas whether which while who whoever whom whose why with within without worse worst
would yet you your
""".split()
)
_ORDINARY_SUFFIX = re.compile(
    r"(ly|ing|ed|ful|less|ous|ive|ness|ment|tion|tions|sion|ions|"
    r"ies|able|ible|ence|ance|est)$"
)


def split_sentences(paragraph: str) -> list[str]:
    out, start = [], 0
    for match in _BOUNDARY.finditer(paragraph):
        prev = re.search(r"(\S+)$", paragraph[start : match.start()])
        if match.group(0).startswith(".") and prev:
            word = prev.group(1)
            if word in _ABBREVIATIONS or re.fullmatch(r"[A-Z]", word):
                continue
        out.append(paragraph[start : match.end()].strip())
        start = match.end()
    tail = paragraph[start:].strip()
    if tail:
        out.append(tail)
    return [s for s in out if s]


@dataclass
class Block:
    kind: str  # heading, prose, bullets
    items: list[str]


def parse_blocks(text: str) -> list[Block]:
    blocks: list[Block] = []
    for raw in re.split(r"\n\s*\n", text.strip()):
        prose: list[str] = []
        for line in (ln.strip() for ln in raw.splitlines()):
            if not line:
                continue
            is_heading = line.startswith("#")
            is_bullet = bool(re.match(r"^([-*•]|\d+[.)])\s+", line))
            if (is_heading or is_bullet) and prose:
                blocks.append(Block("prose", split_sentences(" ".join(prose))))
                prose = []
            if is_heading:
                blocks.append(Block("heading", [line]))
            elif is_bullet:
                if blocks and blocks[-1].kind == "bullets":
                    blocks[-1].items.append(line)
                else:
                    blocks.append(Block("bullets", [line]))
            else:
                prose.append(line)
        if prose:
            blocks.append(Block("prose", split_sentences(" ".join(prose))))
    return blocks


def render_blocks(blocks: list[Block]) -> str:
    parts: list[str] = []
    for i, block in enumerate(blocks):
        if block.kind == "heading":
            nxt = blocks[i + 1] if i + 1 < len(blocks) else None
            if nxt is None or nxt.kind == "heading" or not nxt.items:
                continue  # heading whose section was emptied
            parts.append(block.items[0])
        elif block.items:
            parts.append(("\n" if block.kind == "bullets" else " ").join(block.items))
    return "\n\n".join(parts).strip()


def _vocab(source: str) -> set[str]:
    words = set()
    for w in _WORD.findall(source):
        w = w.lower().replace("’", "'")
        words.add(w)
        words.add(re.sub(r"'s$", "", w))
        words.update(part for part in w.split("-") if part)
    return words


def _name_tokens(sentence: str) -> list[tuple[str, bool]]:
    """(word, sentence_initial) for each word; words after an opening quote or colon count as
    sentence-initial since they are capitalized for grammatical reasons."""
    text = re.sub(r"^\s*([-*•]|\d+[.)])\s+", "", sentence)
    tokens = []
    for m in _WORD.finditer(text):
        before = text[: m.start()].rstrip()
        initial = before == "" or before[-1] in '"“:—(' or before.endswith("--")
        tokens.append((m.group(0), initial))
    return tokens


def unknown_names(sentences: list[str], source: str) -> tuple[dict[int, str], dict[int, str]]:
    """Return (hard flags, soft hints) keyed by sentence index."""
    vocab = _vocab(source)

    def unknown(word: str) -> bool:
        base = re.sub(r"['’]s$", "", word).lower()
        parts = [p for p in base.split("-") if p]
        return base not in vocab and base not in _ALWAYS_OK and not all(p in vocab for p in parts)

    flagged_words: set[str] = set()
    hard: dict[int, list[str]] = {}
    for i, sentence in enumerate(sentences):
        for word, initial in _name_tokens(sentence):
            if not initial and word[0].isupper() and unknown(word):
                hard.setdefault(i, []).append(word)
                flagged_words.add(word.lower())
    hints: dict[int, str] = {}
    for i, sentence in enumerate(sentences):
        for word, initial in _name_tokens(sentence):
            if not (initial and word[0].isupper() and unknown(word)):
                continue
            lower = word.lower()
            if lower in _STARTERS and lower not in flagged_words:
                continue
            ordinary = len(lower) >= 6 and bool(_ORDINARY_SUFFIX.search(lower))
            if lower in flagged_words or not ordinary:
                hard.setdefault(i, []).append(word)
            else:
                hints[i] = f"'{word}' does not appear in the source material"
    return (
        {
            i: "mentions "
            + ", ".join(f"'{w}'" for w in dict.fromkeys(ws))
            + " which never appears in the source material"
            for i, ws in hard.items()
        },
        hints,
    )


@dataclass
class VerificationResult:
    text: str
    checked: int
    removed: list[dict] = field(default_factory=list)
    llm_checked: bool = True
    error: str | None = None

    @property
    def removed_ratio(self) -> float:
        return len(self.removed) / self.checked if self.checked else 0.0

    def as_json(self) -> dict:
        return {
            "checked": self.checked,
            "removed": self.removed,
            "llm_checked": self.llm_checked,
            "error": self.error,
            "prompt_version": prompts.VERIFY_VERSION,
        }


def verify_recap(
    llm: LLM | None, source: str, recap: str, batch_size: int = 40
) -> VerificationResult:
    blocks = parse_blocks(recap)
    refs: list[tuple[int, int]] = []  # (block index, item index) for each checkable sentence
    sentences: list[str] = []
    for bi, block in enumerate(blocks):
        if block.kind != "heading":
            for ii, item in enumerate(block.items):
                refs.append((bi, ii))
                sentences.append(item)

    hard, hints = unknown_names(sentences, source)
    verdicts: dict[int, tuple[bool, str]] = {i: (False, r) for i, r in hard.items()}
    result = VerificationResult(text=recap, checked=len(sentences))

    if llm is not None:
        pending = [i for i in range(len(sentences)) if i not in hard]
        missing_total = 0
        try:
            for start in range(0, len(pending), batch_size):
                batch = pending[start : start + batch_size]
                out = chat_json(
                    llm,
                    [
                        {"role": "system", "content": prompts.VERIFY_SYSTEM},
                        {
                            "role": "user",
                            "content": prompts.verify_user(
                                source,
                                [(i + 1, sentences[i]) for i in batch],
                                {i + 1: hints[i] for i in batch if i in hints},
                            ),
                        },
                    ],
                    prompts.VerifyOut,
                    max_tokens=min(4000, 80 * len(batch) + 200),
                )
                got = {v.id - 1: v for v in out.results}
                for i in batch:
                    if i in got:
                        verdicts[i] = (got[i].supported, got[i].reason)
                    else:
                        missing_total += 1
                        verdicts[i] = (False, "verifier returned no verdict")
            if pending and missing_total > len(pending) / 2:
                raise LLMError(f"verifier skipped {missing_total} of {len(pending)} sentences")
        except LLMError as exc:
            log.warning("LLM verification failed, keeping deterministic checks only: %s", exc)
            result.llm_checked = False
            result.error = str(exc)
            verdicts = {i: (False, r) for i, r in hard.items()}
    else:
        result.llm_checked = False

    drop = {i for i, (ok, _) in verdicts.items() if not ok}
    for i in sorted(drop):
        result.removed.append({"sentence": sentences[i], "reason": verdicts[i][1]})
    dropped_refs = {refs[i] for i in drop}
    for bi, block in enumerate(blocks):
        block.items = [item for ii, item in enumerate(block.items) if (bi, ii) not in dropped_refs]
    result.text = render_blocks(blocks)
    return result
