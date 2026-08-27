"""Dependencies, scope hints and plan markers are parsed from the issue body
(ADR-0007, ADR-0014).

The parser is a pure function. Malformed blocks are reported as issues, never
guessed at. The renderer is its inverse: rendered bodies round-trip and
anything the parser would flag raises instead of being emitted.
"""

import pytest

from ticketflow.domain.parser import parse_body, render_child_body

PLAN_ID = "a3f8c2d91b04"


class TestDependsOn:
    def test_single_key(self) -> None:
        parsed = parse_body("Do the thing.\n\ndepends-on: PROJ-41\n")
        assert parsed.depends_on == ("PROJ-41",)
        assert parsed.issues == ()

    def test_multiple_keys_on_one_line(self) -> None:
        parsed = parse_body("depends-on: PROJ-41, PROJ-38,PROJ-7")
        assert parsed.depends_on == ("PROJ-41", "PROJ-38", "PROJ-7")

    def test_keyword_is_case_insensitive(self) -> None:
        parsed = parse_body("Depends-On: PROJ-1\nDEPENDS-ON: PROJ-2")
        assert parsed.depends_on == ("PROJ-1", "PROJ-2")

    def test_multiple_lines_union_preserving_order(self) -> None:
        parsed = parse_body("depends-on: A-1\nsome text\ndepends-on: A-2, A-3")
        assert parsed.depends_on == ("A-1", "A-2", "A-3")

    def test_duplicate_keys_are_deduplicated(self) -> None:
        parsed = parse_body("depends-on: A-1, A-1\ndepends-on: A-1")
        assert parsed.depends_on == ("A-1",)

    def test_github_issue_numbers(self) -> None:
        parsed = parse_body("depends-on: #12, #7")
        assert parsed.depends_on == ("#12", "#7")

    def test_no_block_means_no_dependencies(self) -> None:
        parsed = parse_body("Just a description. It depends on good weather.")
        assert parsed.depends_on == ()
        assert parsed.issues == ()

    def test_keyword_must_start_the_line(self) -> None:
        parsed = parse_body("This work depends-on: PROJ-1 loosely speaking.")
        assert parsed.depends_on == ()

    def test_leading_whitespace_is_tolerated(self) -> None:
        parsed = parse_body("  depends-on: PROJ-9")
        assert parsed.depends_on == ("PROJ-9",)

    def test_empty_value_is_reported_not_guessed(self) -> None:
        parsed = parse_body("depends-on:\n")
        assert parsed.depends_on == ()
        assert len(parsed.issues) == 1
        assert "depends-on" in parsed.issues[0]

    def test_malformed_key_is_reported_and_valid_keys_kept(self) -> None:
        parsed = parse_body("depends-on: PROJ-1, not a key!, PROJ-2")
        assert parsed.depends_on == ("PROJ-1", "PROJ-2")
        assert len(parsed.issues) == 1
        assert "not a key!" in parsed.issues[0]


class TestScopeHints:
    def test_single_line_of_paths(self) -> None:
        parsed = parse_body("scope: src/api/**, docs/adr/\n")
        assert parsed.scope == ("src/api/**", "docs/adr/")

    def test_absent_scope_is_empty(self) -> None:
        assert parse_body("nothing here").scope == ()

    def test_scope_keyword_case_insensitive(self) -> None:
        assert parse_body("Scope: a/b.py").scope == ("a/b.py",)

    def test_empty_scope_reported(self) -> None:
        parsed = parse_body("scope:")
        assert parsed.scope == ()
        assert len(parsed.issues) == 1

    def test_combined_body(self) -> None:
        body = (
            "Implement the widget.\n"
            "\n"
            "depends-on: PROJ-3, PROJ-4\n"
            "scope: src/widget/, tests/widget/\n"
        )
        parsed = parse_body(body)
        assert parsed.depends_on == ("PROJ-3", "PROJ-4")
        assert parsed.scope == ("src/widget/", "tests/widget/")
        assert parsed.issues == ()


class TestPlanMarker:
    def test_marker_parsed(self) -> None:
        parsed = parse_body(f"Do the thing.\n\ntf-plan: {PLAN_ID}/3")
        assert parsed.plan_marker == (PLAN_ID, 3)
        assert parsed.issues == ()

    def test_absent_marker_is_none(self) -> None:
        assert parse_body("Just a description.").plan_marker is None

    def test_keyword_case_insensitive(self) -> None:
        assert parse_body(f"TF-Plan: {PLAN_ID}/0").plan_marker == (PLAN_ID, 0)

    def test_malformed_marker_reported_not_guessed(self) -> None:
        parsed = parse_body("tf-plan: not-a-marker")
        assert parsed.plan_marker is None
        assert len(parsed.issues) == 1
        assert "tf-plan" in parsed.issues[0]

    def test_conflicting_second_marker_reported_first_wins(self) -> None:
        parsed = parse_body(f"tf-plan: {PLAN_ID}/1\ntf-plan: {PLAN_ID}/2")
        assert parsed.plan_marker == (PLAN_ID, 1)
        assert len(parsed.issues) == 1
        assert "conflicting" in parsed.issues[0]

    def test_duplicate_identical_marker_is_not_an_issue(self) -> None:
        parsed = parse_body(f"tf-plan: {PLAN_ID}/1\ntf-plan: {PLAN_ID}/1")
        assert parsed.plan_marker == (PLAN_ID, 1)
        assert parsed.issues == ()

    def test_keyword_must_start_the_line(self) -> None:
        assert parse_body(f"see tf-plan: {PLAN_ID}/1 above").plan_marker is None


class TestRenderChildBody:
    def test_round_trips_through_parse_body(self) -> None:
        rendered = render_child_body(
            "Build the widget.\n",
            plan_id=PLAN_ID,
            item_index=2,
            depends_on=("#12", "PROJ-7"),
            scope=("src/widget/", "tests/"),
        )
        parsed = parse_body(rendered)
        assert parsed.depends_on == ("#12", "PROJ-7")
        assert parsed.plan_marker == (PLAN_ID, 2)
        assert parsed.scope == ("src/widget/", "tests/")
        assert parsed.issues == ()

    def test_re_render_is_byte_identical(self) -> None:
        first = render_child_body("Body.", plan_id=PLAN_ID, item_index=1, depends_on=("#1",))
        second = render_child_body("Body.", plan_id=PLAN_ID, item_index=1, depends_on=("#1",))
        assert first == second

    def test_marker_only_when_no_dependencies(self) -> None:
        rendered = render_child_body("Root item.", plan_id=PLAN_ID, item_index=0)
        parsed = parse_body(rendered)
        assert parsed.depends_on == ()
        assert parsed.plan_marker == (PLAN_ID, 0)
        assert parsed.issues == ()

    def test_malformed_key_raises_instead_of_emitting(self) -> None:
        with pytest.raises(ValueError, match="depends-on key"):
            render_child_body("B", plan_id=PLAN_ID, item_index=0, depends_on=("not a key!",))

    def test_duplicate_keys_raise(self) -> None:
        with pytest.raises(ValueError, match="duplicate"):
            render_child_body("B", plan_id=PLAN_ID, item_index=0, depends_on=("#1", "#1"))

    def test_malformed_plan_id_raises(self) -> None:
        with pytest.raises(ValueError, match="plan id"):
            render_child_body("B", plan_id="XYZ", item_index=0)

    def test_negative_index_raises(self) -> None:
        with pytest.raises(ValueError, match="index"):
            render_child_body("B", plan_id=PLAN_ID, item_index=-1)

    def test_scope_path_with_comma_raises(self) -> None:
        with pytest.raises(ValueError, match="scope"):
            render_child_body("B", plan_id=PLAN_ID, item_index=0, scope=("a,b",))
