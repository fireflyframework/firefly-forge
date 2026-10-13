# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Validate built discovery metadata, PNG previews and the complete canonical sitemap."""

import json
import struct
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree


class Head(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.meta, self.canonical, self.title = {}, None, ""
        self.schema, self.mode, self.buffer = None, None, ""
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "meta":
            key = attrs.get("property", attrs.get("name"))
            if key in self.meta:
                raise ValueError("Duplicate metadata: " + str(key))
            if key:
                self.meta[key] = attrs.get("content", "")
        if tag == "link" and attrs.get("rel") == "canonical":
            self.canonical = attrs.get("href")
        if tag == "title" or (tag == "script" and attrs.get("type") == "application/ld+json"):
            self.mode, self.buffer = tag, ""

    def handle_data(self, data):
        if self.mode:
            self.buffer += data

    def handle_endtag(self, tag):
        if self.mode == tag:
            if tag == "title":
                self.title = self.buffer
            else:
                self.schema = json.loads(self.buffer)
            self.mode = None


def local_image(root, base, value):
    url, site = urlsplit(value), urlsplit(base)
    if (url.scheme, url.netloc) != (site.scheme, site.netloc) or not url.path.startswith(site.path):
        raise ValueError("Social preview escapes the canonical repository site")
    if url.query or url.fragment:
        raise ValueError("Social preview requires a versioned local filename")
    path = (root / unquote(url.path[len(site.path) :])).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError("Social preview is missing or escapes the site directory")
    content = path.read_bytes()
    if content[:8] != b"\x89PNG\r\n\x1a\n" or len(content) < 24 or struct.unpack(">II", content[16:24]) != (1200, 630):
        raise ValueError("Social preview must be a real 1200x630 PNG")
    return path


def check_discovery(root, base):
    root = Path(root).resolve()
    canonical, titles, descriptions, images = set(), set(), set(), set()
    for path in sorted(root.rglob("*.html")):
        page = Head(path.read_text(encoding="utf-8"))
        if path.name == "404.html":
            if page.meta.get("robots") != "noindex,follow":
                raise ValueError("The 404 page must be noindex")
            continue
        description = page.meta.get("description", "")
        if not 50 <= len(description) <= 240 or not page.title or not page.canonical:
            raise ValueError(str(path.relative_to(root)) + ": missing page discovery metadata")
        if page.title in titles or description in descriptions or page.canonical in canonical:
            raise ValueError("Page titles, descriptions and canonical URLs must be unique")
        if not page.canonical.startswith(base) or page.canonical != page.meta.get("og:url"):
            raise ValueError("Open Graph must retain the exact canonical repository URL")
        if page.title != page.meta.get("og:title") or description != page.meta.get("og:description"):
            raise ValueError("Search and social metadata disagree")
        if page.meta.get("twitter:card") != "summary_large_image" or page.meta.get("twitter:image") != page.meta.get(
            "og:image"
        ):
            raise ValueError("Social cards require consistent Open Graph and Twitter previews")
        if (page.meta.get("og:image:width"), page.meta.get("og:image:height")) != ("1200", "630"):
            raise ValueError("Social card dimensions are missing")
        if not page.meta.get("og:image:alt") or not page.meta.get("twitter:image:alt"):
            raise ValueError("Social card alternative text is missing")
        if not page.schema or page.schema.get("@type") != "WebPage" or page.schema.get("url") != page.canonical:
            raise ValueError("Structured data must describe the actual documentation page")
        if page.schema.get("isPartOf", {}).get("url") != base:
            raise ValueError("Structured data site identity changed")
        image = local_image(root, base, page.meta.get("og:image", ""))
        canonical.add(page.canonical)
        titles.add(page.title)
        descriptions.add(description)
        images.add(image)
    sitemap = ElementTree.parse(root / "sitemap.xml")
    mapped = {node.text for node in sitemap.findall(".//{http://www.sitemaps.org/schemas/sitemap/0.9}loc")}
    if not canonical or mapped != canonical:
        raise ValueError("Sitemap must contain every canonical documentation page exactly")
    if len(images) != len(canonical):
        raise ValueError("Every documentation page requires its own social preview")
    return {"pages": len(canonical), "cards": len(images), "base": base}


if __name__ == "__main__":
    import sys

    print(json.dumps(check_discovery(Path(sys.argv[1]), sys.argv[2]), sort_keys=True))
