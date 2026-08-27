"""Dependencies and scope hints are parsed from the issue body (ADR-0007).

The parser is a pure function. Malformed blocks are reported as issues, never
guessed at.
"""

from ticketflow.domain.parser import parse_body


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
