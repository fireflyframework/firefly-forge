# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Host-only behavior checks for the native container acceptance guards."""

import copy
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.container_default_command_smoke import (
    cleanup_program,
    require_graceful_shutdown,
    require_owned_schema,
    require_ready_health,
    require_test_database,
)


class ContainerAcceptanceGuards(unittest.TestCase):
    def setUp(self):
        self.health = {
            "status": "ok",
            "detail": "",
            "service_role": "combined",
            "admission_ready": True,
            "execution_ready": True,
            "execution": {"state": "ready", "local_enabled": True},
        }
        self.state = {"Running": False, "ExitCode": 0, "OOMKilled": False, "Error": ""}
        self.logs = "INFO: Application shutdown complete.\nINFO: Finished server process [12]\n"

    def test_accepts_actual_combined_ready_health(self):
        require_ready_health(self.health)

    def test_rejects_http200_degraded_or_nonexecuting_health(self):
        cases = [
            {"status": "degraded"},
            {"detail": "Execution scheduler stopped"},
            {"detail": {}},
            {"service_role": "api"},
            {"admission_ready": False},
            {"execution_ready": False},
            {"execution_ready": "true"},
            {"execution": {"state": "stopped", "local_enabled": True}},
            {"execution": {"state": "ready", "local_enabled": False}},
        ]
        for patch in cases:
            with self.subTest(patch=patch), self.assertRaises(AssertionError):
                require_ready_health({**copy.deepcopy(self.health), **patch})

    def test_rejects_missing_or_malformed_health_contract(self):
        for payload in [None, [], {}, {"status": "ok"}]:
            with self.subTest(payload=payload), self.assertRaises(AssertionError):
                require_ready_health(payload)

    def test_accepts_clean_uvicorn_sigterm_with_shutdown_evidence(self):
        for code in [0, 143]:
            with self.subTest(code=code):
                require_graceful_shutdown({**self.state, "ExitCode": code}, self.logs)

    def test_rejects_exit_without_completed_application_shutdown(self):
        for logs in ["", "INFO: Shutting down\n", "INFO: Finished server process [12]\n"]:
            for code in [0, 143]:
                with self.subTest(code=code, logs=logs), self.assertRaises(AssertionError):
                    require_graceful_shutdown({**self.state, "ExitCode": code}, logs)

    def test_rejects_killed_running_or_oom_container_even_with_completion_text(self):
        for patch in [
            {"ExitCode": 137},
            {"ExitCode": 1},
            {"OOMKilled": True},
            {"Running": True},
            {"Error": "runtime failed"},
        ]:
            with self.subTest(patch=patch), self.assertRaises(AssertionError):
                require_graceful_shutdown({**self.state, **patch}, self.logs)

    def test_accepts_only_explicit_postgresql_test_database_names(self):
        for url in [
            "postgresql+asyncpg://tester:fixture@127.0.0.1:5432/forge_test",
            "postgresql://tester:fixture@localhost/dedicated_test",
        ]:
            with self.subTest(url=url):
                require_test_database(url)

    def test_refuses_missing_production_or_ambiguous_database_before_docker(self):
        for url in [
            "",
            "postgresql+asyncpg://tester:fixture@localhost/forge",
            "postgresql://tester:fixture@localhost/production",
            "sqlite:///forge_test",
            "postgresql:///forge_test",
            "postgresql://localhost/forge_test?database=production",
            "postgresql://localhost/forge_test#production",
            "postgresql://localhost/forge_test/extra",
        ]:
            with self.subTest(url=url), self.assertRaises((AssertionError, ValueError)):
                require_test_database(url)

    def test_entrypoint_refuses_production_database_before_invoking_docker(self):
        script = Path(__file__).with_name("container_default_command_smoke.py")
        process = subprocess.run(
            [sys.executable, str(script), "must-not-start"],
            env={**os.environ, "FORGE_DATABASE_URL": "postgresql://localhost/production", "PATH": ""},
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertNotEqual(process.returncode, 0)
        self.assertIn("dedicated PostgreSQL database ending in _test", process.stderr)
        self.assertNotIn("FileNotFoundError", process.stderr)

    def test_cleanup_refuses_unowned_schema_names(self):
        for schema in [
            "public",
            "forge",
            "forge_test_" + "a" * 32,
            "forge_default_smoke_abc",
            "forge_default_smoke_" + "a" * 32 + '";DROP SCHEMA public;--',
        ]:
            with self.subTest(schema=schema), self.assertRaises(AssertionError):
                cleanup_program(schema)
        owned = "forge_default_smoke_" + "a" * 32
        require_owned_schema(owned)
        compile(cleanup_program(owned), "<owned-schema-cleanup>", "exec")


class ContainerCleanupBoundary(unittest.TestCase):
    def run_cleanup_case(self, inspect_code=0, removal_code=0, ddl_code=0):
        with tempfile.TemporaryDirectory(prefix="forge-supervision-guard-") as directory:
            root = Path(directory)
            docker = root / "docker"
            docker.write_text(
                f"#!{sys.executable}\n"
                "import json,sys\n"
                "from pathlib import Path\n"
                f"root=Path({str(root)!r})\n"
                "args=sys.argv[1:]\n"
                "if args[:2]==['image','inspect']:\n"
                " print(json.dumps([{'Config':{'Cmd':['/app/.venv/bin/forge','serve','--host','0.0.0.0'],"
                "'User':'forge','Entrypoint':['/usr/bin/tini','--']}}]))\n"
                "elif args[:2]==['run','--detach']:\n"
                " sys.exit(42)\n"
                f"elif args[:1]==['inspect']: sys.exit({inspect_code})\n"
                "elif args[:2]==['rm','--force']:\n"
                f" code={removal_code}\n"
                " if code==0: (root/'removed').write_text('removed')\n"
                " sys.exit(code)\n"
                "elif args[:2]==['run','--rm']:\n"
                " (root/'ddl-requested').write_text('after-removal' "
                "if (root/'removed').exists() else 'before-removal')\n"
                f" sys.exit({ddl_code})\n"
                "else: sys.exit(99)\n"
            )
            docker.chmod(0o700)
            process = subprocess.run(
                [sys.executable, str(Path(__file__).with_name("container_default_command_smoke.py")), "fixture"],
                env={**os.environ, "PATH": str(root), "FORGE_DATABASE_URL": "postgresql://localhost/owned_test"},
                capture_output=True,
                text=True,
                timeout=5,
            )
            requested = root / "ddl-requested"
            return process, requested.read_text() if requested.exists() else None

    def test_removal_failure_does_not_drop_schema_and_preserves_original_failure(self):
        process, requested = self.run_cleanup_case(removal_code=17)
        self.assertIsNone(requested, "Schema must remain while the owned app may still be running")
        self.assertIn(
            "exit status 42",
            [line for line in process.stderr.splitlines() if line.startswith("subprocess.CalledProcessError:")][-1],
        )
        self.assertIn("exit status 17", process.stderr)

    def test_ambiguous_inspect_failure_does_not_drop_schema(self):
        process, requested = self.run_cleanup_case(inspect_code=9)
        self.assertIsNone(requested, "A failed inspect does not prove container absence")
        self.assertIn("exit status 42", process.stderr)
        self.assertIn("reconciliation", process.stderr)

    def test_positive_removal_allows_only_subsequent_schema_cleanup(self):
        process, requested = self.run_cleanup_case()
        self.assertEqual(requested, "after-removal")
        self.assertIn("exit status 42", process.stderr)

    def test_schema_cleanup_failure_does_not_replace_original_failure(self):
        process, requested = self.run_cleanup_case(ddl_code=19)
        self.assertEqual(requested, "after-removal")
        self.assertIn(
            "exit status 42",
            [line for line in process.stderr.splitlines() if line.startswith("subprocess.CalledProcessError:")][-1],
        )
        self.assertIn("exit status 19", process.stderr)


if __name__ == "__main__":
    unittest.main()
