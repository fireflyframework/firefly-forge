# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Generate public discovery metadata and local, brand-owned social cards."""

import hashlib
import html
import json
import re
import subprocess
from pathlib import Path
from urllib.parse import urljoin, urlsplit

ROOT = Path(__file__).resolve().parent.parent
_cards = {}


def discovery_data(page, config):
    site = urlsplit(config["site_url"])
    url = urlsplit(page.canonical_url)
    if (
        site.scheme != "https"
        or url.scheme != "https"
        or url.netloc != site.netloc
        or not url.path.startswith(site.path)
        or url.query
        or url.fragment
        or url.username
        or url.password
    ):
        raise ValueError("Discovery requires a public HTTPS canonical URL inside the repository site")
    title = str(page.meta.get("title", page.title)).strip()
    descriptions = config.get("extra", {}).get("discovery", {}).get("descriptions", {})
    description = str(page.meta.get("description", descriptions.get(page.file.src_uri, ""))).strip()
    if not 1 <= len(title) <= 160 or not 50 <= len(description) <= 240:
        raise ValueError(page.file.src_uri + ": each page requires a bounded title and a 50..240 character description")
    if re.search(r"[<>\x00-\x1f]", description):
        raise ValueError("Page descriptions must contain plain text")
    revision = config.get("extra", {}).get("discovery", {}).get("revision", "")
    key = hashlib.sha256(json.dumps([page.file.src_uri, title, description, revision]).encode()).hexdigest()[:24]
    image_path = "assets/social/" + key + ".png"
    return {
        "page_title": title,
        "title": title if page.canonical_url == config["site_url"] else title + " | " + config["site_name"],
        "description": description,
        "url": page.canonical_url,
        "image": urljoin(config["site_url"], image_path),
        "image_path": image_path,
        "site_name": config["site_name"],
        "schema": {
            "@context": "https://schema.org",
            "@type": "WebPage",
            "@id": page.canonical_url,
            "name": title,
            "description": description,
            "url": page.canonical_url,
            "inLanguage": "en",
            "isPartOf": {"@type": "WebSite", "name": config["site_name"], "url": config["site_url"]},
            "publisher": {"@type": "Organization", "name": config["site_author"]},
        },
    }


def render_meta(data):
    values = {
        "og:type": "website",
        "og:locale": "en_US",
        "og:site_name": data["site_name"],
        "og:title": data["title"],
        "og:description": data["description"],
        "og:url": data["url"],
        "og:image": data["image"],
        "og:image:type": "image/png",
        "og:image:width": "1200",
        "og:image:height": "630",
        "og:image:alt": data["title"],
        "twitter:card": "summary_large_image",
        "twitter:title": data["title"],
        "twitter:description": data["description"],
        "twitter:image": data["image"],
        "twitter:image:alt": data["title"],
        "robots": "index,follow,max-image-preview:large",
    }
    tags = []
    for key, value in values.items():
        attribute = "property" if key.startswith("og:") else "name"
        tags.append(f'<meta {attribute}="{key}" content="{html.escape(value, quote=True)}">')
    schema = json.dumps(data["schema"], ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e")
    return "\n".join(tags) + '\n<script type="application/ld+json">' + schema + "</script>"


def on_config(config):
    _cards.clear()
    settings = config["extra"]["discovery"]
    docs = Path(config["docs_dir"]).resolve()
    content = (ROOT / "scripts/build_docs_social.mjs").read_bytes()
    for name in ("font", "logo"):
        path = (docs / settings[name]).resolve()
        if not path.is_relative_to(docs) or not path.is_file():
            raise ValueError("Social cards require existing local brand assets inside docs_dir")
        content += path.read_bytes()
    settings["revision"] = hashlib.sha256(content).hexdigest()
    return config


def on_post_page(output, page, config):
    data = discovery_data(page, config)
    _cards[page.file.src_uri] = data
    output = re.sub(
        r"<title>.*?</title>", lambda _: "<title>" + html.escape(data["title"]) + "</title>", output, count=1
    )
    output = re.sub(
        r'<meta name="description" content="[^"]*">',
        lambda _: '<meta name="description" content="' + html.escape(data["description"], quote=True) + '">',
        output,
        count=1,
    )
    if "</head>" not in output:
        raise ValueError("Documentation page has no head for discovery metadata")
    return output.replace("</head>", render_meta(data) + "\n</head>", 1)


def on_post_template(output, template_name, config):
    del config
    if template_name == "404.html":
        return output.replace("</head>", '<meta name="robots" content="noindex,follow">\n</head>', 1)
    return output


def on_post_build(config):
    settings = config["extra"]["discovery"]
    docs = Path(config["docs_dir"]).resolve()
    inputs = {}
    for name in ("font", "logo"):
        path = (docs / settings[name]).resolve()
        if not path.is_relative_to(docs) or not path.is_file():
            raise ValueError("Social cards require existing local brand assets inside docs_dir")
        inputs[name] = str(path)
    manifest = ROOT / ("work" if (ROOT / "package.json").exists() else "build") / "docs-social-input.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        json.dumps(
            {
                **inputs,
                "site_dir": str(Path(config["site_dir"]).resolve()),
                "site_url": config["site_url"],
                "cards": list(_cards.values()),
            }
        ),
        encoding="utf-8",
    )
    subprocess.run(["node", str(ROOT / "scripts/build_docs_social.mjs"), str(manifest)], check=True, cwd=ROOT)
    rights = []
    for name in ("notice", "font_notice"):
        path = (docs / settings[name]).resolve()
        if not path.is_relative_to(docs) or not path.is_file():
            raise ValueError("Social preview rights require existing local brand and font notices")
        rights.append(path.read_text(encoding="utf-8"))
    notice = Path(config["site_dir"]) / "assets/social/NOTICE.txt"
    notice.write_text("\n\n".join(rights), encoding="utf-8")
