"""Phase 0 smoke test for the Neon Postgres connection.

Verifies URL normalisation, that both endpoints connect, and — the part that
actually matters — that concurrent traffic survives without tripping over
prepared-statement reuse. Times each endpoint so the pooled/direct choice for
the app is made on evidence rather than assumption.

Run from the backend/ directory:
    cr_venv\\Scripts\\python.exe scripts\\check_db.py
"""

import asyncio
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.db.postgres import build_engine_from_url, is_pooled_host, normalize_dsn

BURST = 40

_failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'ok' if ok else 'FAIL'}] {label}" + (f" - {detail}" if detail else ""))
    if not ok:
        _failures.append(label)


def redact(url: str) -> str:
    """Strip the password so this script's output is safe to paste."""
    if "@" not in url or "://" not in url:
        return url or "(unset)"
    scheme, rest = url.split("://", 1)
    creds, host = rest.split("@", 1)
    return f"{scheme}://{creds.split(':', 1)[0]}:***@{host}"


def test_normalization() -> None:
    print("\nURL normalisation")
    raw = "postgresql://u:p@host-pooler.neon.tech/db?sslmode=require&channel_binding=require"
    dsn, ssl_required = normalize_dsn(raw)
    check("scheme rewritten for asyncpg", dsn.startswith("postgresql+asyncpg://"))
    check("sslmode stripped", "sslmode" not in dsn)
    check("channel_binding stripped", "channel_binding" not in dsn)
    check("TLS requirement detected", ssl_required is True)
    check("host and database preserved", "host-pooler.neon.tech/db" in dsn)
    check("pooled host detected", is_pooled_host(dsn) is True)

    plain, plain_ssl = normalize_dsn("postgresql://u:p@host.neon.tech/db")
    check("no-query URL survives", plain == "postgresql+asyncpg://u:p@host.neon.tech/db", plain)
    check("TLS not falsely required", plain_ssl is False)
    check("direct host detected", is_pooled_host(plain) is False)


async def _worker(sessions: async_sessionmaker[AsyncSession], n: int) -> int:
    """One short session issuing parameterised queries.

    Bound parameters are what push asyncpg onto the prepared-statement path, so
    this is the traffic shape that exposes a bad PgBouncer configuration.
    """
    async with sessions() as session:
        await session.execute(text("SELECT 1"))
        result = await session.execute(text("SELECT (:n)::int * 2"), {"n": n})
        return result.scalar_one()


async def exercise_endpoint(label: str, url: str) -> None:
    print(f"\n{label}")
    print(f"  {redact(url)}")
    if not url:
        check(f"{label}: configured", False, "missing from .env")
        return

    dsn, _ = normalize_dsn(url)
    pooled = is_pooled_host(dsn)
    print(
        "  mode: "
        + (
            "PgBouncer -> NullPool + unique statement names"
            if pooled
            else "direct -> QueuePool(5) + statement cache"
        )
    )

    engine = build_engine_from_url(url)
    sessions = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    try:
        async with engine.connect() as conn:
            version = (await conn.execute(text("SELECT version()"))).scalar_one()
            db, user = (
                await conn.execute(text("SELECT current_database(), current_user"))
            ).one()
        check(f"{label}: connected", True, f"{version.split(' on ')[0]} | {db}/{user}")

        # Warm one connection so the timing below excludes first-connect cost
        # (Neon free-tier compute may have been suspended).
        async with sessions() as session:
            await session.execute(text("SELECT 1"))

        started = time.perf_counter()
        results = await asyncio.gather(
            *(_worker(sessions, i) for i in range(BURST)), return_exceptions=True
        )
        elapsed = time.perf_counter() - started

        errors = [r for r in results if isinstance(r, BaseException)]
        if errors:
            kinds = Counter(type(e).__name__ for e in errors)
            check(
                f"{label}: {BURST} concurrent sessions",
                False,
                f"{len(errors)}/{BURST} failed: {dict(kinds)}",
            )
            print(f"        first error: {errors[0]!r}")
        else:
            check(
                f"{label}: {BURST} concurrent sessions",
                results == [i * 2 for i in range(BURST)],
                f"all correct, {elapsed * 1000:.0f} ms total",
            )
    except Exception as e:
        check(f"{label}: connected", False, repr(e))
    finally:
        await engine.dispose()


async def main() -> int:
    print("=" * 68)
    print("Phase 0 - Neon Postgres smoke test")
    print("=" * 68)

    test_normalization()
    await exercise_endpoint("Pooled endpoint (DATABASE_URL)", settings.database_url)
    await exercise_endpoint(
        "Direct endpoint (DATABASE_URL_DIRECT)", settings.database_url_direct
    )

    print("\n" + "=" * 68)
    if _failures:
        print(f"FAILED ({len(_failures)}): " + ", ".join(_failures))
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
