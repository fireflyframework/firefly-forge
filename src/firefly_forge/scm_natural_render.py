# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Bound the complete escaped AI quote before accepting its typed response."""

from collections.abc import Callable

from firefly_forge.collaboration_feedback import _text


def reply_quote(message: str, questions: list[str], safe: Callable[[str], str]) -> str:
    def line(value: str) -> str:
        return _text(value, safe, 6 * len(value) + 100, preserve_word_slashes=True)

    quote = "> " + "\n> ".join(line(value) for value in message.splitlines())
    if questions:
        quote += "\n>\n> " + "\n> ".join(f"{index}. {line(value)}" for index, value in enumerate(questions, 1))
    if len(quote) > 4000:
        raise ValueError("The complete reply and typed questions exceed the bounded public quote")
    return quote
