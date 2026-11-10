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
