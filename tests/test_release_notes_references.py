from __future__ import annotations

import pytest

from tests.test_release_notes import CFG, FakeGitHub, commit
from tools import release_notes as rn


def test_reference_order_is_first_seen_across_legacy_shapes() -> None:
    # Given: interleaved reference grammars, including a duplicate SHA prefix
    text = (
        "thing (#13) ([#12](https://github.com/acme/pkg/pull/12)) "
        "(`abcdef1`) ([fedcba9](https://github.com/acme/pkg/commit/fedcba987)) "
        "(`ABCDEF123`)(#13)"
    )
    # When: parse the existing body
    item = rn.parse_body("- " + text, CFG).items[0]
    # Then: neither a grammar pass nor de-duplication changes first-seen order
    assert (item.prs, item.shas) == ([13, 12], ["abcdef1", "fedcba9"])


@pytest.mark.parametrize(
    ("refs", "prs", "shas"),
    [
        ("(#143, #150)", [143, 150], []),
        ("(#143) (#150)", [143, 150], []),
        ("([#12](https://github.com/acme/pkg/pull/12))", [12], []),
        ("([abcdef1](https://github.com/acme/pkg/commit/abcdef123456))", [], ["abcdef1"]),
        ("(#12)", [12], []),
        ("(`abc1234`)", [], ["abc1234"]),
        ("by @external in https://github.com/acme/old-pkg/pull/12", [12], []),
        ("in https://github.com/acme/pkg/pull/12", [12], []),
        ("(#12, #13, `abc1234`, `def5678`)", [12, 13], ["abc1234", "def5678"]),
        (
            "([#12](https://github.com/acme/pkg/pull/12)) "
            "([abcdef1](https://github.com/acme/pkg/commit/abcdef123456))",
            [12],
            ["abcdef1"],
        ),
        ("(#13, #12, #13, `ABC1234`, `abc123456`)", [13, 12], ["abc1234"]),
    ],
)
def test_reference_shapes_round_trip_with_external_credit(
    refs: str, prs: list[int], shas: list[str]
) -> None:
    # Given: legacy and canonical references with independently verified authors
    gh = FakeGitHub(
        prs=dict.fromkeys(prs, ("external", False)),
        commits={("v1.0.0", "v1.0.1"): [commit(s, "fix: thing", "external") for s in shas]},
        veterans={"external"},
    )
    # When: render twice, as --force does
    once = rn.build_notes("v1.0.1", f"- fix: thing {refs}\n", gh, CFG)
    twice = rn.build_notes("v1.0.1", once, gh, CFG)
    item = rn.parse_body(once).items[0]
    # Then: every reference and its external credit survives without title debris
    assert (item.title, item.prs, item.shas) == ("thing", prs, shas)
    assert "by @external" in once
    assert twice == once


def test_item_emits_all_references_in_one_group() -> None:
    # Given: an item with multiple PRs and commits
    item = rn.Item("thing", prs=[12, 13], shas=["abc1234", "def5678"], authors=["user"])
    # When: serialize
    line = item.line()
    # Then: neither reference class replaces the other
    assert line == "- thing (#12, #13, `abc1234`, `def5678`) by @user"


@pytest.mark.parametrize("kind", ["pull", "issues", "commit"])
def test_foreign_links_are_verbatim_not_local_authorship(kind: str) -> None:
    # Given: an unrelated repository reference colliding with local PR 12
    ref = "abcdef1" if kind == "commit" else "12"
    link = f"([#{ref}](https://github.com/other/project/{kind}/{ref}))"
    gh = FakeGitHub(prs={12: ("victim", False)})
    # When: standardize
    body = rn.build_notes("v1.0.1", f"- thing {link}\n", gh, CFG)
    # Then: the link is not converted or used for credit
    assert f"- thing {link}\n" in body
    assert "@victim" not in body


@pytest.mark.parametrize(
    ("old", "new", "missing"),
    [
        ("- removed (#1)\n- kept (#12)", "- kept (#12)", "#1"),
        ("- removed (#58)\n- kept (#580)", "- kept (#580)", "#58"),
        ("- x (#12, `abcdef1`)", "- x (#12)", "abcdef1"),
        (
            "- x ([#12](https://github.com/acme/pkg/pull/12)) "
            "([abcdef1](https://github.com/acme/pkg/commit/abcdef123456))",
            "- x (#12)",
            "abcdef1",
        ),
        ("- x (`abcdef1`, `fedcba9`)", "- x (`abcdef1`)", "fedcba9"),
    ],
)
def test_verify_requires_exact_independent_references(old: str, new: str, missing: str) -> None:
    # Given: a candidate that loses one exact identity
    # When: compare reference sets
    problems = rn.verify(old, new, "v1.0.1", CFG)
    # Then: prefix collisions and same-line PRs cannot hide lost commits
    assert any(missing in problem for problem in problems)


@pytest.mark.parametrize("kind", ["pull", "issues"])
def test_verify_accepts_local_link_as_preserved_reference(kind: str) -> None:
    # Given: the same identity represented as a link rather than a plain token
    old = "- x (#12)"
    new = f"- x https://github.com/acme/pkg/{kind}/12"
    # When / Then: both bodies use the same extractor
    assert rn.verify(old, new, "v1.0.1", CFG) == []


def test_foreign_link_with_a_local_looking_label_is_neither_parsed_nor_credited() -> None:
    # Given: a foreign link whose label contains "(#12)", and a local PR 12 by someone else
    link = "[foreign (#12)](https://github.com/foreign/repo/pull/12)"
    gh = FakeGitHub(prs={12: ("victim", False)})
    # When: standardize it twice
    once = rn.build_notes("v1.0.1", f"- thing {link}\n", gh, CFG)
    twice = rn.build_notes("v1.0.1", once, gh, CFG)
    # Then: the link stays whole, its label creates no local reference and no credit
    assert f"- thing {link}\n" in once
    assert "@victim" not in once
    assert twice == once


def test_local_markdown_link_stays_verbatim_beside_a_real_reference() -> None:
    # Given: an ordinary local link in the title, plus a genuine trailing reference
    link = "[the discussion](https://github.com/acme/pkg/pull/5)"
    gh = FakeGitHub(prs={13: ("external", False)}, veterans={"external"})
    # When: standardize
    body = rn.build_notes("v1.0.1", f"- thing {link} (#13)\n", gh, CFG)
    # Then: only the real reference is read; the link text is kept as written
    assert f"- thing {link} (#13) by @external\n" in body


@pytest.mark.parametrize(
    ("label", "foreign_url"),
    [
        ("#12", "https://github.com/foreign/repo/pull/12"),
        ("#12", "https://github.com/foreign/repo/issues/12"),
        ("`abcdef1`", "https://github.com/foreign/repo/commit/abcdef1234"),
    ],
)
def test_verify_does_not_accept_a_foreign_link_label_as_a_local_reference(
    label: str, foreign_url: str
) -> None:
    # Given: the local reference exists only as a label on a foreign link
    old = f"- x ({label})"
    new = f"- x [{label}]({foreign_url})"
    # When: compare reference sets
    problems = rn.verify(old, new, "v1.0.1", CFG)
    # Then: the label does not stand in for the lost local reference
    assert problems and "lost" in problems[0]


@pytest.mark.parametrize("label", ["#12", "the pull request"])
def test_verify_accepts_a_local_markdown_link_through_its_url(label: str) -> None:
    # Given: the local PR kept as a Markdown link, with or without a numeric label
    old = "- x (#12)"
    new = f"- x [{label}](https://github.com/acme/pkg/pull/12)"
    # When / Then: the URL carries the identity
    assert rn.verify(old, new, "v1.0.1", CFG) == []


@pytest.mark.parametrize("token", ["BREAKING CHANGE", "BREAKING-CHANGE"])
def test_compare_breaking_footer_keeps_multiline_migration(token: str) -> None:
    # Given: a non-bang subject with a conventional breaking footer
    gh = FakeGitHub(
        commits={
            ("v1.0.0", "v1.0.1"): [
                commit(
                    "a",
                    "fix: drop API",
                    "maint",
                    f"{token}: remove foo; migrate to bar.\nUse bar(value) instead.\nRefs: #12",
                )
            ]
        }
    )
    # When: reconstruct a compare-only body
    body = rn.build_notes("v1.0.1", "", gh, CFG)
    # Then: migration guidance is retained under the breaking item, not other trailers
    assert "### ⚠️ Breaking Changes" in body
    assert "  remove foo; migrate to bar.\n  Use bar(value) instead." in body
    assert "Refs:" not in body


def _breaking_commit_body(footer: str) -> FakeGitHub:
    return FakeGitHub(
        commits={("v1.0.0", "v1.0.1"): [commit("a", "fix: drop API", "maint", footer)]}
    )


@pytest.mark.parametrize("token", ["BREAKING CHANGE", "BREAKING-CHANGE"])
def test_compare_breaking_footer_keeps_paragraphs_after_a_blank_line(token: str) -> None:
    # Given: migration guidance whose second paragraph follows a blank line, then a real trailer
    gh = _breaking_commit_body(f"{token}: remove foo.\n\nUse bar(value) instead.\n\nRefs: #12")
    # When: reconstruct a compare-only body
    body = rn.build_notes("v1.0.1", "", gh, CFG)
    # Then: both paragraphs stay under the breaking item; the trailer and its text do not
    assert "  remove foo.\n\n  Use bar(value) instead.\n" in body
    assert "Refs:" not in body


def test_compare_breaking_footer_keeps_prose_lines_that_contain_a_colon() -> None:
    # Given: guidance lines whose text before a colon is several words, not a trailer token
    gh = _breaking_commit_body(
        "BREAKING CHANGE: remove foo.\nMigration steps: replace foo with bar.\n\nThen rerun it."
    )
    # When: reconstruct a compare-only body
    body = rn.build_notes("v1.0.1", "", gh, CFG)
    # Then: nothing after the first line is mistaken for a footer and dropped
    assert "  Migration steps: replace foo with bar.\n\n  Then rerun it.\n" in body


def test_compare_breaking_footer_ends_at_the_next_trailer() -> None:
    # Given: a breaking footer followed by a sign-off and unrelated body text
    gh = _breaking_commit_body(
        "BREAKING CHANGE: remove foo.\n\nSigned-off-by: Dev <dev@example.com>\n\nunrelated text"
    )
    # When: reconstruct a compare-only body
    body = rn.build_notes("v1.0.1", "", gh, CFG)
    # Then: only the breaking guidance is published
    assert "  remove foo.\n" in body
    assert "Signed-off-by" not in body and "dev@example.com" not in body
    assert "unrelated text" not in body


def test_compare_breaking_footer_with_its_value_on_the_next_line() -> None:
    # Given: a footer token that carries no text on its own line
    gh = _breaking_commit_body("BREAKING CHANGE:\nremove foo.")
    # When: reconstruct a compare-only body
    body = rn.build_notes("v1.0.1", "", gh, CFG)
    # Then: no whitespace-only line precedes the guidance
    assert "(`aaaaaaa`)\n  remove foo.\n" in body


def test_reconstructed_breaking_paragraphs_survive_a_second_render() -> None:
    # Given: a reconstructed item with two guidance paragraphs
    gh = _breaking_commit_body("BREAKING CHANGE: remove foo.\n\nUse bar(value) instead.")
    # When: render the result again, as --force does
    once = rn.build_notes("v1.0.1", "", gh, CFG)
    twice = rn.build_notes("v1.0.1", once, gh, CFG)
    # Then: the paragraph break is read back from the item rather than dropped
    assert twice == once
    assert "  remove foo.\n\n  Use bar(value) instead.\n" in twice


def test_item_continuation_keeps_blank_lines_between_its_paragraphs() -> None:
    # Given: a bullet with an indented second paragraph, followed by an ordinary bullet
    body = "- first (#1)\n  one\n\n  two\n\n- second (#2)\n"
    # When: parse the existing body
    first, second = rn.parse_body(body, CFG).items
    # Then: only the blank line inside the item is kept
    assert first.extra == ["  one", "", "  two"]
    assert second.extra == []


def test_reconstructed_subject_neutralizes_markup_but_existing_notes_stay_verbatim() -> None:
    # Given: untrusted commit text and separately maintainer-authored Markdown
    subject = "fix: [trusted](https://evil.example) @victim <details><summary>x</summary>"
    gh = FakeGitHub(commits={("v1.0.0", "v1.0.1"): [commit("a", subject, "external")]})
    # When: reconstruct; render an existing release independently
    reconstructed = rn.build_notes("v1.0.1", "", gh, CFG)
    maintained = rn.build_notes("v1.0.1", "### Notes\n" + subject, FakeGitHub(), CFG)
    # Then: only reconstructed text is neutralized; deliberate author mentions remain
    assert r"\[trusted\](https://evil.example) `@victim` x" in reconstructed
    assert "<details>" not in reconstructed
    assert "by @external" in reconstructed
    assert subject in maintained
