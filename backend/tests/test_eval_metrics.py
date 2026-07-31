"""Scoring logic for the retrieval eval.

Every number step 5 reports comes out of these few functions, so they are worth
testing directly. No database and no network: `QueryOutcome` is pure, and the
rankings here are handwritten rather than retrieved.
"""

import pytest

from eval._common import matches
from eval.run_eval import QueryOutcome

SECURITY = "backend/app/core/security.py::hash_password"
VERIFY = "backend/app/core/security.py::verify_password"
TEST_DOUBLE = "backend/tests/test_security.py::TestHashing.test_hashes"


def outcome(ranked, expected=(SECURITY,), category="natural") -> QueryOutcome:
    return QueryOutcome(
        id=1,
        query="hash a password",
        category=category,
        expected=list(expected),
        ranked=list(ranked),
    )


class TestMatching:
    def test_exact_key_matches(self):
        assert matches(SECURITY, SECURITY)

    def test_qualname_must_match_exactly(self):
        """`login` exists in two files; they are different answers."""
        assert not matches(SECURITY, VERIFY)

    def test_path_matches_by_suffix(self):
        """A corpus rooted at backend/ and one rooted at the repo root agree."""
        assert matches("app/core/security.py::hash_password", SECURITY)
        assert matches(SECURITY, "app/core/security.py::hash_password")

    def test_windows_separators_are_normalised_by_key(self):
        from eval._common import key

        assert key("app\\core\\security.py", "hash_password").startswith("app/core/")

    def test_a_different_file_with_the_same_name_does_not_match(self):
        assert not matches(
            "backend/app/services/auth_service.py::login",
            "backend/app/api/v1/auth.py::login",
        )


class TestFirstHitRank:
    def test_rank_is_one_indexed(self):
        assert outcome([SECURITY, VERIFY]).first_hit_rank == 1

    def test_finds_a_hit_further_down(self):
        assert outcome([VERIFY, TEST_DOUBLE, SECURITY]).first_hit_rank == 3

    def test_none_when_absent(self):
        assert outcome([VERIFY, TEST_DOUBLE]).first_hit_rank is None

    def test_empty_ranking_is_a_miss_not_a_crash(self):
        assert outcome([]).first_hit_rank is None

    def test_any_acceptable_answer_counts(self):
        """Multi-answer labels mean alternatives, not a set to be exhausted."""
        o = outcome([VERIFY], expected=(SECURITY, VERIFY))
        assert o.first_hit_rank == 1


class TestSuccessAtK:
    @pytest.mark.parametrize("position,expected", [(1, True), (5, True), (6, False)])
    def test_cutoff_is_inclusive(self, position, expected):
        ranked = [VERIFY] * (position - 1) + [SECURITY]
        assert outcome(ranked).success_at(5) is expected

    def test_success_at_1_is_stricter(self):
        o = outcome([VERIFY, SECURITY])
        assert o.success_at(1) is False
        assert o.success_at(5) is True


class TestReciprocalRank:
    @pytest.mark.parametrize("rank,rr", [(1, 1.0), (2, 0.5), (4, 0.25)])
    def test_is_one_over_rank(self, rank, rr):
        ranked = [VERIFY] * (rank - 1) + [SECURITY]
        assert outcome(ranked).reciprocal_rank == rr

    def test_zero_when_nothing_relevant_is_retrieved(self):
        assert outcome([VERIFY, TEST_DOUBLE]).reciprocal_rank == 0.0


class TestStrictRecall:
    """The metric the headline number deliberately is not.

    With two acceptable answers, finding one is a complete success for a user
    and a 0.5 for textbook recall. Both are printed so the gap is visible.
    """

    def test_one_of_two_acceptable_answers_is_a_half(self):
        o = outcome([SECURITY], expected=(SECURITY, VERIFY))
        assert o.strict_recall_at(5) == 0.5
        assert o.success_at(5) is True

    def test_both_found_is_one(self):
        o = outcome([SECURITY, VERIFY], expected=(SECURITY, VERIFY))
        assert o.strict_recall_at(5) == 1.0

    def test_respects_the_cutoff(self):
        ranked = [SECURITY] + [TEST_DOUBLE] * 8 + [VERIFY]
        o = outcome(ranked, expected=(SECURITY, VERIFY))
        assert o.strict_recall_at(5) == 0.5
        assert o.strict_recall_at(10) == 1.0


class TestGoldenSetIntegrity:
    """Cheap guards so a malformed edit fails here rather than mid-run."""

    def test_loads_and_has_queries(self):
        from eval._common import load_golden_set

        golden = load_golden_set()
        assert len(golden["queries"]) >= 50

    def test_ids_are_unique(self):
        from eval._common import load_golden_set

        ids = [q["id"] for q in load_golden_set()["queries"]]
        assert len(ids) == len(set(ids))

    def test_every_query_has_at_least_one_expected_answer(self):
        from eval._common import load_golden_set

        assert all(q["expected"] for q in load_golden_set()["queries"])

    def test_categories_are_declared(self):
        from eval._common import load_golden_set

        golden = load_golden_set()
        declared = set(golden["categories"])
        assert {q["category"] for q in golden["queries"]} <= declared
