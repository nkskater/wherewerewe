# Spoiler safety

A recap tool that spoils the show is worse than no recap tool. There are two very different
ways a recap can leak, and they need different defences.

## 1. Boundary leakage: material from episodes you haven't seen

This one is solved **structurally**, not by asking the model nicely.

- **Episode summaries are isolated.** Each episode is summarized from its own dialogue and its
  own synopsis, and nothing else. A summary of S02E04 cannot mention S02E05, because the model
  never saw S02E05 while writing it. That makes the cache safe to share between viewers at
  different positions.
- **Assembly filters by position.** A recap for a viewer at S02E04 loads summaries for
  `(season, episode) <= (2, 4)` and nothing more. Season rollups are only used for seasons
  that end before the boundary season.
- **Requests past your position are refused.** The API rejects a recap or character lookup
  for an episode after the viewer's boundary.
- **The character ledger is time-aware.** Character states are stored per episode, and a
  lookup reads only states at or before the boundary. That includes *names*: if episode 9
  reveals that "the Driver" is called Bob, a viewer at episode 5 still sees "the Driver".
  The internal record that links the two is never shown.
- **The boundary is conservative.** An episode counts only when finished (90% by default).
  Specials have no reliable position in the order, so they never move the boundary.
  Rewatching an early episode doesn't move it back, and when a playback-start event comes
  from a rewatch, the push recap stops before the episode being played. Multi-episode files
  count as fully watched, since the viewer has the whole file.

These guarantees are covered by tests (`tests/test_pipeline.py`, `tests/test_api.py`), which
assert, for example, that the exact prompt sent to the model contains nothing past the
boundary.

## 2. Model-knowledge leakage: things the model already knows about the show

This is the hard part. Llama, Qwen and friends have read the Wikipedia plot summaries of
*Breaking Bad*. Given a recap job about "Walter White" and "Jesse Pinkman", a model may
"helpfully" add that Gus Fring is waiting in season 2. There is no structural fix, so the
defences are layered:

1. **Grounding prompt.** Every prompt tells the model to use only the provided material,
   never outside knowledge, and to frame unresolved threads as open questions rather than
   hints.
2. **No show title.** The recap and summary prompts refer to "the series". Character names
   remain a strong cue, but the title is the strongest one and it is never needed. Set
   `RECAP_HIDE_TITLE=false` to measure the difference with the eval suite.
3. **Deterministic name check.** Any recap sentence containing a capitalized word that
   appears nowhere in the source material is removed before the LLM check runs. A name the
   model brought in from its own memory ("Gus", "Saul Goodman") is exactly that kind of word.
   At the start of a sentence every word is capitalized, so there a word only counts as a
   name if it isn't a common sentence opener or shaped like an ordinary word. Borderline
   cases go to the verifier as hints.
4. **LLM verification pass.** A second call (temperature 0, optionally a different model via
   `VERIFY_MODEL`) checks every remaining sentence against the source material and marks
   unsupported claims, including predictions. Unsupported sentences are removed. If more than
   40% are removed, the recap is regenerated once with a stricter instruction, and whichever
   version had the smaller share of sentences removed is used. If the verifier fails or skips most sentences, the recap is kept
   with the name check applied and marked unverified in the UI.
5. **Spoiler eval set.** `backend/tests/spoiler_eval/fixtures/` holds known shows with a
   cutoff, hand-written summaries up to the cutoff, and regexes for well-known later events
   (deaths, reveals, characters introduced later). The real assembly, generation and
   verification code runs at the cutoff. The test fails if any forbidden event appears in
   the final recap, and it reports draft leaks separately so you can see how much the
   verifier is catching. The suite runs in CI against a small Ollama model
   (`.github/workflows/spoiler-eval.yml`) and on demand with `wherewerewe eval` against
   whatever model you have configured.

## Known limits

- The name check can't catch a leak that uses only names already in the source (for example
  "Hank dies"). That is the verifier's job, so verifier quality matters. A 14B-class model
  is a reasonable floor.
- The name check occasionally removes a harmless sentence, such as one using a nickname
  that the summaries never use. Removal is the safe failure.
- The episode summaries themselves are not verified (that would double the indexing cost).
  They are generated from a single episode with the same grounding prompt, and they never
  include the show title.
