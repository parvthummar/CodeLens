"""Entity persistence: dedupe, upsert identity, delete_missing, hydration."""

import uuid

import pytest

from app.services import entity_service
from app.services.parser_service import CodeEntity

pytestmark = pytest.mark.db


def ent(file_path: str, name: str, code: str, kind: str = "function") -> CodeEntity:
    return CodeEntity(
        name=name,
        entity_type=kind,
        source_code=code,
        signature=f"def {name}():",
        file_path=file_path,
        start_line=1,
        end_line=2,
    )


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


class TestDeleteMissing:
    async def test_removes_only_absent_entities(self, db, project):
        entities = [ent("a.py", "keep", "v"), ent("a.py", "drop", "v")]
        ids = await entity_service.upsert_entities(
            db, entity_service.build_rows(project.id, entities, ["d", "d"])
        )
        removed = await entity_service.delete_missing(db, project.id, [ids[("a.py", "keep")]])
        assert removed == [ids[("a.py", "drop")]]
        assert await entity_service.count_for_project(db, project.id) == 1

    async def test_empty_keep_set_deletes_everything(self, db, project):
        """What should happen when a repo no longer parses to any entities."""
        entities = [ent("a.py", "one", "v"), ent("a.py", "two", "v")]
        ids = await entity_service.upsert_entities(
            db, entity_service.build_rows(project.id, entities, ["d", "d"])
        )
        removed = await entity_service.delete_missing(db, project.id, [])
        assert sorted(removed) == sorted(ids.values())
        assert await entity_service.count_for_project(db, project.id) == 0

    async def test_keeping_everything_removes_nothing(self, db, project):
        ids = await entity_service.upsert_entities(
            db, entity_service.build_rows(project.id, [ent("a.py", "one", "v")], ["d"])
        )
        assert await entity_service.delete_missing(db, project.id, list(ids.values())) == []

    async def test_does_not_touch_other_projects(self, db, user):
        from tests.conftest import make_project

        mine, theirs = make_project(user), make_project(user)
        db.add_all([mine, theirs])
        await db.flush()

        await entity_service.upsert_entities(
            db, entity_service.build_rows(mine.id, [ent("a.py", "foo", "v")], ["d"])
        )
        keep_theirs = await entity_service.upsert_entities(
            db, entity_service.build_rows(theirs.id, [ent("a.py", "foo", "v")], ["d"])
        )

        await entity_service.delete_missing(db, mine.id, [])
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
