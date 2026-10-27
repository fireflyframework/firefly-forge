# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Versioned lookup keeps scopes, retained operation identities and one-shot writes."""

import json

import httpx
import pytest
from test_original_recovery_sdk import connection

REQUEST, CORPUS = "a" * 32, "b" * 32
REFRESH = {
    "version": 1,
    "discovery_digest": "c" * 64,
    "expected_status_revision": "d" * 64,
    "policy_digest": "e" * 64,
    "make_current": True,
}


def test_sdk_keeps_routes_versions_cursors_and_original_request_identity():
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={"observed": True})

    with connection(respond) as client:
        client.framework_knowledge_status()
        client.check_framework_updates("larafly", {"version": 1, "framework_version": "26.10.4"})
        client.refresh_framework_knowledge("larafly", REQUEST, REFRESH)
        client.framework_knowledge_refresh("larafly", REQUEST)
        client.observe_framework_knowledge_refresh("larafly", REQUEST)
        client.framework_corpora("larafly", cursor="opaque+/=", page_size=37)
        client.framework_corpus_sources("larafly", CORPUS, cursor="next-cursor", page_size=41)
        client.search_framework_knowledge("larafly", {"query": "transaction rollback", "corpus_ref": CORPUS})
    base = "/api/v1/knowledge/frameworks"
    assert [(r.method, r.url.path) for r in calls] == [
        ("GET", base + "/status"),
        ("POST", base + "/larafly/check-updates"),
        ("POST", base + f"/larafly/refreshes/{REQUEST}"),
        ("GET", base + f"/larafly/refreshes/{REQUEST}"),
        ("POST", base + f"/larafly/refreshes/{REQUEST}/observe"),
        ("GET", base + "/larafly/corpora"),
        ("GET", base + f"/larafly/corpora/{CORPUS}/sources"),
        ("POST", base + "/larafly/search"),
    ]
    assert json.loads(calls[1].content) == {"version": 1, "framework_version": "26.10.4"}
    assert json.loads(calls[2].content) == REFRESH
    assert dict(calls[5].url.params) == {"cursor": "opaque+/=", "page_size": "37"}
    assert dict(calls[6].url.params) == {"cursor": "next-cursor", "page_size": "41"}
    assert json.loads(calls[7].content) == {"query": "transaction rollback", "corpus_ref": CORPUS}


@pytest.mark.parametrize("framework", ["../larafly", "PyFly", "python", "https://other.example", ""])
@pytest.mark.parametrize(
    "method",
    [
        "check_framework_updates",
        "refresh_framework_knowledge",
        "framework_knowledge_refresh",
        "observe_framework_knowledge_refresh",
        "framework_corpora",
        "framework_corpus_sources",
        "search_framework_knowledge",
    ],
)
def test_framework_names_cannot_retarget_native_routes(method, framework):
    arguments = {
        "check_framework_updates": ({},),
        "refresh_framework_knowledge": (REQUEST, REFRESH),
        "framework_knowledge_refresh": (REQUEST,),
        "observe_framework_knowledge_refresh": (REQUEST,),
        "framework_corpora": (),
        "framework_corpus_sources": (CORPUS,),
        "search_framework_knowledge": ({"query": "DI", "corpus_ref": CORPUS},),
    }
    calls = []
    with connection(lambda request: calls.append(request)) as client, pytest.raises(ValueError):
        getattr(client, method)(framework, *arguments[method])
    assert calls == []


@pytest.mark.parametrize(
    "method,args",
    [
        ("refresh_framework_knowledge", ("pyfly", "../private", REFRESH)),
        ("framework_knowledge_refresh", ("pyfly", "../private")),
        ("observe_framework_knowledge_refresh", ("pyfly", "../private")),
        ("framework_corpus_sources", ("pyfly", "../private")),
    ],
)
def test_operation_and_corpus_identifiers_are_validated_before_http(method, args):
    calls = []
    with connection(lambda request: calls.append(request)) as client, pytest.raises(ValueError):
        getattr(client, method)(*args)
    assert calls == []


@pytest.mark.parametrize(
    "change",
    [{"token": "private-canary-never-echo"}, {"make_current": "true"}, {"version": 2}, {"discovery_digest": "wrong"}],
)
def test_refresh_refuses_unreviewed_fields_before_http_without_echoing_inputs(change):
    calls = []
    with connection(lambda request: calls.append(request)) as client, pytest.raises(ValueError) as caught:
        client.refresh_framework_knowledge("pyfly", REQUEST, {**REFRESH, **change})
    assert "private-canary-never-echo" not in str(caught.value)
    assert calls == []


@pytest.mark.parametrize("page_size", [True, 0, -1, 201, "50"])
def test_paging_window_validation_does_not_issue_a_partial_query(page_size):
    calls = []
    with connection(lambda request: calls.append(request)) as client, pytest.raises(ValueError):
        client.framework_corpora("pyfly", page_size=page_size)
    assert calls == []


@pytest.mark.parametrize("method", ["refresh_framework_knowledge", "observe_framework_knowledge_refresh"])
def test_uncertain_write_is_not_retried_and_same_request_can_be_read(method):
    calls = []

    def respond(request):
        calls.append(request)
        if request.method == "POST":
            raise httpx.ReadTimeout("Controlled lost response", request=request)
        return httpx.Response(200, json={"request_id": REQUEST, "state": "needs_reconciliation"})

    with connection(respond) as client:
        with pytest.raises(httpx.ReadTimeout):
            args = ("pyfly", REQUEST, REFRESH) if method == "refresh_framework_knowledge" else ("pyfly", REQUEST)
            getattr(client, method)(*args)
        assert len(calls) == 1
        assert client.framework_knowledge_refresh("pyfly", REQUEST)["request_id"] == REQUEST
    assert [request.method for request in calls] == ["POST", "GET"]


@pytest.mark.parametrize(
    "body",
    [
        {"query": "DI", "corpus_ref": CORPUS, "framework_version": "26.10.11"},
        {"query": "DI", "framework_version": "main"},
        {"query": "DI", "corpus_ref": CORPUS, "api_key": "private-canary-never-echo"},
        {"query": "DI"},
    ],
)
def test_search_cannot_mix_scopes_omit_version_or_accept_credentials(body):
    calls = []
    with connection(lambda request: calls.append(request)) as client, pytest.raises(ValueError) as caught:
        client.search_framework_knowledge("pyfly", body)
    assert "private-canary-never-echo" not in str(caught.value)
    assert calls == []


@pytest.mark.parametrize(
    "method,route,body",
    [
        (
            "renew_framework_knowledge_budget",
            "budget",
            {
                "version": 1,
                "expected_revision": "f" * 64,
                "total_budget": {"requests": 100_000, "bytes": 1_073_741_824, "seconds": 86_400},
            },
        ),
        ("cancel_framework_knowledge_refresh", "cancel", {"version": 1, "expected_revision": "f" * 64}),
        ("resume_framework_knowledge_refresh", "resume", {"version": 1, "expected_revision": "f" * 64}),
    ],
)
def test_refresh_controls_preserve_revision_and_retained_operation_without_retries(method, route, body):
    calls = []

    def respond(request):
        calls.append(request)
        if request.method == "POST":
            raise httpx.ReadTimeout("Controlled lost control response", request=request)
        return httpx.Response(200, json={"request_id": REQUEST})

    with connection(respond) as client:
        with pytest.raises(httpx.ReadTimeout):
            getattr(client, method)("pyfly", REQUEST, body)
        assert len(calls) == 1
        assert client.framework_knowledge_refresh("pyfly", REQUEST)["request_id"] == REQUEST
    assert calls[0].url.path == f"/api/v1/knowledge/frameworks/pyfly/refreshes/{REQUEST}/{route}"
    assert json.loads(calls[0].content) == body
    assert [r.method for r in calls] == ["POST", "GET"]


@pytest.mark.parametrize(
    "method",
    ["renew_framework_knowledge_budget", "cancel_framework_knowledge_refresh", "resume_framework_knowledge_refresh"],
)
@pytest.mark.parametrize(
    "change", [{"spent_budget": 0}, {"expected_revision": None}, {"version": 2}, {"token": "private-canary-never-echo"}]
)
def test_refresh_controls_cannot_reset_spend_or_accept_unreviewed_choices(method, change):
    body = {"version": 1, "expected_revision": "f" * 64}
    if method == "renew_framework_knowledge_budget":
        body["total_budget"] = {"requests": 100_000, "bytes": 1_073_741_824, "seconds": 86_400}
    calls = []
    with connection(lambda request: calls.append(request)) as client, pytest.raises(ValueError) as caught:
        getattr(client, method)("pyfly", REQUEST, {**body, **change})
    assert calls == []
    assert "private-canary-never-echo" not in str(caught.value)


def test_generation_activation_retains_validation_and_original_request_on_lost_response():
    body = {
        "version": 1,
        "corpus_ref": CORPUS,
        "expected_status_revision": "c" * 64,
        "validation_run_id": "d" * 32,
    }
    calls = []

    def respond(request):
        calls.append(request)
        if request.method == "POST":
            raise httpx.ReadTimeout("Controlled activation response loss", request=request)
        return httpx.Response(200, json={"request_id": REQUEST})

    with connection(respond) as client:
        with pytest.raises(httpx.ReadTimeout):
            client.activate_framework_generation_target("larafly", REQUEST, body)
        assert len(calls) == 1
        assert client.get_framework_generation_target("larafly", REQUEST)["request_id"] == REQUEST
    assert [r.method for r in calls] == ["POST", "GET"]
    assert all(r.url.path == f"/api/v1/knowledge/frameworks/larafly/generation-targets/{REQUEST}" for r in calls)
    assert json.loads(calls[0].content) == body


@pytest.mark.parametrize(
    "change",
    [
        {"version": 2},
        {"validation_run_id": "../other"},
        {"expected_status_revision": None},
        {"token": "private-canary-never-echo"},
    ],
)
def test_generation_activation_requires_native_validation_and_exact_review_before_http(change):
    calls = []
    body = {
        "version": 1,
        "corpus_ref": CORPUS,
        "expected_status_revision": "c" * 64,
        "validation_run_id": "d" * 32,
    }
    with connection(lambda request: calls.append(request)) as client, pytest.raises(ValueError) as caught:
        client.activate_framework_generation_target("pyfly", REQUEST, {**body, **change})
    assert not calls and "private-canary-never-echo" not in str(caught.value)


@pytest.mark.parametrize("method", ["activate_framework_generation_target", "get_framework_generation_target"])
def test_generation_activation_cannot_retarget_framework_or_request_routes(method):
    calls = []
    with connection(lambda request: calls.append(request)) as client:
        for framework, request_id in [("../pyfly", REQUEST), ("pyfly", "../request")]:
            args = (framework, request_id, {}) if method.startswith("activate") else (framework, request_id)
            with pytest.raises(ValueError):
                getattr(client, method)(*args)
    assert not calls


def test_generation_candidates_keep_framework_corpus_and_review_page():
    calls = []
    with connection(
        lambda request: calls.append(request) or httpx.Response(200, json={"items": [], "next_cursor": REQUEST})
    ) as client:
        result = client.framework_generation_candidates("larafly", CORPUS, cursor=REQUEST, page_size=37)
    assert result == {"items": [], "next_cursor": REQUEST}
    assert len(calls) == 1 and calls[0].method == "GET"
    assert calls[0].url.path == "/api/v1/knowledge/frameworks/larafly/generation-candidates"
    assert dict(calls[0].url.params) == {"corpus_ref": CORPUS, "cursor": REQUEST, "page_size": "37"}


@pytest.mark.parametrize(
    "change", [{"framework": "../pyfly"}, {"corpus_ref": "bad"}, {"cursor": "../run"}, {"page_size": True}]
)
def test_generation_candidates_reject_bad_scope_before_http(change):
    calls = []
    values = {"framework": "pyfly", "corpus_ref": CORPUS, "cursor": REQUEST, "page_size": 50, **change}
    with connection(lambda request: calls.append(request)) as client, pytest.raises(ValueError):
        client.framework_generation_candidates(**values)
    assert not calls
