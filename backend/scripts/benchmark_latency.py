"""End-to-end latency benchmark for the search endpoint.

Measures `POST /api/v1/projects/{id}/search` over the wire, which is the only
way to capture what a user actually waits for: FastAPI routing, the
`get_current_user` lookup, the query embedding (one OpenAI call), the Pinecone
query, the Postgres full-text query, RRF fusion, and the hydrate SELECT that
turns fused ids back into rows.

Run from the backend/ directory, against a running API:

    cr_venv\\Scripts\\python.exe scripts\\benchmark_latency.py \\
        --project-id <uuid> --email you@example.com --password ...

If the project's owner has no password you can use (the eval corpus owner is
created with a random one), mint a token locally instead — `get_current_user`
only needs a signed `sub`:

    ... --project-id <uuid> --mint-token retrieval-eval@codelens.local

**Cost.** Every search embeds its query, so N requests are N
`text-embedding-3-small` calls. At ~9 tokens per query and $0.02/1M tokens, 100
requests is about $0.00002 — but it is not zero, and it is a real call to a paid
API, not a stub.

**What is deliberately not measured.** Requests are issued one at a time. This
is a latency benchmark, not a throughput or saturation benchmark: p95 under
concurrency is a different number and needs a different instrument. Cold-start
effects are excluded via `--warmup`, which is reported rather than hidden.
"""

import argparse
import asyncio
import json
import pathlib
import random
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402

BACKEND = pathlib.Path(__file__).resolve().parents[1]
GOLDEN_SET = BACKEND / "eval" / "golden_set.json"

# Queries beyond the golden set. The golden set is 62 labelled queries written
# against this repository; a latency run wants a hundred or more, and padding by
# repeating the same string would measure a warm path rather than a realistic
# mix. These are the same shape — plain English, no identifiers — and are not
# labelled, because nothing here scores relevance.
EXTRA_QUERIES = [
    "retry a failed operation with exponential backoff",
    "validate a URL before using it",
    "convert a datetime to an ISO 8601 string",
    "read configuration from environment variables",
    "open a database connection and close it afterwards",
    "serialize an object to JSON",
    "paginate a list of results",
    "cache the result of an expensive computation",
    "send an HTTP request with a timeout",
    "write a temporary file and clean it up",
    "compare two dictionaries and report the differences",
    "recursively walk a directory tree",
    "split a large list into fixed-size chunks",
    "run several async tasks with a concurrency limit",
    "raise a helpful error when a required field is missing",
    "normalise a connection string before use",
    "generate a unique identifier for a record",
    "measure how long a function takes to run",
    "log a one-line summary when a job finishes",
    "skip files that cannot be parsed",
    "count rows belonging to a single tenant",
    "batch items before sending them to an external API",
    "check whether a background worker is still alive",
    "mark a task as finished and record the timestamp",
    "roll back a transaction when something fails",
    "strip query parameters a driver does not understand",
    "turn a ranked list of ids into full records",
    "return a 404 when the requested object does not exist",
    "reject a request that is missing a bearer token",
    "clean up resources even if an exception is raised",
    "find the entry point of the application",
    "define the shape of an API response",
    "apply a database schema migration",
    "detect whether a host is behind a connection pooler",
    "extract the function signature from source code",
    "build the text that gets sent to the language model",
    "decide which items still need processing",
    "delete records that no longer exist upstream",
    "store a vector alongside its identifier",
    "translate a domain error into an HTTP status code",
    "load settings from a dotenv file",
    "guard an endpoint so only the owner can call it",
    "produce a stable hash of a block of text",
    "shorten a string for display without breaking words",
    "queue work to be done later instead of inline",
    "report progress while a long job is running",
    "pick the top results after merging two ranked lists",
    "make a shallow clone of a git repository",
]


def load_query_pool() -> tuple[list[str], int]:
    """Every distinct query available, and how many came from the golden set."""
    golden: list[str] = []
    if GOLDEN_SET.exists():
        data = json.loads(GOLDEN_SET.read_text(encoding="utf-8"))
        golden = [q["query"] for q in data.get("queries", [])]
    pool = list(dict.fromkeys(golden + EXTRA_QUERIES))
    return pool, len(golden)


def build_queries(n: int, seed: int) -> tuple[list[str], int, int]:
    """`n` queries, shuffled deterministically. Cycles only if the pool is smaller."""
    pool, from_golden = load_query_pool()
    rng = random.Random(seed)
    shuffled = pool[:]
    rng.shuffle(shuffled)
    queries = [shuffled[i % len(shuffled)] for i in range(n)]
    return queries, len(pool), from_golden


def percentile(sorted_values: list[float], p: float) -> float:
    """Nearest-rank percentile.

    Deliberately not `statistics.quantiles`, which interpolates: with 100
    samples an interpolated p99 is a blend of the two slowest requests rather
    than a request that actually happened. Nearest rank always names a real
    observation, which is the honest thing to put on a resume.
    """
    if not sorted_values:
        return float("nan")
    rank = max(1, min(len(sorted_values), int(-(-p * len(sorted_values) // 100))))
    return sorted_values[rank - 1]


@dataclass
class Sample:
    query: str
    ms: float
    status: int
    results: int


@dataclass
class StageBreakdown:
    embed: list[float] = field(default_factory=list)
    pinecone: list[float] = field(default_factory=list)
    db_connect: list[float] = field(default_factory=list)
    keyword: list[float] = field(default_factory=list)
    fuse_hydrate: list[float] = field(default_factory=list)


def mint_token(email: str) -> str:
    """Sign a token locally instead of logging in.

    `get_current_user` decodes the bearer token, reads `sub`, and looks the user
    up — so a locally signed token for an existing email is as valid as one from
    `/login`, without needing that user's password. Requires the same
    `JWT_SECRET` the API is running with.
    """
    from app.core.security import create_access_token

    return create_access_token({"sub": email})


def login(client: httpx.Client, email: str, password: str) -> str:
    """Log in, signing up first if the account does not exist yet."""
    r = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    if r.status_code == 200:
        return r.json()["access_token"]
    signup = client.post(
        "/api/v1/auth/signup", json={"email": email, "password": password}
    )
    if signup.status_code not in (200, 201):
        raise SystemExit(
            f"login failed ({r.status_code}) and signup failed "
            f"({signup.status_code}): {signup.text[:200]}"
        )
    r = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    r.raise_for_status()
    return r.json()["access_token"]


def resolve_project(client: httpx.Client, headers: dict, project_id: str | None) -> str:
    """The project to search, defaulting to the caller's first ready one."""
    if project_id:
        r = client.get(f"/api/v1/projects/{project_id}", headers=headers)
        if r.status_code != 200:
            raise SystemExit(f"project {project_id}: HTTP {r.status_code} {r.text[:200]}")
        status = r.json().get("status")
        if status != "ready":
            raise SystemExit(f"project {project_id} is '{status}', not 'ready'")
        return project_id

    r = client.get("/api/v1/projects/", headers=headers)
    r.raise_for_status()
    ready = [p for p in r.json() if p.get("status") == "ready"]
    if not ready:
        raise SystemExit("no ready project for this user; pass --project-id")
    print(f"using project {ready[0]['id']} ({ready[0].get('name')})")
    return ready[0]["id"]


def run_http(
    base_url: str,
    token: str,
    project_id: str,
    queries: list[str],
    top_k: int,
    warmup: int,
    timeout: float,
) -> list[Sample]:
    headers = {"Authorization": f"Bearer {token}"}
    url = f"/api/v1/projects/{project_id}/search"
    samples: list[Sample] = []

    with httpx.Client(base_url=base_url, timeout=timeout) as client:
        for i, query in enumerate(queries[:warmup]):
            client.post(url, headers=headers, json={"query": query, "top_k": top_k})
            print(f"\r  warmup {i + 1}/{warmup}", end="", flush=True)
        if warmup:
            print()

        for i, query in enumerate(queries, start=1):
            started = time.perf_counter()
            r = client.post(url, headers=headers, json={"query": query, "top_k": top_k})
            elapsed_ms = (time.perf_counter() - started) * 1000
            results = 0
            if r.status_code == 200:
                results = len(r.json().get("results", []))
            samples.append(Sample(query, elapsed_ms, r.status_code, results))
            if i % 10 == 0 or i == len(queries):
                print(f"\r  {i}/{len(queries)} requests", end="", flush=True)
    print()
    return samples


async def run_stage_breakdown(
    project_id: str, queries: list[str], top_k: int
) -> StageBreakdown:
    """Time the pipeline's four stages in-process, to attribute the total.

    This bypasses HTTP on purpose: the point is not another end-to-end figure
    but where the end-to-end figure goes. It calls the same service functions
    the route does, in the same order, so the sum tracks the request time minus
    framework and network overhead.
    """
    import uuid

    from sqlalchemy import select
    from sqlalchemy import text as sa_text

    from app.db.postgres import dispose_engine, session_scope
    from app.models.project import Project
    from app.services import search_service
    from app.services.embedding_service import embed_query
    from app.services.entity_service import get_by_ids, keyword_search
    from app.services.pinecone_service import query_vectors

    out = StageBreakdown()
    pid = uuid.UUID(project_id)
    try:
        async with session_scope() as db:
            project = (
                await db.execute(select(Project).where(Project.id == pid))
            ).scalar_one()
            db.expunge(project)

        for query in queries:
            t0 = time.perf_counter()
            embedding = await embed_query(query)
            t1 = time.perf_counter()
            matches = await query_vectors(
                namespace=project.pinecone_namespace,
                embedding=embedding,
                top_k=search_service.CANDIDATE_DEPTH,
            )
            t2 = time.perf_counter()
            async with session_scope() as db:
                # Establishing the connection is timed separately and on
                # purpose. Against a pooled Neon endpoint the engine uses
                # NullPool, so every request pays a fresh TCP + TLS handshake to
                # the database region — and folding that into the first query
                # makes Postgres look slow when what is slow is connecting to
                # it. The route pays this once per request too, via get_db.
                await db.execute(sa_text("SELECT 1"))
                t2b = time.perf_counter()
                keyword = await keyword_search(
                    db, project.id, query, limit=search_service.CANDIDATE_DEPTH
                )
                t3 = time.perf_counter()
                dense = []
                for match in matches:
                    try:
                        dense.append(int(match["id"]))
                    except (KeyError, TypeError, ValueError):
                        continue
                fused = search_service.reciprocal_rank_fusion([dense, keyword])[:top_k]
                await get_by_ids(db, project.id, [eid for eid, _ in fused])
                t4 = time.perf_counter()

            out.embed.append((t1 - t0) * 1000)
            out.pinecone.append((t2 - t1) * 1000)
            out.db_connect.append((t2b - t2) * 1000)
            out.keyword.append((t3 - t2b) * 1000)
            out.fuse_hydrate.append((t4 - t3) * 1000)
    finally:
        await dispose_engine()
    return out


def summarise(samples: list[Sample]) -> dict:
    ok = [s for s in samples if s.status == 200]
    latencies = sorted(s.ms for s in ok)
    if not latencies:
        raise SystemExit("every request failed; nothing to summarise")
    return {
        "requests": len(samples),
        "succeeded": len(ok),
        "failed": len(samples) - len(ok),
        "min_ms": latencies[0],
        "p50_ms": percentile(latencies, 50),
        "p90_ms": percentile(latencies, 90),
        "p95_ms": percentile(latencies, 95),
        "p99_ms": percentile(latencies, 99),
        "max_ms": latencies[-1],
        "mean_ms": statistics.fmean(latencies),
        "stdev_ms": statistics.stdev(latencies) if len(latencies) > 1 else 0.0,
        "mean_results": statistics.fmean(s.results for s in ok),
    }


def report(summary: dict, samples: list[Sample], stages: StageBreakdown | None) -> None:
    print("\n=== search latency, end to end ===")
    print(f"  requests         {summary['requests']} ({summary['failed']} failed)")
    print(f"  results/query    {summary['mean_results']:.1f} mean")
    print()
    for label, key in (
        ("min", "min_ms"),
        ("p50", "p50_ms"),
        ("p90", "p90_ms"),
        ("p95", "p95_ms"),
        ("p99", "p99_ms"),
        ("max", "max_ms"),
    ):
        marker = "   <- headline" if key == "p95_ms" else ""
        print(f"  {label:<16} {summary[key]:8.1f} ms{marker}")
    print(f"\n  mean             {summary['mean_ms']:8.1f} ms")
    print(f"  stdev            {summary['stdev_ms']:8.1f} ms")

    if summary["failed"]:
        codes = {}
        for s in samples:
            if s.status != 200:
                codes[s.status] = codes.get(s.status, 0) + 1
        print(f"\n  failures by status: {codes}")

    slowest = sorted((s for s in samples if s.status == 200), key=lambda s: -s.ms)[:3]
    print("\n  slowest requests:")
    for s in slowest:
        print(f"    {s.ms:7.1f} ms  {s.query[:60]!r}")

    if stages and stages.embed:
        print("\n=== where the time goes (in-process, same calls the route makes) ===")
        rows = [
            ("embed query (OpenAI)", stages.embed),
            ("vector search (Pinecone)", stages.pinecone),
            ("open DB connection", stages.db_connect),
            ("full text (Postgres)", stages.keyword),
            ("fuse + hydrate rows", stages.fuse_hydrate),
        ]
        total_p50 = sum(percentile(sorted(v), 50) for _, v in rows)
        print(f"  {'stage':<26}{'p50 ms':>10}{'p95 ms':>10}{'share of p50':>14}")
        for label, values in rows:
            ordered = sorted(values)
            p50 = percentile(ordered, 50)
            print(
                f"  {label:<26}{p50:10.1f}{percentile(ordered, 95):10.1f}"
                f"{p50 / total_p50 * 100:13.0f}%"
            )
        print(f"  {'sum of stages':<26}{total_p50:10.1f}")
        print(f"  samples: {len(stages.embed)}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    ap.add_argument("--project-id", help="defaults to the caller's first ready project")
    ap.add_argument("--email")
    ap.add_argument("--password")
    ap.add_argument("--token", help="use an existing bearer token")
    ap.add_argument("--mint-token", metavar="EMAIL", help="sign a token locally")
    ap.add_argument("-n", "--queries", type=int, default=100)
    ap.add_argument("--top-k", type=int, default=10)
    ap.add_argument("--warmup", type=int, default=5, help="excluded from the stats")
    ap.add_argument("--seed", type=int, default=1729)
    ap.add_argument("--timeout", type=float, default=60.0)
    ap.add_argument(
        "--stages",
        type=int,
        default=0,
        metavar="N",
        help="also time N queries stage by stage, in-process (extra paid calls)",
    )
    ap.add_argument("--json", metavar="PATH", help="write the run to a JSON file")
    args = ap.parse_args()

    if args.token:
        token = args.token
    elif args.mint_token:
        token = mint_token(args.mint_token)
    elif args.email and args.password:
        with httpx.Client(base_url=args.base_url, timeout=args.timeout) as client:
            token = login(client, args.email, args.password)
    else:
        raise SystemExit("need --token, --mint-token EMAIL, or --email with --password")

    queries, pool_size, from_golden = build_queries(args.queries, args.seed)
    headers = {"Authorization": f"Bearer {token}"}
    with httpx.Client(base_url=args.base_url, timeout=args.timeout) as client:
        project_id = resolve_project(client, headers, args.project_id)

    print(
        f"\n{args.queries} queries from a pool of {pool_size} distinct "
        f"({from_golden} from the golden set, {pool_size - from_golden} extra), "
        f"top_k={args.top_k}, warmup={args.warmup}, sequential"
    )
    print(f"target: {args.base_url}/api/v1/projects/{project_id}/search")

    samples = run_http(
        args.base_url,
        token,
        project_id,
        queries,
        args.top_k,
        args.warmup,
        args.timeout,
    )
    summary = summarise(samples)

    stages = None
    if args.stages:
        print(f"\ntiming {args.stages} queries stage by stage...")
        stages = asyncio.run(
            run_stage_breakdown(project_id, queries[: args.stages], args.top_k)
        )

    report(summary, samples, stages)

    if args.json:
        payload = {
            "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "target": {
                "base_url": args.base_url,
                "project_id": project_id,
                "top_k": args.top_k,
                "warmup": args.warmup,
                "concurrency": 1,
            },
            "config": {
                "embedding_model": settings.openai_embedding_model,
                "embedding_dimensions": settings.openai_embedding_dimensions,
                "candidate_depth": 30,
                "distinct_queries": pool_size,
                "seed": args.seed,
            },
            "summary": summary,
            "samples": [
                {"query": s.query, "ms": round(s.ms, 2), "status": s.status}
                for s in samples
            ],
        }
        if stages:
            payload["stages_ms"] = {
                "embed": stages.embed,
                "pinecone": stages.pinecone,
                "db_connect": stages.db_connect,
                "keyword": stages.keyword,
                "fuse_hydrate": stages.fuse_hydrate,
            }
        path = pathlib.Path(args.json)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        print(f"\nwrote {path}")


if __name__ == "__main__":
    main()
