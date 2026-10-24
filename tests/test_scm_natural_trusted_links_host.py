# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Controlled native-context facts permit exact delivery hrefs, never arbitrary model links."""

import copy

import pytest
from markdown_it import MarkdownIt
from test_scm_polling import poll_case as poll_case

from firefly_forge.scm_natural_feedback import natural_card
from firefly_forge.scm_natural_models import NaturalDecision

HOST = "https://github.com"
REPO = {"id": "a" * 32, "provider": "github", "url": HOST + "/team/factory-acceptance-product.git"}
PR = HOST + "/team/factory-acceptance-product/pull/5"
REVISION = "c5a383909352952893207e40aa4e0f4e165b8792"
COMMIT = HOST + "/team/factory-acceptance-product/commit/" + REVISION


def fixture(message):
    scope = {
        "repository_id": REPO["id"],
        "project_id": "b" * 32,
        "issue": {"kind": "issue", "number": 4},
        "actor": "7",
        "binding_digest": "d" * 64,
        "native_user_id": "e" * 32,
    }
    return {
        "provider": "github",
        "repository_id": REPO["id"],
        "actor": "7",
        "issue": scope["issue"],
        "command": {"language": "es"},
        "natural_turn": {
            "state": "status",
            "reply": message,
            "decision": {"intent": "status", "language": "es", "questions": []},
            "context": {
                "scope": scope,
                "candidates": [
                    {
                        **scope,
                        "run_id": "f" * 32,
                        "delivery": {
                            "evidence": "verified_native_journal",
                            "checkpoint": {"revision": REVISION, "checkpoint_digest": "1" * 64},
                            "remote": {"status": "draft", "pr_url": PR},
                            "product_acceptance": "not_established_by_checkpoint",
                        },
                    }
                ],
            },
        },
    }


def links(body):
    return [
        token.attrGet("href")
        for block in MarkdownIt().parse(body)
        for token in (block.children or [])
        if token.type == "link_open"
    ]


def card(data, safe=lambda value: value):
    return natural_card(data, safe, repository=REPO, host=HOST)


def test_exact_native_pr_and_commit_markdown_links_are_clickable_without_product_completion_claim():
    message = f"Avance guardado, todavía sin aceptación completa:\n[PR #5]({PR}) y [commit guardado]({COMMIT})."
    body = card(fixture(message))
    assert links(body) == [PR, COMMIT]
    assert "Avance guardado" in body and "sin aceptación completa" in body
    assert "\\[PR" not in body
    assert "</a> y <a " in MarkdownIt().render(body)


def test_bare_native_urls_and_trailing_sentence_punctuation_render_as_links():
    body = card(fixture(f"Revisa {PR}.\nCódigo: {COMMIT}!"))
    assert links(body) == [PR, COMMIT]
    assert "Código:" in body and "Revisa <" in body


@pytest.mark.parametrize(
    "changed",
    ["unverified", "unknown", "local_only", "foreign_scope", "foreign_repository", "foreign_host", "bad_revision"],
)
def test_model_text_cannot_make_a_href_trusted_without_exact_native_delivery_scope(changed):
    data = fixture(f"[PR]({PR}) [commit]({COMMIT})")
    candidate = data["natural_turn"]["context"]["candidates"][0]
    if changed == "unverified":
        candidate["delivery"]["evidence"] = "model_claim"
    elif changed == "unknown":
        candidate["delivery"]["remote"]["status"] = "unknown"
    elif changed == "local_only":
        candidate["delivery"]["remote"] = {"status": "not_requested", "pr_url": None}
    elif changed == "foreign_scope":
        candidate["project_id"] = "2" * 32
    elif changed == "foreign_repository":
        data["repository_id"] = "2" * 32
    elif changed == "foreign_host":
        candidate["delivery"]["remote"]["pr_url"] = "https://other.example/team/factory-acceptance-product/pull/5"
    else:
        candidate["delivery"]["checkpoint"]["revision"] = "not-a-commit"
    assert links(card(data)) == []


@pytest.mark.parametrize(
    "url",
    [
        PR + "?token=fictional-private-value",
        PR + "#untrusted",
        PR.replace("https://", "http://"),
        PR.replace("github.com/", "github.com.evil.example/"),
        PR.replace("/team/", "/someone/"),
        PR.replace("/pull/5", "/pull/6"),
        COMMIT[:-1] + "0",
        "https://user:fictional-private-value@github.com/team/factory-acceptance-product/pull/5",
        "https://github.com/team/factory-acceptance-product/commit/%2e%2e",
        "javascript:alert(1)",
    ],
)
def test_similar_arbitrary_or_token_bearing_model_urls_do_not_become_links(url):
    body = card(fixture(f"[Review]({url})"))
    assert links(body) == []
    assert "fictional-private-value" not in body


def test_native_links_do_not_relax_secret_absolute_path_html_or_mention_guards():
    message = (
        f"[PR @everyone]({PR}) <script>alert(1)</script> /private/owned/secret.txt "
        "C:\\Private\\key.txt KEY=fixture-value"
    )
    body = card(fixture(message))
    assert links(body) == [PR]
    assert "@everyone" not in body and "<script>" not in body
    assert "/private/owned" not in body and "Private" not in body and "fixture-value" not in body


def test_current_credential_callback_runs_before_tokenization_and_can_remove_an_entire_link():
    secret = f"[PR]({PR})"
    seen = []

    def safe(value):
        seen.append(value)
        return value.replace(secret, "redacted credential")

    body = card(fixture(secret), safe)
    assert secret in seen and links(body) == [] and "redacted credential" in body


def test_a_retained_href_that_now_matches_a_credential_is_never_published():
    body = card(fixture(f"[PR]({PR})"), lambda value: value.replace(PR, "private value omitted"))
    assert links(body) == [] and PR not in body


def test_raw_url_size_bound_prevents_late_public_quote_overflow_without_truncation():
    message = "\n".join("x" * 360 + f" [saved]({COMMIT})" for _ in range(9))
    with pytest.raises(ValueError, match="quote"):
        NaturalDecision(intent="status", message=message)


def test_no_repository_authority_keeps_existing_safe_unlinked_rendering():
    data = fixture(f"[PR]({PR})")
    before = copy.deepcopy(data)
    assert links(natural_card(data, lambda value: value)) == []
    assert data == before


async def test_native_feedback_supplies_current_repository_and_host_to_the_link_renderer(poll_case, monkeypatch):
    from test_scm_natural_intake_host import prepared

    import firefly_forge.scm_natural_feedback as module

    async with poll_case() as case:
        _, receipt = await prepared(case, monkeypatch, intent="status")
        original = module.natural_card
        seen = []

        def render(data, safe, *, repository, host):
            seen.append((repository["id"], host))
            return original(data, safe, repository=repository, host=host)

        monkeypatch.setattr(module, "natural_card", render)
        result = await case.collaboration.process(receipt)
        assert result["feedback_status"] == "sent", result
        assert seen and all(value == (case.repository, "https://github.com") for value in seen)
        assert case.state["natural_calls"] == 1


def test_settings_with_a_native_href_are_redacted_before_link_tokenization():
    body = card(fixture(f"FORGE_URL={PR} [PR]({PR})"))
    assert links(body) == [PR]
    assert "FORGE_URL=" not in body
