# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Public contracts expose observation separately from owner-only credentials."""

from firefly_forge.api import openapi_document


def test_fallback_openapi_describes_scoped_reads_and_interactive_token_issuance():
    document = openapi_document()
    operation = document["paths"]["/api/v1/factory-dashboard"]["get"]
    parameters = {row["name"]: row for row in operation["parameters"]}
    assert parameters["kind"]["schema"]["enum"] == ["factory", "project", "standalone", "run"]
    assert parameters["limit"]["schema"]["maximum"] == 200
    assert operation["security"] == [{"operator": []}, {"browserSession": []}]
    assert set(document["paths"]["/api/v1/factory-dashboard"]) == {"get"}
    assert set(document["paths"]["/api/v1/factory-dashboard/projects"]) == {"get"}
    create = document["paths"]["/api/v1/identity/tokens"]["post"]
    assert create["security"] == [{"browserSession": []}]
    assert "201" in create["responses"]
    assert document["components"]["securitySchemes"]["browserSession"]["name"] == "forge_session"
