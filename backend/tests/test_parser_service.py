"""Parser tests.

Pure functions over a temp directory, so these are fast and catch the most
regressions per line. Several assertions here document deliberate limitations
rather than desired behaviour - they are marked as such.
"""

import hashlib

from app.services.parser_service import CodeEntity, parse_codebase


def write(tmp_path, rel: str, source: str) -> None:
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


def by_name(entities: list[CodeEntity]) -> dict[str, CodeEntity]:
    return {e.name: e for e in entities}


class TestExtraction:
    def test_extracts_top_level_function(self, tmp_path):
        write(tmp_path, "m.py", "def alpha(a, b):\n    return a + b\n")
        found = by_name(parse_codebase(str(tmp_path)))
        assert "alpha" in found
        assert found["alpha"].entity_type == "function"
        assert found["alpha"].start_line == 1
        assert found["alpha"].end_line == 2

    def test_extracts_class_and_its_methods(self, tmp_path):
        write(tmp_path, "m.py", "class Thing:\n    def go(self):\n        pass\n")
        found = by_name(parse_codebase(str(tmp_path)))
        assert found["Thing"].entity_type == "class"
        # Methods are qualified, which is what becomes entities.qualname.
        assert found["Thing.go"].entity_type == "method"

    def test_async_functions_are_functions(self, tmp_path):
        write(tmp_path, "m.py", "async def fetch():\n    pass\n")
        assert by_name(parse_codebase(str(tmp_path)))["fetch"].entity_type == "function"

    def test_async_methods_are_methods(self, tmp_path):
        write(tmp_path, "m.py", "class C:\n    async def go(self):\n        pass\n")
        assert by_name(parse_codebase(str(tmp_path)))["C.go"].entity_type == "method"

    def test_source_code_captured(self, tmp_path):
        write(tmp_path, "m.py", "def alpha():\n    return 42\n")
        assert "return 42" in by_name(parse_codebase(str(tmp_path)))["alpha"].source_code

    def test_file_path_is_relative(self, tmp_path):
        write(tmp_path, "pkg/deep/m.py", "def alpha():\n    pass\n")
        path = by_name(parse_codebase(str(tmp_path)))["alpha"].file_path
        assert not path.startswith(str(tmp_path))
        assert "m.py" in path

    def test_multiple_files(self, tmp_path):
        write(tmp_path, "a.py", "def one():\n    pass\n")
        write(tmp_path, "b.py", "def two():\n    pass\n")
        assert {"one", "two"} <= set(by_name(parse_codebase(str(tmp_path))))


class TestLimitations:
    """These assert current behaviour, not desired behaviour."""

    def test_nested_functions_are_not_extracted(self, tmp_path):
        write(tmp_path, "m.py", "def outer():\n    def inner():\n        pass\n")
        found = by_name(parse_codebase(str(tmp_path)))
        assert "outer" in found
        assert "inner" not in found

    def test_conditionally_defined_functions_are_not_extracted(self, tmp_path):
        write(tmp_path, "m.py", "if True:\n    def conditional():\n        pass\n")
        assert "conditional" not in by_name(parse_codebase(str(tmp_path)))

    def test_nested_class_methods_are_not_extracted(self, tmp_path):
        write(tmp_path, "m.py", "class Outer:\n    class Inner:\n        def go(self):\n            pass\n")
        found = by_name(parse_codebase(str(tmp_path)))
        assert "Outer" in found
        assert "Outer.Inner.go" not in found


class TestSkipsAndTolerance:
    def test_skips_noise_directories(self, tmp_path):
        write(tmp_path, "keep.py", "def keep():\n    pass\n")
        for noisy in ("__pycache__", "venv", ".venv", "node_modules", ".git"):
            write(tmp_path, f"{noisy}/skip.py", "def skipped():\n    pass\n")
        found = by_name(parse_codebase(str(tmp_path)))
        assert "keep" in found
        assert "skipped" not in found

    def test_skips_egg_info(self, tmp_path):
        write(tmp_path, "pkg.egg-info/skip.py", "def skipped():\n    pass\n")
        assert "skipped" not in by_name(parse_codebase(str(tmp_path)))

    def test_syntax_error_skips_only_that_file(self, tmp_path):
        write(tmp_path, "bad.py", "def broken(:\n")
        write(tmp_path, "good.py", "def fine():\n    pass\n")
        found = by_name(parse_codebase(str(tmp_path)))
        assert "fine" in found

    def test_undecodable_file_skips_only_that_file(self, tmp_path):
        (tmp_path / "binary.py").write_bytes(b"\xff\xfe\x00\x01 def x():")
        write(tmp_path, "good.py", "def fine():\n    pass\n")
        assert "fine" in by_name(parse_codebase(str(tmp_path)))

    def test_empty_directory_returns_empty(self, tmp_path):
        assert parse_codebase(str(tmp_path)) == []


class TestSignatures:
    def test_includes_argument_names(self, tmp_path):
        write(tmp_path, "m.py", "def alpha(a, b, c):\n    pass\n")
        sig = by_name(parse_codebase(str(tmp_path)))["alpha"].signature
        assert "a" in sig and "b" in sig and "c" in sig

    def test_marks_return_annotation(self, tmp_path):
        write(tmp_path, "m.py", "def alpha() -> int:\n    pass\n")
        assert "->" in by_name(parse_codebase(str(tmp_path)))["alpha"].signature

    def test_no_arrow_without_annotation(self, tmp_path):
        write(tmp_path, "m.py", "def alpha():\n    pass\n")
        assert "->" not in by_name(parse_codebase(str(tmp_path)))["alpha"].signature

    def test_class_signature_includes_base(self, tmp_path):
        write(tmp_path, "m.py", "class Child(Parent):\n    pass\n")
        assert "Parent" in by_name(parse_codebase(str(tmp_path)))["Child"].signature


class TestContentHash:
    def test_is_sha256_of_source(self, tmp_path):
        write(tmp_path, "m.py", "def alpha():\n    return 1\n")
        entity = by_name(parse_codebase(str(tmp_path)))["alpha"]
        assert entity.content_hash == hashlib.sha256(
            entity.source_code.encode("utf-8")
        ).hexdigest()

    def test_length_fits_the_column(self, tmp_path):
        write(tmp_path, "m.py", "def alpha():\n    pass\n")
        # entities.content_hash is String(64)
        assert len(by_name(parse_codebase(str(tmp_path)))["alpha"].content_hash) == 64

    def test_same_source_same_hash_across_files(self, tmp_path):
        body = "def alpha():\n    return 1\n"
        write(tmp_path, "a.py", body)
        write(tmp_path, "b.py", body)
        hashes = {e.content_hash for e in parse_codebase(str(tmp_path))}
        assert len(hashes) == 1

    def test_changed_source_changes_hash(self, tmp_path):
        write(tmp_path, "a.py", "def alpha():\n    return 1\n")
        first = by_name(parse_codebase(str(tmp_path)))["alpha"].content_hash
        write(tmp_path, "a.py", "def alpha():\n    return 2\n")
        second = by_name(parse_codebase(str(tmp_path)))["alpha"].content_hash
        assert first != second
