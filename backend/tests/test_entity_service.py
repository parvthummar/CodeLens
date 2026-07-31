"""Entity persistence: dedupe, upsert identity, delete_missing, hydration."""

import uuid

import pytest

from app.services import entity_service
from app.services.parser_service import CodeEntity

pytestmark = pytest.mark.db


def ent(
    file_path: str,
    name: str,
    code: str,
    kind: str = "function",
    *,
    start_line: int = 1,
    end_line: int = 2,
) -> CodeEntity:
    return CodeEntity(
        name=name,
        entity_type=kind,
        source_code=code,
        signature=f"def {name}():",
        file_path=file_path,
        start_line=start_line,
        end_line=end_line,
    )


async def columns(db, entity_id, *names):
    """Read columns straight from the database, bypassing the identity map.

    mark_seen and refresh_locations issue Core UPDATEs, so an ORM instance the
    session is already holding would still show the pre-update values.
    """
    from sqlalchemy import select

    from app.models.entity import Entity

    row = (
        await db.execute(
            select(*[getattr(Entity, n) for n in names]).where(Entity.id == entity_id)
        )
    ).one()
    return row if len(names) > 1 else row[0]


async def seed(db, project_id, entities, descriptions=None, run_id=None):
    """Upsert entities for a project and return their ids keyed by identity."""
    rows = entity_service.build_rows(
        project_id,
        entities,
        descriptions or ["d"] * len(entities),
        run_id=run_id,
    )
    return await entity_service.upsert_entities(db, rows)


class TestDedupe:
    """No database needed, but kept beside the code it covers."""

    def test_collapses_same_file_and_qualname(self):
        out = entity_service.dedupe([ent("a.py", "foo", "1"), ent("a.py", "foo", "2")])
        assert len(out) == 1

    def test_last_definition_wins(self):
        out = entity_service.dedupe([ent("a.py", "foo", "first"), ent("a.py", "foo", "second")])
        assert out[0].source_code == "second"

    def test_same_name_in_different_files_is_distinct(self):
        out = entity_service.dedupe([ent("a.py", "foo", "x"), ent("b.py", "foo", "y")])
        assert len(out) == 2

    def test_different_names_in_one_file_are_distinct(self):
        out = entity_service.dedupe([ent("a.py", "foo", "x"), ent("a.py", "bar", "y")])
        assert len(out) == 2

    def test_empty_input(self):
        assert entity_service.dedupe([]) == []


class TestBuildRows:
    def test_pairs_descriptions_in_order(self):
        pid = uuid.uuid4()
        entities = [ent("a.py", "one", "x"), ent("a.py", "two", "y")]
        rows = entity_service.build_rows(pid, entities, ["d-one", "d-two"])
        assert [r["description"] for r in rows] == ["d-one", "d-two"]

    def test_maps_parser_name_to_qualname(self):
        rows = entity_service.build_rows(uuid.uuid4(), [ent("a.py", "C.go", "x", "method")], ["d"])
        assert rows[0]["qualname"] == "C.go"
        assert rows[0]["entity_type"] == "method"

    def test_carries_content_hash(self):
        entity = ent("a.py", "one", "x")
        rows = entity_service.build_rows(uuid.uuid4(), [entity], ["d"])
        assert rows[0]["content_hash"] == entity.content_hash

    def test_stamps_every_row_with_the_project(self):
        pid = uuid.uuid4()
        rows = entity_service.build_rows(pid, [ent("a.py", "one", "x"), ent("a.py", "two", "y")], ["d", "d"])
        assert all(r["project_id"] == pid for r in rows)


class TestUpsert:
    async def test_returns_an_id_per_entity_keyed_by_identity(self, db, project):
        entities = [ent("a.py", "foo", "v1"), ent("b.py", "bar", "v1")]
        ids = await entity_service.upsert_entities(
            db, entity_service.build_rows(project.id, entities, ["d", "d"])
        )
        assert set(ids) == {("a.py", "foo"), ("b.py", "bar")}
        assert all(isinstance(v, int) for v in ids.values())

    async def test_empty_batch_is_a_noop(self, db, project):
        assert await entity_service.upsert_entities(db, []) == {}

    async def test_row_count_matches(self, db, project):
        entities = [ent("a.py", "foo", "v"), ent("a.py", "bar", "v"), ent("b.py", "baz", "v")]
        await entity_service.upsert_entities(
            db, entity_service.build_rows(project.id, entities, ["d"] * 3)
        )
        assert await entity_service.count_for_project(db, project.id) == 3

    async def test_ids_are_stable_across_reindex(self, db, project):
        """The core Phase 4 guarantee: re-indexing must not remap identity."""
        entities = [ent("a.py", "foo", "v1"), ent("a.py", "bar", "v1")]
        first = await entity_service.upsert_entities(
            db, entity_service.build_rows(project.id, entities, ["d", "d"])
        )
        changed = [ent("a.py", "foo", "v2"), ent("a.py", "bar", "v1")]
        second = await entity_service.upsert_entities(
            db, entity_service.build_rows(project.id, changed, ["d2", "d"])
        )
        assert second == first

    async def test_reindex_creates_no_duplicate_rows(self, db, project):
        entities = [ent("a.py", "foo", "v1")]
        rows = entity_service.build_rows(project.id, entities, ["d"])
        await entity_service.upsert_entities(db, rows)
        await entity_service.upsert_entities(db, rows)
        assert await entity_service.count_for_project(db, project.id) == 1

    async def test_reindex_updates_in_place(self, db, project):
        ids = await entity_service.upsert_entities(
            db, entity_service.build_rows(project.id, [ent("a.py", "foo", "v1")], ["old"])
        )
        updated = ent("a.py", "foo", "v2")
        await entity_service.upsert_entities(
            db, entity_service.build_rows(project.id, [updated], ["new"])
        )
        entity = (await entity_service.get_by_ids(db, project.id, list(ids.values())))[ids[("a.py", "foo")]]
        assert entity.source_code == "v2"
        assert entity.description == "new"
        assert entity.content_hash == updated.content_hash

    async def test_projects_do_not_collide(self, db, user):
        """Identity is scoped per project, so two projects can hold the same path."""
        from tests.conftest import make_project

        one, two = make_project(user), make_project(user)
        db.add_all([one, two])
        await db.flush()

        entities = [ent("a.py", "foo", "v")]
        ids_one = await entity_service.upsert_entities(
            db, entity_service.build_rows(one.id, entities, ["d"])
        )
        ids_two = await entity_service.upsert_entities(
            db, entity_service.build_rows(two.id, entities, ["d"])
        )
        assert ids_one[("a.py", "foo")] != ids_two[("a.py", "foo")]

    async def test_long_source_is_not_truncated(self, db, project):
        """The 20 KB cap only existed because of Pinecone's metadata ceiling."""
        big = ent("big.py", "huge", "x = 1\n" * 8000)
        ids = await entity_service.upsert_entities(
            db, entity_service.build_rows(project.id, [big], ["d"])
        )
        entity = (await entity_service.get_by_ids(db, project.id, list(ids.values())))[list(ids.values())[0]]
        assert entity.source_code == big.source_code
        assert len(entity.source_code) > 20_000


class TestDiff:
    """Sorting parsed entities against what is already stored. No database.

    This is the decision that makes a re-index cheap, so it is tested as a pure
    function rather than only through the pipeline.
    """

    def stored(self, *rows) -> dict:
        return {
            (e.file_path, e.name): entity_service.StoredEntity(
                eid, e.content_hash, e.start_line, e.end_line
            )
            for eid, e in rows
        }

    def test_everything_is_changed_on_a_first_index(self):
        parsed = [ent("a.py", "foo", "v1"), ent("a.py", "bar", "v1")]
        diff = entity_service.diff_entities(parsed, {})
        assert diff.changed == parsed
        assert diff.moved == []
        assert diff.unchanged_ids == []

    def test_identical_source_is_unchanged(self):
        entity = ent("a.py", "foo", "v1")
        diff = entity_service.diff_entities([entity], self.stored((7, entity)))
        assert diff.changed == []
        assert diff.unchanged_ids == [7]

    def test_edited_source_is_changed(self):
        before = ent("a.py", "foo", "v1")
        after = ent("a.py", "foo", "v2")
        diff = entity_service.diff_entities([after], self.stored((7, before)))
        assert diff.changed == [after]
        assert diff.unchanged_ids == []

    def test_a_new_entity_is_changed(self):
        old = ent("a.py", "foo", "v1")
        new = ent("a.py", "bar", "v1")
        diff = entity_service.diff_entities([old, new], self.stored((7, old)))
        assert diff.changed == [new]
        assert diff.unchanged_ids == [7]

    def test_same_source_at_new_lines_is_moved_not_changed(self):
        """A shifted entity needs its location fixed, not a fresh description."""
        before = ent("a.py", "foo", "v1", start_line=1, end_line=2)
        after = ent("a.py", "foo", "v1", start_line=40, end_line=41)
        diff = entity_service.diff_entities([after], self.stored((7, before)))
        assert diff.changed == []
        assert diff.moved == [(7, after)]

    def test_a_rename_is_a_new_entity_plus_a_deletion(self):
        """qualname is identity, so a rename cannot be matched by hash alone."""
        before = ent("a.py", "foo", "v1")
        after = ent("a.py", "renamed", "v1")
        diff = entity_service.diff_entities([after], self.stored((7, before)))
        assert diff.changed == [after]
        # 7 goes unclaimed, so the deletion pass removes it.
        assert diff.unchanged_ids == []

    def test_moving_a_file_is_a_new_entity(self):
        before = ent("a.py", "foo", "v1")
        after = ent("pkg/a.py", "foo", "v1")
        diff = entity_service.diff_entities([after], self.stored((7, before)))
        assert diff.changed == [after]

    def test_reused_counts_both_cheap_buckets(self):
        same = ent("a.py", "same", "v1")
        shifted_before = ent("a.py", "shifted", "v1", start_line=1, end_line=2)
        shifted_after = ent("a.py", "shifted", "v1", start_line=9, end_line=10)
        edited = ent("a.py", "edited", "v2")

        diff = entity_service.diff_entities(
            [same, shifted_after, edited],
            self.stored((1, same), (2, shifted_before), (3, ent("a.py", "edited", "v1"))),
        )
        assert diff.reused == 2
        assert len(diff.changed) == 1

    def test_stored_entities_the_parser_no_longer_finds_are_simply_absent(self):
        gone = ent("a.py", "gone", "v1")
        diff = entity_service.diff_entities([], self.stored((7, gone)))
        assert diff.changed == []
        assert diff.reused == 0


class TestLoadStored:
    async def test_returns_identity_hash_and_location(self, db, project):
        entity = ent("a.py", "foo", "v1", start_line=3, end_line=9)
        ids = await seed(db, project.id, [entity])

        stored = await entity_service.load_stored(db, project.id)
        row = stored[("a.py", "foo")]
        assert row.id == ids[("a.py", "foo")]
        assert row.content_hash == entity.content_hash
        assert (row.start_line, row.end_line) == (3, 9)

    async def test_is_scoped_to_one_project(self, db, user):
        from tests.conftest import make_project

        mine, theirs = make_project(user), make_project(user)
        db.add_all([mine, theirs])
        await db.flush()

        await seed(db, mine.id, [ent("a.py", "mine", "v")])
        await seed(db, theirs.id, [ent("a.py", "theirs", "v")])

        assert set(await entity_service.load_stored(db, mine.id)) == {("a.py", "mine")}

    async def test_empty_project_returns_empty(self, db, project):
        assert await entity_service.load_stored(db, project.id) == {}

    async def test_round_trips_through_the_diff(self, db, project):
        """The two halves have to agree, or every re-index redoes everything."""
        entities = [ent("a.py", "foo", "v1"), ent("b.py", "bar", "v1")]
        await seed(db, project.id, entities)

        diff = entity_service.diff_entities(
            entities, await entity_service.load_stored(db, project.id)
        )
        assert diff.changed == []
        assert len(diff.unchanged_ids) == 2


class TestMarkSeen:
    async def test_stamps_the_run(self, db, project):
        ids = await seed(db, project.id, [ent("a.py", "foo", "v")])
        run = uuid.uuid4()

        assert await entity_service.mark_seen(db, list(ids.values()), run) == 1
        assert await columns(db, ids[("a.py", "foo")], "last_seen_run") == run

    async def test_empty_is_a_noop(self, db, project):
        assert await entity_service.mark_seen(db, [], uuid.uuid4()) == 0

    async def test_does_not_bump_updated_at(self, db, project):
        """Nothing about the entity changed — only that a run looked at it."""
        ids = await seed(db, project.id, [ent("a.py", "foo", "v")])
        entity_id = ids[("a.py", "foo")]
        before = await columns(db, entity_id, "updated_at")

        await entity_service.mark_seen(db, [entity_id], uuid.uuid4())
        assert await columns(db, entity_id, "updated_at") == before

    async def test_handles_a_batch_far_past_the_parameter_limit(self, db, project):
        """The point of the array bind: an IN list would fail here.

        Postgres caps a statement at 65535 parameters, so the old NOT IN
        approach broke somewhere around that many entities. 70k ids go in as a
        single array parameter.
        """
        ids = await seed(db, project.id, [ent("a.py", "foo", "v")])
        entity_id = ids[("a.py", "foo")]

        # One real id among 70,000 that do not exist. Only the real one matches,
        # but all 70,001 have to reach Postgres for that to be provable.
        padded = [entity_id, *range(10**12, 10**12 + 70_000)]
        assert await entity_service.mark_seen(db, padded, uuid.uuid4()) == 1


class TestRefreshLocations:
    async def test_updates_lines_without_touching_the_description(self, db, project):
        original = ent("a.py", "foo", "v", start_line=1, end_line=2)
        ids = await seed(db, project.id, [original], ["the description"])
        entity_id = ids[("a.py", "foo")]

        moved = ent("a.py", "foo", "v", start_line=50, end_line=51)
        run = uuid.uuid4()
        assert await entity_service.refresh_locations(db, [(entity_id, moved)], run) == 1

        start, end, description, content_hash, seen = await columns(
            db, entity_id, "start_line", "end_line", "description", "content_hash",
            "last_seen_run",
        )
        assert (start, end) == (50, 51)
        assert description == "the description"
        assert content_hash == original.content_hash
        assert seen == run

    async def test_empty_is_a_noop(self, db, project):
        assert await entity_service.refresh_locations(db, [], uuid.uuid4()) == 0

    async def test_each_row_gets_its_own_lines(self, db, project):
        one = ent("a.py", "one", "v1", start_line=1, end_line=2)
        two = ent("a.py", "two", "v2", start_line=3, end_line=4)
        ids = await seed(db, project.id, [one, two])

        run = uuid.uuid4()
        await entity_service.refresh_locations(
            db,
            [
                (ids[("a.py", "one")], ent("a.py", "one", "v1", start_line=10, end_line=11)),
                (ids[("a.py", "two")], ent("a.py", "two", "v2", start_line=20, end_line=21)),
            ],
            run,
        )

        assert await columns(db, ids[("a.py", "one")], "start_line") == 10
        assert await columns(db, ids[("a.py", "two")], "start_line") == 20


class TestDeleteMissing:
    """Deletion is now "what this run did not stamp", not "not in this list"."""

    async def test_removes_only_entities_the_run_did_not_see(self, db, project):
        run = uuid.uuid4()
        entities = [ent("a.py", "keep", "v"), ent("a.py", "drop", "v")]
        ids = await seed(db, project.id, entities)
        await entity_service.mark_seen(db, [ids[("a.py", "keep")]], run)

        removed = await entity_service.delete_missing(db, project.id, run)
        assert removed == [ids[("a.py", "drop")]]
        assert await entity_service.count_for_project(db, project.id) == 1

    async def test_a_run_that_saw_nothing_deletes_everything(self, db, project):
        """What should happen when a repo no longer parses to any entities."""
        entities = [ent("a.py", "one", "v"), ent("a.py", "two", "v")]
        ids = await seed(db, project.id, entities)

        removed = await entity_service.delete_missing(db, project.id, uuid.uuid4())
        assert sorted(removed) == sorted(ids.values())
        assert await entity_service.count_for_project(db, project.id) == 0

    async def test_stamping_everything_removes_nothing(self, db, project):
        run = uuid.uuid4()
        await seed(db, project.id, [ent("a.py", "one", "v")], run_id=run)
        assert await entity_service.delete_missing(db, project.id, run) == []

    async def test_an_upsert_stamps_the_run_it_was_written_by(self, db, project):
        """Rows written this run are kept without a separate mark_seen pass."""
        run = uuid.uuid4()
        await seed(db, project.id, [ent("a.py", "one", "v")], run_id=run)
        assert await entity_service.count_for_project(db, project.id) == 1
        assert await entity_service.delete_missing(db, project.id, run) == []

    async def test_a_previous_runs_stamp_does_not_survive(self, db, project):
        first, second = uuid.uuid4(), uuid.uuid4()
        await seed(db, project.id, [ent("a.py", "one", "v")], run_id=first)
        assert len(await entity_service.delete_missing(db, project.id, second)) == 1

    async def test_rows_predating_the_column_are_treated_as_unseen(self, db, project):
        """NULL means "no run has claimed this", not "keep it regardless"."""
        await seed(db, project.id, [ent("a.py", "legacy", "v")], run_id=None)
        assert len(await entity_service.delete_missing(db, project.id, uuid.uuid4())) == 1

    async def test_does_not_touch_other_projects(self, db, user):
        from tests.conftest import make_project

        mine, theirs = make_project(user), make_project(user)
        db.add_all([mine, theirs])
        await db.flush()

        await seed(db, mine.id, [ent("a.py", "foo", "v")])
        keep_theirs = await seed(db, theirs.id, [ent("a.py", "foo", "v")])

        await entity_service.delete_missing(db, mine.id, uuid.uuid4())
        assert await entity_service.count_for_project(db, theirs.id) == 1
        assert list(keep_theirs.values())[0] in await entity_service.get_by_ids(
            db, theirs.id, list(keep_theirs.values())
        )


class TestHydration:
    async def test_get_by_ids_returns_a_map(self, db, project):
        ids = await entity_service.upsert_entities(
            db, entity_service.build_rows(project.id, [ent("a.py", "foo", "v")], ["d"])
        )
        entity_id = ids[("a.py", "foo")]
        fetched = await entity_service.get_by_ids(db, project.id, [entity_id])
        assert fetched[entity_id].qualname == "foo"

    async def test_empty_ids_returns_empty(self, db):
        assert await entity_service.get_by_ids(db, uuid.uuid4(), []) == {}

    async def test_unknown_ids_are_simply_absent(self, db):
        assert await entity_service.get_by_ids(db, uuid.uuid4(), [10**15]) == {}


class TestCascade:
    async def test_deleting_the_project_deletes_its_entities(self, db, user):
        from tests.conftest import make_project

        record = make_project(user)
        db.add(record)
        await db.flush()
        await entity_service.upsert_entities(
            db, entity_service.build_rows(record.id, [ent("a.py", "foo", "v")], ["d"])
        )

        await db.delete(record)
        await db.flush()
        assert await entity_service.count_for_project(db, record.id) == 0
