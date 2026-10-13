# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
# https://www.apache.org/licenses/LICENSE-2.0
"""Verify built pages and anchors under the published repository base path."""

from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

BASE = "https://fireflyframework.github.io/firefly-forge/"
ROOT = Path("build/docs-pages/firefly-forge")


class Page(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.links = []
        self.ids = set()
        self.canonical = None
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        for attribute in ("id", "name" if tag == "a" else "id"):
            if attribute in attrs:
                self.ids.add(attrs[attribute])
        for attribute in ("href", "src"):
            if attrs.get(attribute):
                self.links.append(attrs[attribute])
        if tag == "link" and attrs.get("rel") == "canonical":
            self.canonical = attrs.get("href")


def check_site(root, base_url=BASE):
    root = root.resolve()
    pages = {path: Page(path.read_text(encoding="utf-8")) for path in root.rglob("*.html")}
    base = urlsplit(base_url)
    errors = []
    checked = 0
    for path, page in pages.items():
        relative = path.relative_to(root).as_posix()
        page_url = urljoin(base_url, relative.removesuffix("index.html"))
        if relative != "404.html" and page.canonical != page_url:
            errors.append(f"{relative}: canonical URL must be {page_url}")
        for link in page.links:
            url = urlsplit(urljoin(page_url, link))
            if (url.scheme, url.netloc) != (base.scheme, base.netloc):
                continue
            checked += 1
            if not url.path.startswith(base.path):
                errors.append(f"{relative}: URL escapes repository base path: {link}")
                continue
            target = (root / unquote(url.path[len(base.path) :])).resolve()
            if not target.is_relative_to(root):
                errors.append(f"{relative}: URL escapes site directory: {link}")
                continue
            if target.is_dir():
                target /= "index.html"
            if not target.is_file():
                errors.append(f"{relative}: missing local target: {link}")
            elif url.fragment and target.suffix == ".html":
                destination = pages.get(target)
                if destination is None or unquote(url.fragment) not in destination.ids:
                    errors.append(f"{relative}: missing local anchor: {link}")
    if not pages or errors:
        raise ValueError("\n".join(errors) or "No built HTML pages found")
    return len(pages), checked


if __name__ == "__main__":
    try:
        pages, urls = check_site(ROOT)
    except ValueError as error:
        raise SystemExit(str(error)) from error
    print(f"Verified {pages} pages and {urls} local URLs at {BASE}")
