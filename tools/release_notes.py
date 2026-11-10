#!/usr/bin/env python3
"""Standardize GitHub Release notes and credit human contributors.

Release Please stays the owner of versions, tags, ``CHANGELOG.md`` and the
GitHub Release it creates. This tool only *rewrites the body of an existing
GitHub Release* into one stable layout and adds verified attribution. It never
creates or moves tags, never touches ``CHANGELOG.md`` and never republishes
anything.

Layout (empty sections are omitted)::

    ## vX.Y.Z (YYYY-MM-DD)
    Notes            (status annotations, e.g. "not published to PyPI")
    Highlights
    Breaking Changes (+ upgrade notes)
    Features / Bug Fixes / Performance / Reverts
    New Contributors (only when independently verified)
    <details> Documentation, maintenance and other changes </details>
    **Full Changelog**: compare link

Rules that keep the notes honest:

* Items are *preserved*, not invented. The source is the existing release body
  (Release Please, GitHub-generated or hand-written). Only a release whose body
  is compare-link-only is reconstructed from the commits in its compare range,
  and it says so.
* ``by @user`` is reserved for API-verified external human contributors.
  Co-author trailers are claims, not evidence. Bots and logins listed in
  ``non_human_logins`` are never credited; nothing is guessed.
* "New Contributors" excludes the owner. An external human is listed only
  when GitHub verifies no earlier commit or merged PR by that login.
* ``apply`` refuses to write when the new body drops a PR/commit reference the
  old body had, or loses a ``protected`` phrase from the config.

Stdlib-only on purpose, like the other release-gate tools. Network access goes
through the REST API (``GH_TOKEN`` in CI) behind the small ``GitHub`` protocol so
the logic is unit-testable with a fake.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import difflib
import http.client
import json
import os
from pathlib import Path
import re
import sys
from time import monotonic
from typing import Protocol, TypedDict
from urllib.parse import quote

Json = str | int | float | bool | None | list["Json"] | dict[str, "Json"]
MARKER = "<!-- release-notes-format: v1 -->"
CONFIG_PATH = Path(".github/release-notes.json")
MAX_LINE = 20_000
MAX_BODY = 500_000

SECTION_ORDER = ("Breaking Changes", "Features", "Bug Fixes", "Performance", "Reverts")
OTHER = "Other changes"
CONVENTIONAL_SECTION = {
    "feat": "Features",
    "fix": "Bug Fixes",
    "perf": "Performance",
    "revert": "Reverts",
}
SECTION_EMOJI = {
    "Breaking Changes": "\u26a0\ufe0f ",
    "Features": "",
    "Bug Fixes": "",
    "Performance": "",
    "Reverts": "",
}

_PLUMBING_SUBJECT = re.compile(
    r"^(chore\(main\): release|(chore|docs|build)(\([^)]*\))?: (update changelog|bump version)|"
    r"bump version to|merge (pull request|branch))",
    re.IGNORECASE,
)
_CONVENTIONAL = re.compile(
    r"^(?P<type>[a-z]+)(?:\((?P<scope>[^)]*)\))?(?P<bang>!)?:\s+(?P<desc>.+)$"
)
_RP_SCOPE = re.compile(r"^\*\*(?P<scope>[^*]+):\*\*\s+(?P<desc>.+)$")
_REFERENCE_LINK = re.compile(
    r"\(\[(?:#\d+|[0-9a-fA-F]{7,40})\]\("
    r"(?P<url>https?://github\.com/[^\s()\[\]]+)\)\)"
)
_GITHUB_REF = re.compile(
    r"https?://github\.com/(?P<repo>[^/\s]+/[^/\s]+)/"
    r"(?P<kind>pull|issues|commit)/(?P<ref>[0-9a-fA-F]+)\b"
)
_REFERENCE_GROUP = re.compile(
    r"\((?P<tokens>(?:#\d+|`[0-9a-fA-F]{7,40}`)"
    r"(?:[ \t]*,[ \t]*(?:#\d+|`[0-9a-fA-F]{7,40}`))*)\)"
)
_PARSE_TOKENS = re.compile(f"(?:{_REFERENCE_LINK.pattern})|(?:{_REFERENCE_GROUP.pattern})")
_LINK_TOKENS = re.compile(r"\\.|`+|!?\[|\]")
_MAX_LINK_TAIL = 2_048


def _balanced_parentheses(depth: int) -> str:
    inner = r"(?:[^\s()\\]|\\.)*"
    for _ in range(depth - 1):
        inner = rf"(?:[^\s()\\]|\\.|\({inner}\))*"
    return inner


# The "(destination "title")" after a link label. Every alternative inside a repetition starts
# with a different character, so a failed attempt never backtracks into itself.
_LINK_TAIL = re.compile(
    r"\([ \t]*"
    rf"(?:<(?:[^<>\n\\]|\\.)*>|(?!<){_balanced_parentheses(5)})"
    r"""(?:[ \t]+(?:"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|\((?:[^()\\]|\\.)*\)))?"""
    r"[ \t]*\)"
)
_FULL_CHANGELOG = re.compile(r"^\*{0,2}Full Changelog\*{0,2}:.*$", re.IGNORECASE)
_OUR_TAIL = re.compile(r"by (?:@[\w\-\[\]]+)(?:, @[\w\-\[\]]+)*$")
# Lines reach this pattern right-stripped; a lazy ".+?" before "\s*$" would rescan every inner
# whitespace run, which is quadratic on an adversarial heading.
_HEADING = re.compile(r"^(?P<hashes>#{2,4})\s+(?P<text>.+)$")
# A Conventional Commits footer is "token: value" or "token #value"; a token has no spaces, so
# prose such as "Migration steps: ..." is not a trailer.
_BREAKING_FOOTER = re.compile(r"BREAKING(?: CHANGE|-CHANGE):[ \t]*(.*)")
_FOOTER_TOKEN = re.compile(r"[\w-]+(?::[ \t]| #)")


# --------------------------------------------------------------------------- model


@dataclass
class Item:
    title: str
    section: str = OTHER
    prs: list[int] = field(default_factory=list)
    shas: list[str] = field(default_factory=list)
    authors: list[str] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)
    key: str = ""

    def line(self) -> str:
        refs = [f"#{n}" for n in dict.fromkeys(self.prs)] + [
            f"`{s}`" for s in dict.fromkeys(s[:7].lower() for s in self.shas)
        ]
        text = self.title + (f" ({', '.join(refs)})" if refs else "")
        if self.authors:
            text += " by " + ", ".join(f"@{a}" for a in self.authors)
        return "\n".join([f"- {text}", *self.extra])


@dataclass
class Parsed:
    date: str | None = None
    notes: list[str] = field(default_factory=list)
    highlights: list[str] = field(default_factory=list)
    upgrade: list[str] = field(default_factory=list)
    upgrade_title: str = ""
    items: list[Item] = field(default_factory=list)
    compare_prev: str | None = None
    legacy_new_contributors: bool = False


@dataclass
class Commit:
    sha: str
    subject: str
    message: str
    login: str | None
    login_is_bot: bool = False


class GitHubError(RuntimeError):
    """A GitHub API request failed."""


class ReleaseMetadata(TypedDict):
    tagName: str
    body: str
    publishedAt: str | None
    id: int


class GitHub(Protocol):
    def releases(self) -> list[tuple[str, str]]: ...

    def release_body(self, tag: str) -> str: ...

    def compare(self, base: str, head: str) -> list[Commit]: ...

    def pr_authors(self) -> dict[int, tuple[str, bool]]: ...

    def had_prior_contribution(self, login: str, before_tag: str) -> bool: ...

    def release_metadata(self, tag: str) -> ReleaseMetadata: ...

    def set_body(self, tag: str, body: str, original: str) -> None: ...


@dataclass
class Config:
    repo: str
    owner_login: str | None = None
    legacy_repos: list[str] = field(default_factory=list)
    non_human_logins: set[str] = field(default_factory=set)
    protected: dict[str, list[str]] = field(default_factory=dict)
    # Evidence-backed clarifications per tag: {tag: {"notes"|"highlights"|"upgrade": [lines]}}
    annotations: dict[str, dict[str, list[str]]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path = CONFIG_PATH) -> Config:
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            repo=data["repo"],
            owner_login=data.get("owner_login"),
            legacy_repos=list(data.get("legacy_repos", [])),
            non_human_logins={x.lower() for x in data.get("non_human_logins", [])},
            protected={k: list(v) for k, v in data.get("protected", {}).items()},
            annotations={
                tag: {block: list(lines) for block, lines in blocks.items()}
                for tag, blocks in data.get("annotations", {}).items()
            },
        )


# --------------------------------------------------------------------------- parsing


def _normalize_heading(text: str) -> str:
    return re.sub(r"[^a-z' ]+", "", text.lower()).strip()


def _classify_heading(text: str) -> tuple[str, str | None]:
    """Return (mode, section). Modes: highlights/notes/upgrade/skip/items."""
    h = _normalize_heading(text)
    if "highlight" in h:
        return "highlights", None
    if "new contributor" in h:
        return "skip", None
    if "upgrad" in h or "migrat" in h:
        return "upgrade", None
    if (
        h in {"notes", "note", "status", "release status"}
        or h.endswith(" notes")
        and "release" not in h
    ):
        return "notes", None
    if "break" in h:
        return "items", "Breaking Changes"
    if any(k in h for k in ("feature", "added", "enhancement")) or h == "new":
        return "items", "Features"
    if "fix" in h or "bug" in h:
        return "items", "Bug Fixes"
    if "perf" in h:
        return "items", "Performance"
    if "revert" in h:
        return "items", "Reverts"
    if "doc" in h:
        return "items", "Documentation"
    return "items", None


def _clean_title(raw: str) -> tuple[str, str | None, bool]:
    """Return (display title, conventional type, breaking-bang)."""
    scope_match = _RP_SCOPE.match(raw)
    if scope_match:
        return f"**{scope_match['scope']}:** {scope_match['desc']}", None, False
    conv = _CONVENTIONAL.match(raw)
    if conv and conv["type"] in {
        "feat", "fix", "perf", "revert", "docs", "refactor", "style", "test", "ci",
        "chore", "build",
    }:  # fmt: skip
        title = f"**{conv['scope']}:** {conv['desc']}" if conv["scope"] else conv["desc"]
        return title, conv["type"], bool(conv["bang"])
    return raw, None, False


def _check_size(text: str) -> None:
    if len(text) > MAX_BODY:
        raise ValueError("release body exceeds 500000 character limit")
    if any(len(line) > MAX_LINE for line in text.splitlines()):
        raise ValueError("release line exceeds 20000 character limit")


def _local_ref(url: str, cfg: Config | None) -> tuple[str, str] | None:
    match = _GITHUB_REF.fullmatch(url)
    if match is None or cfg is None:
        return None
    if match["repo"].lower() not in {r.lower() for r in [cfg.repo, *cfg.legacy_repos]}:
        return None
    kind, ref = match["kind"], match["ref"]
    if kind == "commit":
        return (kind, ref[:7].lower()) if 7 <= len(ref) <= 40 else None
    return (kind, ref) if ref.isdecimal() else None


def _code_span_end(line: str, start: int, length: int) -> int | None:
    """Return where the code span opened by ``length`` backticks ends, or None if it never closes.

    Only a run of exactly ``length`` backticks closes the span, and a backslash inside a code span
    is literal, so the closer is searched in the raw text rather than among escape tokens.
    """
    marker = "`" * length
    while (at := line.find(marker, start)) >= 0:
        end = at + length
        while end < len(line) and line[end] == "`":
            end += 1
        if end - at == length:
            return end
        start = end
    return None


def _link_spans(line: str) -> list[tuple[int, int]]:
    """Return the ``(start, end)`` of each inline Markdown link or image on a line.

    A forward pass reads the line the way CommonMark does: a backslash escapes the next
    character, a code span hides the brackets inside it, ``[`` (or ``![``) opens and ``]`` closes
    a label that may nest to any depth, and the text after a closing ``]`` is read once as
    ``(destination "title")``. A link cannot contain another link: once one is found, the
    ``[`` openers before it are text, while an image may sit inside a link. Each character is
    read a bounded number of times, so the work is linear in the length of the line.
    """
    openers: list[tuple[int, bool]] = []
    dead = 0
    found: list[tuple[int, int]] = []
    position = 0
    while token := _LINK_TOKENS.search(line, position):
        text = token[0]
        position = token.end()
        if text in ("[", "!["):
            openers.append((token.start(), text == "!["))
        elif text == "]":
            if not openers:
                continue
            start, is_image = openers.pop()
            usable = is_image or len(openers) >= dead
            dead = min(dead, len(openers))
            tail = _LINK_TAIL.match(line, position, position + _MAX_LINK_TAIL) if usable else None
            if tail:
                found.append((start, tail.end()))
                position = tail.end()
                if not is_image:
                    dead = len(openers)
        elif text[0] == "`":
            position = _code_span_end(line, position, len(text)) or position
    spans: list[tuple[int, int]] = []
    for start, end in sorted(found):
        if spans and start < spans[-1][1]:
            spans[-1] = (spans[-1][0], max(spans[-1][1], end))
        else:
            spans.append((start, end))
    return spans


def _outside_links(line: str) -> str:
    """Replace every inline link with one space, so text on either side is never joined."""
    kept: list[str] = []
    position = 0
    for start, end in _link_spans(line):
        kept += (line[position:start], " ")
        position = end
    kept.append(line[position:])
    return "".join(kept)


def _protect_links(text: str) -> str:
    """Blank every link except a Release Please ``([#N](url))`` wrapper; offsets are unchanged."""
    kept: list[str] = []
    position = 0
    for start, end in _link_spans(text):
        if start > 0 and _REFERENCE_LINK.fullmatch(text[start - 1 : end + 1]):
            continue
        kept += (text[position:start], "\x00" * (end - start))
        position = end
    kept.append(text[position:])
    return "".join(kept)


def parse_item(text: str, section: str | None, cfg: Config | None = None) -> Item:
    _check_size(text)
    prs: list[int] = []
    shas: list[str] = []
    text = text.strip()
    text = _OUR_TAIL.sub("", text).rstrip()

    def take(kind: str, ref: str) -> None:
        if kind == "commit":
            shas.append(ref[:7].lower())
        else:
            prs.append(int(ref))

    def token(match: re.Match[str]) -> str:
        if match["url"]:
            local = _local_ref(match["url"], cfg)
            if local is None:
                return match[0]
            take(*local)
            return ""
        for ref in match["tokens"].split(","):
            ref = ref.strip()
            take("pull" if ref.startswith("#") else "commit", ref.strip("#`"))
        return ""

    parts: list[str] = []
    position = 0
    for match in _PARSE_TOKENS.finditer(_protect_links(text)):
        parts += (text[position : match.start()], token(match))
        position = match.end()
    parts.append(text[position:])
    text = "".join(parts)
    before, separator, url = text.rpartition(" in ")
    local = _local_ref(url, cfg) if separator else None
    if local is not None:
        take(*local)
        text = _OUR_TAIL.sub("", before.rstrip()).rstrip()
    text = " ".join(text.split())
    title, ctype, bang = _clean_title(text)
    key = re.sub(r"^\*\*[^*]+:\*\*\s+", "", title).strip().lower()
    resolved = section
    if bang:
        resolved = "Breaking Changes"
    elif resolved in (None, OTHER) and ctype:
        resolved = CONVENTIONAL_SECTION.get(ctype, OTHER)
    return Item(
        title=title,
        section=resolved or OTHER,
        prs=list(dict.fromkeys(prs)),
        shas=list(dict.fromkeys(shas)),
        key=key,
    )


def parse_body(body: str, cfg: Config | None = None) -> Parsed:
    """Split an existing release body into verbatim blocks and structured items."""
    _check_size(body)
    parsed = Parsed()
    body = _drop_spans(body, "<!--", "-->")
    mode, section = "items", None
    verbatim_level = 0
    last: Item | None = None
    blank_before = False
    for line in body.splitlines():
        stripped = line.rstrip()
        after_blank, blank_before = blank_before, not stripped
        if not stripped:
            if mode in {"highlights", "notes", "upgrade"}:
                getattr(parsed, mode).append("")
            continue
        if re.fullmatch(r"(-{3,}|\*{3,}|_{3,})", stripped.strip()):
            continue
        if stripped.strip().startswith(("<details", "</details")):
            mode, section, last = "items", None, None
            continue
        if _FULL_CHANGELOG.match(stripped.strip()):
            compare = re.search(r"compare/([^\s)]{1,255}?)\.\.\.", stripped)
            if compare:
                parsed.compare_prev = compare[1]
            continue
        heading = _HEADING.match(stripped)
        if heading:
            level = len(heading["hashes"])
            if mode in {"highlights", "notes", "upgrade"} and level > verbatim_level:
                target = getattr(parsed, mode)
                target.append(stripped)
                continue
            text = heading["text"]
            rp = re.match(
                r"^\[?v?(\d+\.\d+\.\d+(?:[^\]\s()\d][^\]\s()]*)?)\]?"
                r"(?:\([^)]*\))?\s*(?:\((\d{4}-\d{2}-\d{2})\))?$",
                text,
            )
            if level == 2 and rp:
                parsed.date = rp[2]
                mode, section = "items", None
                last = None
                continue
            mode, section = _classify_heading(text)
            if mode == "upgrade" and not parsed.upgrade_title:
                parsed.upgrade_title = text
            verbatim_level = level
            last = None
            continue
        if mode in {"highlights", "notes", "upgrade"}:
            getattr(parsed, mode).append(stripped)
            continue
        if mode == "skip":
            continue
        bullet = re.match(r"^[-*]\s+(?P<text>.+)$", stripped)
        if bullet:
            last = parse_item(bullet["text"], section, cfg)
            parsed.items.append(last)
        elif line.startswith((" ", "\t")) and last is not None:
            if after_blank and (not last.extra or last.extra[-1]):
                last.extra.append("")
            last.extra.append(stripped)
        else:
            parsed.notes.append(stripped)
    for block in (parsed.notes, parsed.highlights, parsed.upgrade):
        while block and not block[-1].strip():
            block.pop()
        del block[: next((i for i, text in enumerate(block) if text.strip()), len(block))]
    return parsed


# --------------------------------------------------------------------------- attribution


def _is_human(login: str | None, is_bot: bool, cfg: Config) -> bool:
    if not login or is_bot:
        return False
    low = login.lower()
    return not (low.endswith("[bot]") or low.startswith("app/") or low in cfg.non_human_logins)


def _is_external_human(login: str | None, is_bot: bool, cfg: Config) -> bool:
    """Credit verified humans other than this repository's owner."""
    return (
        login is not None
        and _is_human(login, is_bot, cfg)
        and (cfg.owner_login is None or login.lower() != cfg.owner_login.lower())
    )


def _commit_key(subject: str) -> str:
    conv = _CONVENTIONAL.match(subject)
    return (conv["desc"] if conv else subject).strip().lower()


def attribute(
    item: Item,
    commits: list[Commit],
    pr_authors: dict[int, tuple[str, bool]],
    cfg: Config,
) -> None:
    people: list[str] = []

    def add(login: str | None, is_bot: bool = False) -> None:
        if login and _is_external_human(login, is_bot, cfg) and login not in people:
            people.append(login)

    for pr in item.prs:
        known = pr_authors.get(pr)
        if known:
            add(*known)
    matched = [
        c
        for c in commits
        if any(c.sha.startswith(s) for s in item.shas)
        or any(re.search(rf"\(#{pr}\)\s*$", c.subject) for pr in item.prs)
    ]
    if not item.prs and not item.shas and item.key:
        matched += [c for c in commits if _commit_key(c.subject) == item.key]
    for commit in matched[:1]:
        if not item.prs or not people:
            add(commit.login, commit.login_is_bot)
    item.authors = people


def new_contributors(
    commits: list[Commit],
    gh: GitHub,
    prev_tag: str | None,
    cfg: Config,
    pr_authors: dict[int, tuple[str, bool]] | None = None,
) -> list[tuple[str, str]]:
    """External humans whose first merged contribution is in this range.

    ``commits`` come from the compare API in chronological order, so the first
    commit seen per login is the earliest. A login is celebrated only when GitHub
    shows no commit or merged PR by it before ``prev_tag``. Returns
    ``(login, reference)`` where the reference is ``#PR`` or a short SHA.
    """
    if prev_tag is None:
        return []
    first_seen: dict[str, str] = {}
    for commit in commits:
        if _PLUMBING_SUBJECT.match(commit.subject):
            continue
        pr = re.search(r"\(#(\d+)\)\s*$", commit.subject)
        ref = f"#{pr[1]}" if pr else commit.sha[:7]
        people: list[str] = []
        if pr and pr_authors:
            known = pr_authors.get(int(pr[1]))
            if known and _is_external_human(known[0], known[1], cfg):
                people.append(known[0])
        if commit.login and _is_external_human(commit.login, commit.login_is_bot, cfg):
            people.append(commit.login)
        for login in people:
            if login:
                first_seen.setdefault(login, ref)
    return [
        (login, ref)
        for login, ref in first_seen.items()
        if not gh.had_prior_contribution(login, prev_tag)
    ]


# --------------------------------------------------------------------------- rendering


def render(
    tag: str,
    parsed: Parsed,
    cfg: Config,
    prev_tag: str | None,
    firsts: list[tuple[str, str]],
    reconstructed_from: str | None = None,
) -> str:
    def canon(text: str) -> str:
        for old in cfg.legacy_repos:
            text = text.replace(f"github.com/{old}/", f"github.com/{cfg.repo}/")
        return text

    out: list[str] = [f"## {tag}" + (f" ({parsed.date})" if parsed.date else ""), ""]
    notes = list(parsed.notes)
    if reconstructed_from:
        notes.append(
            f"_The original release text contained only a compare link. This summary was "
            f"reconstructed from the commits in `{reconstructed_from}`._"
        )
    if notes:
        out += ["### Notes", "", *map(canon, notes), ""]
    if parsed.highlights:
        out += ["### Highlights", "", *map(canon, parsed.highlights), ""]
    by_section: dict[str, list[Item]] = {}
    for item in parsed.items:
        by_section.setdefault(item.section, []).append(item)
    for name in SECTION_ORDER:
        section_items = by_section.pop(name, [])
        if name == "Breaking Changes" and (section_items or parsed.upgrade):
            out += [f"### {SECTION_EMOJI[name]}{name}", ""]
            out += [i.line() for i in section_items]
            if parsed.upgrade:
                title = parsed.upgrade_title or "Upgrade notes"
                out += ["", f"#### {title}", "", *map(canon, parsed.upgrade)]
            out.append("")
        elif section_items:
            out += [f"### {name}", "", *(i.line() for i in section_items), ""]
    if firsts:
        out += ["### New Contributors", ""]
        for login, ref in firsts:
            out.append(f"- @{login} made their first contribution in {ref}")
        out.append("")
    rest = [i for name, group in by_section.items() for i in group]
    classified = any(
        by_section_name in SECTION_ORDER for by_section_name in (i.section for i in parsed.items)
    )
    if rest and not classified:
        out += ["### Changes", "", *(i.line() for i in rest), ""]
    elif rest:
        out += [
            f"<details><summary>Documentation, maintenance and other changes ({len(rest)})"
            "</summary>",
            "",
            *(i.line() for i in rest),
            "",
            "</details>",
            "",
        ]
    if not parsed.items and not (parsed.highlights or notes or parsed.upgrade):
        out += [
            "_No user-facing changes could be reconstructed from the release history; "
            "see the full changelog for the exact commits._",
            "",
        ]
    if prev_tag:
        out.append(f"**Full Changelog**: https://github.com/{cfg.repo}/compare/{prev_tag}...{tag}")
    out += ["", MARKER]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).rstrip() + "\n"


# --------------------------------------------------------------------------- orchestration


def _version_key(tag: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", tag))


def previous_tag(
    tag: str, releases: list[tuple[str, str]], parsed: Parsed | None = None
) -> str | None:
    ordered = sorted((t for t, _ in releases), key=_version_key)
    if tag in ordered and ordered.index(tag) > 0:
        return ordered[ordered.index(tag) - 1]
    return parsed.compare_prev if parsed else None


def _commit_items(commits: list[Commit]) -> list[Item]:
    items: list[Item] = []
    for commit in commits:
        if _PLUMBING_SUBJECT.match(commit.subject):
            continue
        _check_size(commit.message)
        subject = re.sub(r"\(#\d+\)$", "", commit.subject.rstrip()).rstrip()
        pr = re.search(r"\(#(\d+)\)\s*$", commit.subject)
        item = parse_item(subject, None)
        item.title = _plain_commit_text(item.title)
        item.shas = [commit.sha[:7].lower()]
        item.prs = [int(pr[1])] if pr else []
        in_breaking = False
        for line in commit.message.splitlines()[1:]:
            breaking = _BREAKING_FOOTER.match(line)
            if breaking:
                in_breaking = True
                item.section = "Breaking Changes"
                if breaking[1].strip():
                    item.extra.append("  " + _plain_commit_text(breaking[1].rstrip()))
            elif _FOOTER_TOKEN.match(line):
                in_breaking = False
            elif in_breaking and line.strip():
                item.extra.append("  " + _plain_commit_text(line.rstrip()))
            elif in_breaking and item.extra and item.extra[-1]:
                item.extra.append("")
        while item.extra and not item.extra[-1]:
            item.extra.pop()
        items.append(item)
    return items


def _drop_spans(text: str, opener: str, closer: str) -> str:
    """Delete every ``opener``...``closer`` span in one forward pass; an unclosed opener stays.

    A lazy regex such as ``<!--.*?-->`` rescans the whole tail for every unclosed opener, which
    is quadratic. Once ``find`` misses a closer, no later opener can have one, so the scan ends.
    """
    kept: list[str] = []
    position = 0
    while (start := text.find(opener, position)) >= 0:
        end = text.find(closer, start + len(opener))
        if end < 0:
            break
        kept.append(text[position:start])
        position = end + len(closer)
    kept.append(text[position:])
    return "".join(kept)


def _plain_commit_text(text: str) -> str:
    text = _drop_spans(text, "<", ">")
    text = text.replace("[", r"\[").replace("]", r"\]")
    return re.sub(r"(?<![\w`])@[A-Za-z0-9_-]+", lambda m: f"`{m[0]}`", text)


def apply_annotations(parsed: Parsed, blocks: dict[str, list[str]]) -> None:
    """Append maintainer-supplied clarifications once (re-rendering never duplicates)."""
    for name in ("notes", "highlights", "upgrade"):
        lines = blocks.get(name, [])
        target: list[str] = getattr(parsed, name)
        if lines and not any(
            target[i : i + len(lines)] == lines for i in range(len(target) - len(lines) + 1)
        ):
            target.extend(["", *lines] if target else lines)


def build_notes(
    tag: str,
    original: str,
    gh: GitHub,
    cfg: Config,
    published: str | None = None,
) -> str:
    _check_size(original)
    releases = gh.releases()
    parsed = parse_body(original, cfg)
    prev = previous_tag(tag, releases, parsed)
    if parsed.date is None and published:
        parsed.date = published[:10]
    commits = gh.compare(prev, tag) if prev else []
    reconstructed = None
    if not parsed.items and not (parsed.highlights or parsed.notes or parsed.upgrade) and commits:
        parsed.items = _commit_items(commits)
        reconstructed = f"{prev}...{tag}"
    apply_annotations(parsed, cfg.annotations.get(tag, {}))
    pr_authors = gh.pr_authors()
    for item in parsed.items:
        attribute(item, commits, pr_authors, cfg)
    firsts = new_contributors(commits, gh, prev, cfg, pr_authors)
    body = render(tag, parsed, cfg, prev, firsts, reconstructed)
    problems = verify(original, body, tag, cfg)
    if problems:
        raise ValueError(f"{tag}: refusing to produce notes: " + "; ".join(problems))
    return body


def verify(original: str, new: str, tag: str, cfg: Config) -> list[str]:
    _check_size(original)
    _check_size(new)
    problems: list[str] = []
    stripped = _drop_spans(original, "<!--", "-->")
    # Unverified "New Contributors" claims are intentionally replaced, not preserved.
    stripped = re.sub(
        r"^#{2,4}[^\n]*New Contributors[^\n]*\n(?:(?!#{2,4} |\*{0,2}Full Changelog).*\n?)*",
        "",
        stripped,
        flags=re.IGNORECASE | re.MULTILINE,
    )
    old_prs, old_shas = _references(stripped, cfg)
    new_prs, new_shas = _references(new, cfg)
    for pr in sorted(old_prs - new_prs):
        problems.append(f"lost PR reference #{pr}")
    for sha in sorted(old_shas - new_shas):
        problems.append(f"lost commit reference {sha}")
    for phrase in cfg.protected.get(tag, []):
        if phrase not in new:
            problems.append(f"protected phrase missing: {phrase!r}")
    return sorted(set(problems))


def _references(body: str, cfg: Config) -> tuple[set[int], set[str]]:
    prs: set[int] = set()
    shas: set[str] = set()
    for match in _GITHUB_REF.finditer(body):
        local = _local_ref(match[0], cfg)
        if local is not None:
            kind, ref = local
            if kind == "commit":
                shas.add(ref)
            else:
                prs.add(int(ref))
    # Only a link's URL identifies it; its label is display text, so a foreign link labelled
    # "#12" must not stand in for local PR 12. Each link becomes a space, never nothing, so
    # "#" before a link and "12" after it cannot join into a reference.
    plain = "\n".join(_outside_links(line) for line in body.splitlines())
    prs.update(int(m[1]) for m in re.finditer(r"(?<![\w/])#(\d+)\b", plain))
    shas.update(m[1][:7].lower() for m in re.finditer(r"`([0-9a-fA-F]{7,40})`", plain))
    return prs, shas


# --------------------------------------------------------------------------- gh client


class GitHubApi:
    """GitHub REST API implementation of :class:`GitHub` (token from the environment).

    Uses ``http.client`` and an explicit ``GH_TOKEN``/``GITHUB_TOKEN`` instead of
    spawning the ``gh`` CLI, so the tool needs no subprocess. Locally:
    ``GH_TOKEN=$(gh auth token) python3 -m tools.release_notes ...``.
    """

    HOST = "api.github.com"

    def __init__(self, repo: str) -> None:
        self.repo = repo
        self._token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
        self._prs: dict[int, tuple[str, bool]] | None = None
        self._cache: dict[str, Json] = {}
        self._veterans: set[str] = set()
        self._release_cache: list[dict[str, Json]] | None = None
        self._deadline = monotonic() + 120

    def _remaining(self) -> float:
        remaining = self._deadline - monotonic()
        if remaining <= 0:
            raise GitHubError("120 second command deadline exceeded")
        return remaining

    def _request(self, method: str, path: str, payload: dict[str, Json] | None = None) -> Json:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "release-notes-tool",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        body = json.dumps(payload) if payload is not None else None
        conn = http.client.HTTPSConnection(self.HOST, timeout=min(30, self._remaining()))
        try:
            conn.request(method, path, body=body, headers=headers)
            response = conn.getresponse()
            raw = response.read().decode("utf-8")
        except (OSError, http.client.HTTPException, UnicodeError) as err:
            raise GitHubError("GitHub transport failed") from err
        finally:
            conn.close()
        self._remaining()
        if response.status not in ({200} if method == "GET" else {200, 201}):
            raise GitHubError(f"{method} {path} -> HTTP {response.status}")
        try:
            data: Json = json.loads(raw)
        except ValueError as err:
            raise GitHubError("GitHub returned invalid JSON") from err
        return data

    def _get(self, path: str) -> Json:
        self._remaining()
        if path not in self._cache:
            self._cache[path] = self._request("GET", path)
        return self._cache[path]

    def _pages(self, path: str) -> list[dict[str, Json]]:
        joiner = "&" if "?" in path else "?"
        items: list[dict[str, Json]] = []
        for page in range(1, 51):
            self._remaining()
            batch = _object_list(self._request("GET", f"{path}{joiner}per_page=100&page={page}"))
            items.extend(batch)
            if len(batch) < 100:
                return items
        raise GitHubError("GitHub pagination exceeds 50 page limit")

    def _all_releases(self) -> list[dict[str, Json]]:
        if self._release_cache is None:
            self._release_cache = self._pages(f"/repos/{self.repo}/releases")
            for release in self._release_cache:
                if type(release.get("draft")) is not bool:
                    raise GitHubError("release draft must be a boolean")
                _string(release.get("tag_name"))
                _string(release.get("published_at") or release.get("created_at"))
        return self._release_cache

    def releases(self) -> list[tuple[str, str]]:
        return [
            (_string(r.get("tag_name")), _string(r.get("published_at") or r.get("created_at")))
            for r in self._all_releases()
            if not r["draft"]
        ]

    def release_body(self, tag: str) -> str:
        return self.release_metadata(tag)["body"]

    def release_metadata(self, tag: str) -> ReleaseMetadata:
        for release in self._all_releases():
            if release["tag_name"] == tag:
                identifier = release.get("id")
                if type(identifier) is not int:
                    raise GitHubError("release id must be an integer")
                published = release.get("published_at")
                return {
                    "id": identifier,
                    "tagName": tag,
                    "body": _string(release.get("body") or ""),
                    "publishedAt": _string(published) if published is not None else None,
                }
        raise GitHubError(f"no release for {tag}")

    def compare(self, base: str, head: str) -> list[Commit]:
        commits: list[Commit] = []
        path = f"/repos/{self.repo}/compare/{quote(base, safe='')}...{quote(head, safe='')}"
        for page in range(1, 51):
            self._remaining()
            data = _object(self._get(f"{path}?per_page=100&page={page}"))
            batch = _object_list(data.get("commits"))
            for raw in batch:
                msg = _string(_object(raw.get("commit")).get("message"))
                author = _object(raw["author"]) if raw.get("author") is not None else {}
                login = author.get("login")
                commits.append(
                    Commit(
                        sha=_string(raw.get("sha")),
                        subject=msg.splitlines()[0] if msg else "",
                        message=msg,
                        login=_string(login) if login is not None else None,
                        login_is_bot=author.get("type") == "Bot",
                    )
                )
            if len(batch) < 100:
                return commits
        raise GitHubError("GitHub compare exceeds 50 page limit")

    def pr_authors(self) -> dict[int, tuple[str, bool]]:
        if self._prs is None:
            self._prs = {}
            for p in self._pages(f"/repos/{self.repo}/pulls?state=closed"):
                if "merged_at" not in p or "user" not in p:
                    raise GitHubError("PR response lacks merge or author evidence")
                if p.get("merged_at") and p.get("user"):
                    user = _object(p["user"])
                    number = p.get("number")
                    if type(number) is not int:
                        raise GitHubError("PR number must be an integer")
                    self._prs[number] = (_string(user.get("login")), user.get("type") == "Bot")
        return self._prs

    def had_prior_contribution(self, login: str, before_tag: str) -> bool:
        """True when GitHub shows an earlier contribution, or when it cannot tell.

        Failing closed means an unverifiable login is never celebrated as new.
        """
        if login in self._veterans:
            return True  # an earlier contribution is also earlier than any later tag
        try:
            ref = _object(self._get(f"/repos/{self.repo}/commits/{quote(before_tag, safe='')}"))
            committer = _object(_object(ref.get("commit")).get("committer"))
            before = _string(committer.get("date"))
            found = bool(
                _object_list(
                    self._get(
                        f"/repos/{self.repo}/commits?author={quote(login, safe='')}"
                        f"&until={quote(before, safe='')}&per_page=1"
                    )
                )
            )
            if not found:
                query = quote(
                    f"repo:{self.repo} is:pr is:merged author:{login} merged:<{before}", safe=""
                )
                result = _object(self._get(f"/search/issues?q={query}"))
                count = result.get("total_count")
                if result.get("incomplete_results") is not False or type(count) is not int:
                    return True
                if count < 0:
                    return True
                found = count > 0
        except (GitHubError, KeyError, ValueError, OSError):
            return True
        if found:
            self._veterans.add(login)
        return found

    def set_body(self, tag: str, body: str, original: str) -> None:
        _check_size(body)
        release = _object(
            self._request("GET", f"/repos/{self.repo}/releases/tags/{quote(tag, safe='')}")
        )
        if (release.get("body") or "") != original:
            raise GitHubError(
                "release body changed since the build snapshot; refusing concurrent edit"
            )
        identifier = release.get("id")
        if type(identifier) is not int:
            raise GitHubError("release id must be an integer")
        _object(self._request("PATCH", f"/repos/{self.repo}/releases/{identifier}", {"body": body}))


def _object(value: Json) -> dict[str, Json]:
    if not isinstance(value, dict):
        raise GitHubError("GitHub response must be an object")
    return value


def _object_list(value: Json) -> list[dict[str, Json]]:
    if not isinstance(value, list) or len(value) > 100:
        raise GitHubError("GitHub response must be a list of at most 100 objects")
    return [_object(item) for item in value]


def _string(value: Json) -> str:
    if not isinstance(value, str):
        raise GitHubError("GitHub response field must be a string")
    return value


# --------------------------------------------------------------------------- CLI


def _backup_tags(directory: Path) -> list[Path]:
    return sorted((p for p in directory.glob("v*.json")), key=lambda p: _version_key(p.stem))


def cmd_preview(args: argparse.Namespace, cfg: Config, gh: GitHub) -> int:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    failures = 0
    summary: list[str] = []
    for path in _backup_tags(Path(args.backup)):
        data = json.loads(path.read_text(encoding="utf-8"))
        tag = data["tagName"]
        old = data["body"] or ""
        try:
            new = build_notes(tag, old, gh, cfg, data.get("publishedAt"))
        except ValueError as err:
            failures += 1
            summary.append(f"FAIL {tag}: {err}")
            continue
        (out / f"{tag}.md").write_text(new, encoding="utf-8")
        diff = "".join(
            difflib.unified_diff(
                old.splitlines(True), new.splitlines(True), f"{tag} (current)", f"{tag} (new)"
            )
        )
        (out / f"{tag}.diff").write_text(diff, encoding="utf-8")
        credited = sorted(
            {a for m in re.findall(r"by (@[\w\-]+(?:, @[\w\-]+)*)", new) for a in m.split(", ")}
        )
        summary.append(
            f"ok   {tag}: {len(old)} -> {len(new)} chars; credits: {', '.join(credited) or '-'}"
        )
    (out / "SUMMARY.txt").write_text("\n".join(summary) + "\n", encoding="utf-8")
    print("\n".join(summary))
    return 1 if failures else 0


def cmd_render(args: argparse.Namespace, cfg: Config, gh: GitHub) -> int:
    published = dict(gh.releases()).get(args.tag)
    original = (
        Path(args.body_file).read_text(encoding="utf-8")
        if args.body_file
        else gh.release_body(args.tag)
    )
    sys.stdout.write(build_notes(args.tag, original, gh, cfg, published))
    return 0


def cmd_apply(args: argparse.Namespace, cfg: Config, gh: GitHub) -> int:
    original = gh.release_body(args.tag)
    if MARKER in original and not args.force:
        action = "would be skipped" if args.dry_run else "nothing to do"
        print(f"{args.tag}: already standardized; {action}")
        return 0
    published = dict(gh.releases()).get(args.tag)
    new = build_notes(args.tag, original, gh, cfg, published)
    if args.dry_run:
        sys.stdout.write(new)
        return 0
    if args.backup:
        directory = Path(args.backup)
        directory.mkdir(parents=True, exist_ok=True)
        metadata = gh.release_metadata(args.tag)
        metadata["body"] = original
        with (directory / f"{quote(args.tag, safe='')}.json").open("x", encoding="utf-8") as backup:
            json.dump(metadata, backup, indent=2)
    gh.set_body(args.tag, new, original)
    print(f"{args.tag}: release notes updated")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    parser.add_argument("--config", default=str(CONFIG_PATH))
    sub = parser.add_subparsers(dest="command", required=True)
    p_prev = sub.add_parser("preview", help="render every backed-up release into --out")
    p_prev.add_argument("--backup", required=True, help="dir of vX.Y.Z.json release dumps")
    p_prev.add_argument("--out", required=True)
    p_render = sub.add_parser("render", help="print standardized notes for one tag")
    p_render.add_argument("tag")
    p_render.add_argument("--body-file")
    p_apply = sub.add_parser("apply", help="rewrite one GitHub Release body (idempotent)")
    p_apply.add_argument("tag")
    p_apply.add_argument("--dry-run", action="store_true")
    p_apply.add_argument("--force", action="store_true", help="re-render even if already done")
    p_apply.add_argument("--backup", help="dir to save the previous body before editing")
    args = parser.parse_args(argv)
    cfg = Config.load(Path(args.config))
    gh = GitHubApi(cfg.repo)
    handler = {"preview": cmd_preview, "render": cmd_render, "apply": cmd_apply}[args.command]
    try:
        return handler(args, cfg, gh)
    except (GitHubError, ValueError, OSError) as err:
        print(f"release notes: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
