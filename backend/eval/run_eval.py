"""Score a retrieval strategy against the golden set.

    python -m eval.run_eval PROJECT_ID                # dense baseline
    python -m eval.run_eval PROJECT_ID --strategy all # every strategy, compared

**On the metric names.** The roadmap says "recall@5". With label sets that mean
"any of these is a correct answer" rather than "all of these should come back",
textbook recall@5 — |relevant ∩ top5| / |relevant| — punishes a query for having
two acceptable answers and finding one. That is not the thing worth measuring
here, so the headline number is **success@5**: did an acceptable answer appear
in the top five. Strict recall@5 is printed beside it so the difference is
visible rather than hidden behind a naming choice.

**MRR** is the reciprocal rank of the first acceptable answer, 0 if none appears
in the retrieved window.

Only the embedding of each query is a paid call — 62 short strings. The corpus
must already be indexed; see index_corpus.py.
"""

import argparse
import asyncio
import json
import pathlib
import statistics
import sys
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.config import settings
from app.db.postgres import dispose_engine, session_scope
from app.models.entity import Entity
from app.models.project import Project
from app.services import entity_service, search_service
from eval._common import key, load_golden_set, matches

# How deep to retrieve. Metrics are computed at 1, 5 and 10 from this one list,
# so the window has to be at least the largest cutoff.
RETRIEVE_K = 10


@dataclass
class QueryOutcome:
    id: int
    query: str
    category: str
    expected: list[str]
    ranked: list[str] = field(default_factory=list)

    @property
    def first_hit_rank(self) -> int | None:
        """1-indexed rank of the first acceptable answer, or None."""
        for i, got in enumerate(self.ranked, start=1):
            if any(matches(got, want) for want in self.expected):
                return i
        return None

    def success_at(self, k: int) -> bool:
        rank = self.first_hit_rank
        return rank is not None and rank <= k

    def strict_recall_at(self, k: int) -> float:
        found = sum(
            1
            for want in self.expected
            if any(matches(got, want) for got in self.ranked[:k])
        )
        return found / len(self.expected)

    @property
    def reciprocal_rank(self) -> float:
        rank = self.first_hit_rank
        return 1.0 / rank if rank else 0.0


async def load_catalogue(project_id: uuid.UUID) -> dict[int, str]:
    """entity id -> `path::qualname`, so ranked ids can be scored against labels."""
    from sqlalchemy import select

    async with session_scope() as db:
        rows = await db.execute(
            select(Entity.id, Entity.file_path, Entity.qualname).where(
                Entity.project_id == project_id
            )
        )
        return {eid: key(path, name) for eid, path, name in rows.all()}


def _resolve(ids: list[int], catalogue: dict[int, str]) -> list[str]:
    """Ranked entity ids -> ranked `path::qualname`, dropping anything unknown."""
    return [catalogue[i] for i in ids if i in catalogue]


async def dense_strategy(
    project: Project, catalogue: dict[int, str], query: str
) -> list[str]:
    """Vectors only. The baseline, and what shipped before step 5."""
    ranked = await search_service.dense_candidates(project, query, depth=RETRIEVE_K)
    return _resolve([entity_id for entity_id, _ in ranked], catalogue)


async def keyword_strategy(
    project: Project, catalogue: dict[int, str], query: str
) -> list[str]:
    """Postgres full text only. Not a candidate to ship — it is the control.

    Without it there is no way to tell whether a hybrid gain came from the
    keyword half contributing something, or merely from RRF reshuffling the
    dense ranking.
    """
    async with session_scope() as db:
        ids = await entity_service.keyword_search(
            db, project.id, query, limit=RETRIEVE_K
        )
    return _resolve(ids, catalogue)


async def hybrid_strategy(
    project: Project, catalogue: dict[int, str], query: str
) -> list[str]:
    """Both halves fused by reciprocal rank — the production code path."""
    async with session_scope() as db:
        ranked = await search_service.hybrid_candidates(db, project, query)
    return _resolve([entity_id for entity_id, _ in ranked][:RETRIEVE_K], catalogue)


STRATEGIES = {
    "dense": dense_strategy,
    "keyword": keyword_strategy,
    "hybrid": hybrid_strategy,
}


def report(name: str, outcomes: list[QueryOutcome]) -> dict:
    n = len(outcomes)
    summary = {
        "strategy": name,
        "queries": n,
        "success@1": sum(o.success_at(1) for o in outcomes) / n,
        "success@5": sum(o.success_at(5) for o in outcomes) / n,
        "success@10": sum(o.success_at(10) for o in outcomes) / n,
        "strict_recall@5": statistics.fmean(o.strict_recall_at(5) for o in outcomes),
        "mrr": statistics.fmean(o.reciprocal_rank for o in outcomes),
    }

    print(f"\n=== {name} ===")
    print(f"  queries          {n}")
    print(f"  success@1        {summary['success@1']:.3f}")
    print(f"  success@5        {summary['success@5']:.3f}   <- headline")
    print(f"  success@10       {summary['success@10']:.3f}")
    print(f"  strict recall@5  {summary['strict_recall@5']:.3f}")
    print(f"  MRR              {summary['mrr']:.3f}")

    by_category = defaultdict(list)
    for o in outcomes:
        by_category[o.category].append(o)
    print("\n  by category:")
    for category, group in sorted(by_category.items()):
        hit = sum(o.success_at(5) for o in group)
        print(
            f"    {category:<9} success@5 {hit}/{len(group)} "
            f"({hit / len(group):.2f})  MRR "
            f"{statistics.fmean(o.reciprocal_rank for o in group):.3f}"
        )

    missed = [o for o in outcomes if not o.success_at(5)]
    if missed:
        print(f"\n  missed at 5 ({len(missed)}):")
        for o in missed:
            rank = o.first_hit_rank
            where = f"found at {rank}" if rank else "not in top 10"
            print(f"    q{o.id:<3} [{o.category}] {o.query!r} — {where}")
            print(f"          wanted {o.expected[0]}")
            print(f"          top1   {o.ranked[0] if o.ranked else '(nothing)'}")
    return summary


async def load_project(project_id: uuid.UUID) -> Project:
    from sqlalchemy import select

    async with session_scope() as db:
        project = (
            await db.execute(select(Project).where(Project.id == project_id))
        ).scalar_one_or_none()
        if project is None:
            raise SystemExit(f"no such project {project_id}")
        db.expunge(project)
        return project


async def evaluate(
    strategy_name: str, project: Project, catalogue: dict[int, str]
) -> list[QueryOutcome]:
    strategy = STRATEGIES[strategy_name]
    outcomes = []
    for q in load_golden_set()["queries"]:
        outcome = QueryOutcome(
            id=q["id"],
            query=q["query"],
            category=q["category"],
            expected=q["expected"],
        )
        outcome.ranked = await strategy(project, catalogue, q["query"])
        outcomes.append(outcome)
        print(".", end="", flush=True)
    print()
    return outcomes


def save_results(
    path: pathlib.Path,
    project: Project,
    catalogue: dict[int, str],
    summaries: list[dict],
    runs: dict[str, list[QueryOutcome]],
) -> None:
    """Write the run to disk, with enough provenance to be worth keeping.

    A bare pair of numbers ages badly: without the corpus size, the commit, the
    models and the retrieval constants, a later run that disagrees cannot be
    told apart from a regression. Per-query ranks go in too, so a future run can
    be diffed against this one rather than merely compared.
    """
    payload = {
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "corpus": {
            "project_id": str(project.id),
            "entities": len(catalogue),
            "indexed_commit": project.last_indexed_commit,
            "test_entities": sum(1 for k in catalogue.values() if "/tests/" in k),
        },
        "config": {
            "retrieve_k": RETRIEVE_K,
            "candidate_depth": search_service.CANDIDATE_DEPTH,
            "rrf_k": search_service.RRF_K,
            "llm_model": settings.openai_llm_model,
            "embedding_model": settings.openai_embedding_model,
            "embedding_dimensions": settings.openai_embedding_dimensions,
        },
        "golden_set": {
            "queries": len(load_golden_set()["queries"]),
            "labelled_by": load_golden_set().get("labelled_by"),
        },
        "summaries": summaries,
        "per_query": {
            name: [
                {
                    "id": o.id,
                    "query": o.query,
                    "category": o.category,
                    "first_hit_rank": o.first_hit_rank,
                    "top1": o.ranked[0] if o.ranked else None,
                }
                for o in outcomes
            ]
            for name, outcomes in runs.items()
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"\nwrote {path}")


def head_to_head(
    base_name: str,
    base: list[QueryOutcome],
    other_name: str,
    other: list[QueryOutcome],
) -> None:
    """Per-query wins and losses behind an aggregate delta.

    A three-point gain on 62 queries is two queries. Whether that is a real
    improvement or reshuffling depends entirely on whether it is "+2 and -0" or
    "+6 and -4", and the aggregate cannot tell you which.
    """
    by_id = {o.id: o for o in other}
    gained, lost, moved = [], [], []
    for b in base:
        o = by_id[b.id]
        if o.success_at(5) and not b.success_at(5):
            gained.append((b, o))
        elif b.success_at(5) and not o.success_at(5):
            lost.append((b, o))
        elif o.reciprocal_rank != b.reciprocal_rank:
            moved.append((b, o))

    print(f"\n  {other_name} vs {base_name}, per query:")
    print(f"    entered top 5   {len(gained)}")
    print(f"    fell out of it  {len(lost)}")
    print(f"    only reordered  {len(moved)}")
    for label, pairs in (("gained", gained), ("lost", lost)):
        for b, o in pairs:
            print(
                f"      [{label}] q{b.id} {b.query!r}: "
                f"rank {b.first_hit_rank or '-'} -> {o.first_hit_rank or '-'}"
            )


def compare(summaries: list[dict]) -> None:
    """Side-by-side table, with each strategy's delta against the first."""
    if len(summaries) < 2:
        return
    base = summaries[0]
    metrics = ["success@1", "success@5", "success@10", "strict_recall@5", "mrr"]

    print("\n=== comparison ===")
    header = f"  {'metric':<17}" + "".join(f"{s['strategy']:>12}" for s in summaries)
    print(header)
    print("  " + "-" * (len(header) - 2))
    for metric in metrics:
        row = f"  {metric:<17}"
        for s in summaries:
            cell = f"{s[metric]:.3f}"
            if s is not base:
                delta = s[metric] - base[metric]
                cell += f" ({delta:+.3f})" if delta else " (  =  )"
            row += f"{cell:>12}" if s is base else f"  {cell}"
        print(row)
    print(f"\n  deltas are against '{base['strategy']}'")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("project_id")
    ap.add_argument(
        "--strategy",
        default="all",
        help=f"one of {', '.join(STRATEGIES)}, or 'all' (default)",
    )
    ap.add_argument(
        "--save",
        metavar="PATH",
        help="write the run, with provenance, to a JSON file",
    )
    args = ap.parse_args()

    pid = uuid.UUID(args.project_id)
    try:
        catalogue = await load_catalogue(pid)
        if not catalogue:
            print(f"project {pid} has no indexed entities — run index_corpus first")
            sys.exit(1)
        project = await load_project(pid)
        print(f"corpus: {len(catalogue)} indexed entities")

        names = list(STRATEGIES) if args.strategy == "all" else [args.strategy]
        summaries, runs = [], {}
        for name in names:
            outcomes = await evaluate(name, project, catalogue)
            runs[name] = outcomes
            summaries.append(report(name, outcomes))

        compare(summaries)
        base = names[0]
        for name in names[1:]:
            head_to_head(base, runs[base], name, runs[name])

        if args.save:
            save_results(
                pathlib.Path(args.save), project, catalogue, summaries, runs
            )
    finally:
        await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
