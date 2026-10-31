# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Render one release section with documentation links bound to its repository and tag."""

import argparse
import re
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit, urlunsplit

CODE = re.compile(r"(```.*?```|~~~.*?~~~|`[^`\n]*`)", re.DOTALL)
LINK = re.compile(
    r"(?P<label>!?\[[^\n]*?\]\()"
    r"(?P<destination><[^>\n]+>|(?:\\.|[^\s()\\]|\((?:\\.|[^()\\])*\))+)"
    r"(?P<title>[ \t]+(?:\"[^\"\n]*\"|'[^'\n]*'))?\)"
)


def release_section(text: str, tag: str) -> str:
    """Select an exact version heading, excluding headings inside Markdown fences."""
    headings: list[tuple[str, int]] = []
    fence: tuple[str, int] | None = None
    offset = 0
    for line in text.splitlines(keepends=True):
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line.rstrip("\r\n"))
        if fence is not None:
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= fence[1] and not marker[2].strip():
                fence = None
        elif marker:
            fence = (marker[1][0], len(marker[1]))
        else:
            heading = re.match(r"^##[ \t]+(\S+)(?:[ \t].*)?$", line.rstrip("\r\n"))
            if heading:
                headings.append((heading[1], offset))
        offset += len(line)
    if fence is not None:
        raise ValueError("Release notes contain an unclosed Markdown fence")
    selected = [index for index, (version, _) in enumerate(headings) if version == tag.removeprefix("v")]
    if len(selected) != 1:
        raise ValueError("Expected exactly one release section for " + tag)
    index = selected[0]
    start = headings[index][1]
    end = headings[index + 1][1] if index + 1 < len(headings) else len(text)
    return text[start:end].rstrip("\r\n")


def render_notes(source: Path, root: Path, repository: str, tag: str) -> str:
    root, source = root.resolve(), source.resolve()
    source.relative_to(root)
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Expected an owner/repository name")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]*", tag):
        raise ValueError("Expected a release tag")

    def replace(match: re.Match[str]) -> str:
        destination = match["destination"]
        angled = destination.startswith("<")
        uri = destination[1:-1] if angled else destination
        parsed = urlsplit(uri)
        if parsed.scheme or parsed.netloc or uri.startswith("/") or not parsed.path:
            return match[0]
        target = (source.parent / unquote(parsed.path)).resolve()
        relative = target.relative_to(root)
        if not target.is_file():
            raise ValueError("Release note target does not exist: " + parsed.path)
        base = f"https://github.com/{repository}/blob/{quote(tag, safe='')}/"
        path = base + quote(relative.as_posix(), safe="/")
        normalized = urlunsplit((*urlsplit(path)[:3], parsed.query, parsed.fragment))
        if angled:
            normalized = "<" + normalized + ">"
        start, end = match.span("destination")
        return match[0][: start - match.start()] + normalized + match[0][end - match.start() :]

    parts = CODE.split(release_section(source.read_text(encoding="utf-8"), tag))
    notes = "".join(part if index % 2 else LINK.sub(replace, part) for index, part in enumerate(parts))
    history = f"https://github.com/{repository}/blob/{quote(tag, safe='')}/" + quote(
        source.relative_to(root).as_posix(), safe="/"
    )
    return notes + f"\n\n[Full release history]({history})\n"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--tag", required=True)
    args = parser.parse_args()
    args.output.write_text(render_notes(args.source, Path.cwd(), args.repository, args.tag), encoding="utf-8")
