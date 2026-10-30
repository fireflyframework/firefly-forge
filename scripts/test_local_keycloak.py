# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Local identity artifacts never create accounts or start services."""

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from scripts.configure_local_keycloak import configure


class LocalKeycloakTests(unittest.TestCase):
    def test_private_configuration_has_exact_callbacks_and_no_default_grants(self):
        with tempfile.TemporaryDirectory() as root:
            output = Path(root).resolve() / "identity"
            configure(output)
            realm = json.loads((output / "forge-realm.json").read_text())
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE((output / "forge.env").stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE((output / "forge-realm.json").stat().st_mode), 0o644)
            self.assertNotIn("users", realm)
            self.assertFalse(realm["registrationAllowed"])
            client = next(c for c in realm["clients"] if c["clientId"] == "forge-studio")
            self.assertEqual(client["redirectUris"], ["http://127.0.0.1:8787/api/v1/auth/callback"])
            self.assertEqual(client["webOrigins"], ["http://127.0.0.1:8787"])
            self.assertFalse(client["directAccessGrantsEnabled"])
            self.assertFalse(client["serviceAccountsEnabled"])
            self.assertFalse(client["fullScopeAllowed"])
            self.assertEqual(client["attributes"]["pkce.code.challenge.method"], "S256")
            self.assertEqual(realm["clientScopeMappings"]["forge-api"][0]["roles"], ["forge-admin"])
            self.assertNotIn("forge-worker", realm["clientScopeMappings"]["forge-api"][0]["roles"])
            self.assertFalse(
                next(c for c in realm["clients"] if c["clientId"] == "forge-api")["serviceAccountsEnabled"]
            )
            audience = next(
                mapper for mapper in client["protocolMappers"] if mapper["protocolMapper"] == "oidc-audience-mapper"
            )
            self.assertEqual(audience["config"]["included.client.audience"], "forge-api")
            self.assertEqual(audience["config"]["access.token.claim"], "true")
            self.assertEqual(audience["config"]["id.token.claim"], "false")
            self.assertNotIn("defaultRoles", realm)
            self.assertNotIn("defaultRoles", next(c for c in realm["clients"] if c["clientId"] == "forge-api"))
            env = {
                key: json.loads(value)
                for key, value in (
                    line.split("=", 1)
                    for line in (output / "forge.env").read_text().splitlines()
                    if line and not line.startswith("#")
                )
            }
            self.assertEqual(client["secret"], "${FORGE_LOCAL_CLIENT_SECRET}")
            self.assertEqual(env["FORGE_OIDC_ISSUER"], "http://127.0.0.1:18080/realms/forge")
            self.assertIn(env["FORGE_OIDC_CLIENT_SECRET"], (output / "keycloak.env").read_text())
            self.assertNotIn(env["FORGE_OIDC_CLIENT_SECRET"], (output / "forge-realm.json").read_text())

    def test_existing_configuration_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as root:
            output = Path(root).resolve() / "identity"
            configure(output)
            before = (output / "forge.env").read_bytes()
            with self.assertRaises(FileExistsError):
                configure(output)
            self.assertEqual((output / "forge.env").read_bytes(), before)

    def test_only_exact_loopback_origins_are_allowed_before_writing(self):
        for origin in [
            "http://0.0.0.0:18080",
            "http://localhost.evil.test",
            "http://user@localhost",
            "http://localhost/path",
            "http://localhost?x=y",
            "https://example.test",
        ]:
            with self.subTest(origin=origin), tempfile.TemporaryDirectory() as root:
                output = Path(root).resolve() / "identity"
                with self.assertRaises(ValueError):
                    configure(output, keycloak_origin=origin)
                self.assertFalse(output.exists())

    def test_restrictive_umask_keeps_private_secrets_and_readable_secret_free_import(self):
        with tempfile.TemporaryDirectory() as root:
            output = Path(root).resolve() / "identity"
            prior = os.umask(0o077)
            try:
                configure(output)
            finally:
                os.umask(prior)
            self.assertEqual(stat.S_IMODE((output / "forge-realm.json").stat().st_mode), 0o644)
            self.assertEqual(stat.S_IMODE((output / "keycloak.env").stat().st_mode), 0o600)

    def test_development_browser_callback_and_issuer_remain_distinct(self):
        with tempfile.TemporaryDirectory() as root:
            output = Path(root).resolve() / "identity"
            configure(output, browser_origin="http://127.0.0.1:5173")
            realm = json.loads((output / "forge-realm.json").read_text())
            studio = next(client for client in realm["clients"] if client["clientId"] == "forge-studio")
            self.assertEqual(studio["webOrigins"], ["http://127.0.0.1:5173"])
            self.assertEqual(studio["redirectUris"], ["http://127.0.0.1:8787/api/v1/auth/callback"])

    def test_theme_and_compose_mount_existing_canonical_assets(self):
        import yaml

        repo = Path(__file__).resolve().parents[1]
        compose = repo / "deploy/keycloak/compose.yaml"
        parsed = yaml.safe_load(compose.read_text())
        service = parsed["services"]["keycloak"]
        self.assertEqual(service["image"], "quay.io/keycloak/keycloak:26.8.1")
        self.assertEqual(
            service["ports"], ["127.0.0.1:${FORGE_KEYCLOAK_PORT:?Prepare local identity configuration}:8080"]
        )
        for mount in service["volumes"]:
            if mount.startswith("."):
                source = mount.split(":")[0]
                self.assertTrue((compose.parent / source).resolve().is_dir())
                self.assertTrue(mount.endswith(":ro"))
        theme = compose.parent / "themes/forge/login"
        self.assertIn("parent=keycloak.v2", (theme / "theme.properties").read_text())
        self.assertIn("styles=css/styles.css css/forge.css", (theme / "theme.properties").read_text())
        for filename in ["forge-lockup-light.svg", "forge-lockup-dark.svg"]:
            self.assertTrue((repo / "studio/public/assets" / filename).is_file())
        self.assertTrue((repo / "studio/public/fonts/manrope-latin-wght-normal.woff2").is_file())

    def test_conflicting_ports_and_cross_site_browser_cookies_are_rejected_before_writing(self):
        for options in [
            {"keycloak_origin": "http://127.0.0.1:8787"},
            {"browser_origin": "http://127.0.0.1:18080"},
            {"browser_origin": "http://localhost:5173"},
        ]:
            with self.subTest(options=options), tempfile.TemporaryDirectory() as root:
                output = Path(root).resolve() / "identity"
                with self.assertRaises(ValueError):
                    configure(output, **options)
                self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
