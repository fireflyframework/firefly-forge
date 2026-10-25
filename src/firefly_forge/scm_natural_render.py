# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Bound complete AI quotations and preserve only server-verified delivery hrefs."""

import re
from collections.abc import Callable

from firefly_forge.collaboration_feedback import _escaped_text, _redact_settings, _text
from firefly_forge.scm_natural_links import NativeDeliveryLinkV1

_URL = r"https://[^\s<>\"\\()\[\]{}]+"
_LINK = re.compile(
    r"(?P<image>!?)\[(?P<label>[^\[\]\r\n]{1,200})\]\((?P<marked>" + _URL + r")\)|(?P<bare>" + _URL + r")"
)


def _quote(message: str, questions: list[str], line: Callable[[str], str]) -> str:
    quote = "> " + "\n> ".join(line(value) for value in message.splitlines())
    if questions:
        quote += "\n>\n> " + "\n> ".join(f"{index}. {line(value)}" for index, value in enumerate(questions, 1))
    if len(quote) > 4000:
        raise ValueError("The complete reply and typed questions exceed the bounded public quote")
    return quote


def validate_reply_quote(message: str, questions: list[str]) -> None:
    def line(value: str) -> str:
        # Count unredacted escaped text and possible bare-URL autolink delimiters.
        # A restored native href cannot make the public quote exceed this bound.
        return _escaped_text(value, 6 * len(value) + 100) + "xx" * value.count("https://")

    _quote(message, questions, line)


def reply_quote(
    message: str,
    questions: list[str],
    safe: Callable[[str], str],
    *,
    trusted_links: tuple[NativeDeliveryLinkV1, ...] = (),
) -> str:
    allowed = {link.href for link in trusted_links if safe(link.href) == link.href}

    def plain(value: str) -> str:
        rendered = _text(value, lambda text: text, 6 * len(value) + 100, preserve_word_slashes=True)
        if not rendered:
            return " " if value else ""
        return (" " if value.startswith(" ") else "") + rendered + (" " if value.endswith(" ") else "")

    def line(value: str) -> str:
        value = " ".join(_redact_settings(safe(value)).split())
        if not allowed:
            return plain(value)
        result, cursor = [], 0
        for match in _LINK.finditer(value):
            result.append(plain(value[cursor : match.start()]))
            raw = match["marked"] or match["bare"]
            href = raw if match["marked"] else raw.rstrip(".,;!:")
            if href not in allowed or match["image"]:
                result.append(plain(match[0]))
            elif match["marked"]:
                result.append("[" + plain(match["label"]) + "](" + href + ")")
            else:
                result.append("<" + href + ">" + plain(raw[len(href) :]))
            cursor = match.end()
        result.append(plain(value[cursor:]))
        return "".join(result)

    return _quote(message, questions, line)
