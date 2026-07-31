# Retrieval evaluation results

Machine-readable runs live in `results/`. This file is the narrative: what was
measured, what it means, and what not to conclude from it.

## Run 1 — 2026-07-31, CodeLens on itself

**Corpus.** Every git-tracked `.py` file in this repository at commit
`cea2b61`, indexed with the production pipeline. **495 entities, of which 351
are tests.** That ratio is the point: test names deliberately echo the
implementations they cover, so `test_removes_only_entities_the_run_did_not_see`
competes with `delete_missing`. Random chance at success@5 is ~1%.

**Golden set.** 62 queries, labelled by Claude Opus 5 **from source code only**
— the pipeline's generated descriptions were never consulted, because writing
queries against the text being retrieved would measure paraphrase overlap rather
than retrieval. Labels name implementations; tests count as misses even when
they concern the same behaviour.

**Config.** gpt-4o-mini descriptions, text-embedding-3-small at 1024 dims,
retrieval depth 30, RRF k=60, metrics at cutoff 10.

### Numbers

| | dense | keyword | hybrid |
|---|---|---|---|
| success@1 | 0.500 | 0.161 | **0.532** |
| success@5 | 0.758 | 0.194 | **0.790** |
| success@10 | 0.806 | 0.194 | **0.839** |
| strict recall@5 | 0.742 | 0.177 | **0.774** |
| MRR | 0.592 | 0.173 | **0.621** |

**hybrid vs dense, per query: 2 gained, 0 lost, 7 reordered.**

### Reading these honestly

**The headline is success@5, not recall@5.** Labels mean "any of these is
correct", not "return all of these". Textbook recall would punish a query for
having two acceptable answers and finding one, so both are reported and the
headline is the one that matches what a user experiences.

**+3.2 points is two queries.** On 62 queries that is inside noise, and an
aggregate cannot distinguish "+2 and −0" from "+6 and −4". The per-query
breakdown is what makes hybrid defensible: it is a *strict* improvement, and the
two queries it gained (q36, q38) are exactly the two the keyword half retrieved
on its own. The gain is traceable to the keyword retriever contributing, not to
RRF reshuffling the dense ranking.

**Keyword alone is bad and still worth fusing.** At 0.194 it loses 37 queries
dense wins, because natural-language queries share little vocabulary with code.
But it is immovable on exact identifiers, which is precisely where embeddings
are vague. That is the whole argument for hybrid, and it is why `keyword` is
evaluated at all — as a control, not a candidate.

### The dominant failure mode

**13 of the 15 dense misses have a test entity at rank 1.** The LLM's
description of a test often states the behaviour *more* explicitly than the
implementation's docstring does, so it embeds closer to a plain-English query.

This is the largest lever left, and it is a ranking prior rather than a
retrieval problem. **It also cannot be measured honestly with this golden set.**
The relevance policy here declares tests non-relevant, so "deprioritise test
files" would be optimising directly against that choice and would report a
large, partly circular gain. It is defensible as a product decision — someone
searching a codebase wants the function, not its test — but any number attached
to it has to carry that caveat, and this eval is not the instrument to produce
one.

### Known limits of this run

- One corpus, and it is the codebase under test. A second corpus on an
  unfamiliar repository is the obvious next measurement.
- 62 queries is small. Differences under ~5 points should not be trusted without
  the per-query breakdown.
- Labels are machine-generated. Spot-auditing a sample would convert "assumed
  correct" into "sampled and verified at N%"; that has not been done.
- Every query has at least one correct answer in the corpus. Queries that
  *should* return nothing are not tested at all.

## Reproducing

```bash
cd backend
python -m eval.validate_golden_set              # free
python -m eval.index_corpus                     # ~$0.04, real API calls
python -m eval.run_eval PROJECT_ID --save eval/results/NAME.json
```

Re-indexing an unchanged corpus is free — the content-hash diff from step 4
makes it zero LLM and zero embedding calls.
