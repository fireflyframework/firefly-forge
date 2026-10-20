# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Bind release-note links to files under the publishing repository and tag."""

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

    parts = CODE.split(source.read_text(encoding="utf-8"))
    return "".join(part if index % 2 else LINK.sub(replace, part) for index, part in enumerate(parts))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--tag", required=True)
    args = parser.parse_args()
    args.output.write_text(render_notes(args.source, Path.cwd(), args.repository, args.tag), encoding="utf-8")
