"""Check the golden set against the corpus before trusting any number from it.

    python -m eval.validate_golden_set

Three ways a hand-written label set silently lies:

- an `expected` entry names something the parser never produces, so the query is
  unanswerable and permanently counts as a miss
- a query tagged `jargon` uses vocabulary that is in fact present in the source,
  which makes it an easy keyword match rather than the hard case it claims to be
- two queries expect the same entity, quietly weighting it double

All three were caught by running this on the first draft.
"""

import shutil
import tempfile
from collections import Counter

from app.services.entity_service import dedupe
from app.services.parser_service import parse_codebase
from eval._common import key, load_golden_set, materialise_corpus

# Stopwords are excluded from the jargon check: "the" appearing in the source
# proves nothing. Short tokens go too, for the same reason.
_STOP = {
    "a", "an", "the", "of", "to", "and", "or", "with", "for", "in", "on",
    "at", "by", "that", "this", "is", "are", "be", "we", "it", "its",
    "one", "under", "every", "when", "into", "out", "up", "so", "not",
}


def build_corpus() -> list:
    """Parse exactly the files a clone of this repo would contain."""
    tmp = tempfile.mkdtemp()
    try:
        materialise_corpus(tmp)
        return dedupe(parse_codebase(tmp))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def distinctive_terms(query: str) -> list[str]:
    return [
        t
        for t in (w.strip(",.?'\"") for w in query.lower().split())
        if t not in _STOP and len(t) > 3
    ]


def main() -> int:
    data = load_golden_set()
    queries = data["queries"]
    corpus = build_corpus()
    known = {key(e.file_path, e.name) for e in corpus}
    source_text = "\n".join(e.source_code for e in corpus).lower()

    problems: list[str] = []

    for q in queries:
        for want in q["expected"]:
            if want not in known:
                problems.append(f"q{q['id']}: no such entity {want!r}")

    for q in queries:
        if q["category"] != "jargon":
            continue
        terms = distinctive_terms(q["query"])
        present = [t for t in terms if t in source_text]
        if terms and len(present) == len(terms):
            problems.append(
                f"q{q['id']}: tagged jargon but every distinctive term appears "
                f"in the source: {present}"
            )

    anchors = Counter(w for q in queries for w in q["expected"])
    for entity, n in anchors.items():
        if n > 2:
            problems.append(f"{entity} is expected by {n} queries")

    tests = sum(1 for e in corpus if "/tests/" in key(e.file_path, e.name))
    print(f"corpus            {len(corpus)} entities")
    print(f"test distractors  {tests} of {len(corpus)}")
    print(f"queries           {len(queries)}")
    print(f"by category       {dict(Counter(q['category'] for q in queries))}")
    print(f"distinct answers  {len(anchors)}")
    print()

    if problems:
        print(f"{len(problems)} problem(s):")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("golden set is consistent with the corpus")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
