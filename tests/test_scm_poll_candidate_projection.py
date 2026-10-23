# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Compact candidate projection keeps only typed API-observed consent references."""

import base64
import copy
import unittest

from firefly_forge import scm_poll_models
from firefly_forge.oauth_credential_fingerprints import OAuthCredentialMatcher

REFERENCE = {
    "version": 1,
    "transport": "github_rest_poll_v1",
    "provider": "github",
    "api_origin": "https://api.github.com",
    "web_origin": "https://github.com",
    "installation_id": "34",
    "remote_repository_id": "42",
    "external_actor_id": "7",
    "connection_revision": "a" * 32,
    "connection_scope_digest": "b" * 64,
    "resource_id": "123",
    "issue_number": 3,
    "resource_updated_at": "2026-10-23T00:00:00Z",
    "payload_digest": "c" * 64,
    "response_sha256": "d" * 64,
    "observed_at": "2026-10-23T01:00:00Z",
}


class CandidateProjectionTests(unittest.TestCase):
    def project(self, data):
        contract = getattr(scm_poll_models, "GitHubPollCandidateV1", None)
        self.assertIsNotNone(contract, "Candidate reads need a compact typed consent projection")
        return contract.from_observation(data, "e" * 64).model_dump(mode="json")

    def test_65_retained_deep_dialogues_project_to_bounded_50_reference_response(self):
        context = {"not_for_candidates": "private retained discussion"}
        for _ in range(30):
            context = {"parent": context}
        rows = [
            {"id": f"{i:032x}", "verification": copy.deepcopy(REFERENCE), "conversation": context} for i in range(1, 66)
        ]
        matcher = OAuthCredentialMatcher(base64.urlsafe_b64encode(b"0" * 32).decode(), "public_projection")
        with self.assertRaisesRegex(ValueError, "structural bound"):
            matcher.contains({"items": rows[:50]}, [])
        items = [self.project(row) for row in rows[:50]]
        self.assertFalse(matcher.contains({"items": items}, []))
        self.assertEqual(items[0], {"id": "0" * 31 + "1", "verification": REFERENCE, "reference_digest": "e" * 64})
        self.assertEqual(len(items), 50)
        self.assertTrue(all(set(item) == {"id", "verification", "reference_digest"} for item in items))
        self.assertTrue(all("conversation" in row for row in rows))

    def test_bad_reference_identity_is_refused_without_exposing_private_context(self):
        data = {
            "id": "1" * 32,
            "verification": {**REFERENCE, "external_actor_id": "not-a-provider-id"},
            "conversation": "private detail",
        }
        with self.assertRaises(ValueError) as caught:
            self.project(data)
        self.assertNotIn("private detail", str(caught.exception))
        self.assertNotIn("not-a-provider-id", str(caught.exception))

    def test_known_credential_inside_returned_reference_still_refuses_admission(self):
        secret = "public-fixture-credential"
        matcher = OAuthCredentialMatcher(base64.urlsafe_b64encode(b"0" * 32).decode(), "public_projection")
        packet = matcher.packet("1" * 32, "registration", "2" * 32, 1, [secret])
        data = {"id": "3" * 32, "verification": {**REFERENCE, "api_origin": f"https://{secret}.example"}}
        projected = self.project(data)
        self.assertTrue(matcher.contains(projected, [packet]))

    def test_malformed_native_row_identity_is_refused(self):
        with self.assertRaises(ValueError):
            self.project({"id": "not-native", "verification": REFERENCE})


if __name__ == "__main__":
    unittest.main()
