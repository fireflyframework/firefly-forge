# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Assistant quotations preserve human prose and line breaks without exposing paths."""

from markdown_it import MarkdownIt

from firefly_forge.collaboration_feedback import _text
from firefly_forge.communication import _assistant_content


def quote(reply):
    return _assistant_content({"reply": reply, "questions": []}, lambda value: value)


def test_assistant_quote_preserves_word_slashes_used_in_public_prose():
    reply = "Revisaremos carga/error/vacío y claro/oscuro para el recorrido API/cliente"
    assert reply in quote(reply)


def test_assistant_quote_preserves_line_breaks_for_separate_delivery_items():
    rendered = MarkdownIt().render(quote("Orden de entrega:\n1. Empresas y contactos.\n2. Oportunidades y pipeline."))
    assert rendered.count("<br />") == 2
    assert rendered.index("1.") < rendered.index("2.")


def test_assistant_quote_still_redacts_genuine_absolute_paths_and_settings():
    rendered = quote("Ruta /private/owned/client.txt y C:\\Private\\client.txt con FORGE_TOKEN=public-fixture-only")
    assert "/private/owned/client.txt" not in rendered
    assert "Private" not in rendered
    assert "public-fixture-only" not in rendered
    assert "omitted" in rendered


def test_other_feedback_retains_its_existing_path_redaction():
    assert "error/vacío" not in _text("carga/error/vacío", lambda value: value)


def test_assistant_quote_cannot_introduce_mentions_or_executable_html():
    rendered = MarkdownIt().render(quote("@everyone <script>alert(1)</script>"))
    assert "@everyone" not in rendered
    assert "<script>" not in rendered
