# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Fictional host checks for the CI PostgreSQL lifecycle; no Docker execution."""

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts.ci_owned_postgres import container_command, validate_container, validate_native_files


class OwnedPostgresGuards(unittest.TestCase):
    def setUp(self):
        self.state = {
            "name": "forge-python-postgres-" + "a" * 32,
            "nonce": "a" * 32,
            "image_id": "sha256:" + "b" * 64,
            "uid": os.getuid(),
            "gid": os.getgid(),
            "port": 5432,
            "data": "/runner/temporary/private/data",
        }
        self.raw = {
            "Id": "c" * 64,
            "Name": "/" + self.state["name"],
            "Image": self.state["image_id"],
            "Config": {"User": f"{os.getuid()}:{os.getgid()}", "Labels": {"org.firefly.forge.ci-postgres": "a" * 32}},
            "HostConfig": {"NetworkMode": "host"},
            "Mounts": [{"Type": "bind", "Source": self.state["data"], "Destination": self.state["data"], "RW": True}],
            "State": {"Running": True},
        }

    def test_command_uses_runner_uid_same_absolute_private_mount_and_loopback(self):
        args = container_command(self.state)
        self.assertIn(f"{os.getuid()}:{os.getgid()}", args)
        self.assertIn("type=bind,src=" + self.state["data"] + ",dst=" + self.state["data"], args)
        self.assertEqual(args[args.index("--network") + 1], "host")
        self.assertIn("listen_addresses=127.0.0.1", args)
        self.assertIn(f"/var/run/postgresql:rw,noexec,nosuid,nodev,uid={os.getuid()},gid={os.getgid()},mode=0700", args)
        self.assertIn(self.state["image_id"], args)
        self.assertIn("POSTGRES_PASSWORD", args)
        self.assertFalse(any("password=" in item.lower() for item in args))
        self.assertNotIn("--privileged", args)
        self.assertNotIn("/var/run/docker.sock", " ".join(args))

    def test_exact_container_identity_is_required_before_cleanup(self):
        self.assertEqual(validate_container(self.raw, self.state), "c" * 64)
        changes = ("user", "image", "network", "mount", "nonce", "name", "readonly", "cid", "foreign_volume")
        for change in changes:
            raw, state = copy.deepcopy(self.raw), dict(self.state)
            if change == "user":
                raw["Config"]["User"] = "999:999"
            if change == "image":
                raw["Image"] = "sha256:" + "d" * 64
            if change == "network":
                raw["HostConfig"]["NetworkMode"] = "bridge"
            if change == "mount":
                raw["Mounts"][0]["Source"] = "/unrelated/data"
            if change == "nonce":
                raw["Config"]["Labels"]["org.firefly.forge.ci-postgres"] = "other"
            if change == "name":
                raw["Name"] = "/other"
            if change == "readonly":
                raw["Mounts"][0]["RW"] = False
            if change == "cid":
                state["container_id"] = "d" * 64
            if change == "foreign_volume":
                raw["Mounts"].append({"Type": "volume", "Destination": "/unrelated/private"})
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_container(raw, state)

    def test_real_file_shape_binds_sql_identity_and_container_pid_without_host_signals(self):
        import sys

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve() / "data"
            root.mkdir(mode=0o700)
            (root / "global").mkdir(mode=0o700)
            (root / "PG_VERSION").write_text("17\n")
            (root / "PG_VERSION").chmod(0o600)
            (root / "postmaster.pid").write_text(f"1\n{root}\n1000\n5432\n")
            (root / "postmaster.pid").chmod(0o600)
            control = root / "global/pg_control"
            control.write_bytes((1234567).to_bytes(8, sys.byteorder) + b"x" * 8184)
            control.chmod(0o600)
            state = {**self.state, "data": str(root)}
            sql = {
                "data_directory": str(root),
                "database": "forge_test",
                "port": 5432,
                "address": "127.0.0.1",
                "started": 1000,
                "system_identifier": "1234567",
            }
            proof = validate_native_files(state, sql)
            self.assertEqual(proof["postmaster_pid_in_container"], 1)
            self.assertNotIn("host_pid", proof)
            for changed in (
                {"system_identifier": "other"},
                {"data_directory": "/other"},
                {"started": 1001},
                {"port": 5433},
                {"address": "172.17.0.2"},
            ):
                with self.subTest(changed=changed), self.assertRaises(ValueError):
                    validate_native_files(state, {**sql, **changed})
            saved = root / "global/saved-control"
            control.rename(saved)
            control.symlink_to(saved)
            with self.assertRaises(ValueError):
                validate_native_files(state, sql)

    def test_cleanup_signals_only_exact_container_and_preserves_native_data(self):
        self._cleanup(exit_code=0)

    def test_unclean_exit_is_drained_but_never_reported_as_clean_success(self):
        self._cleanup(exit_code=1)

    def test_log_timeout_cannot_prevent_owned_drain(self):
        self._cleanup(exit_code=0, diagnostic="timeout")

    def test_log_write_failure_cannot_prevent_owned_drain(self):
        self._cleanup(exit_code=0, diagnostic="write")

    def test_signal_and_unknown_exit_states_are_never_clean(self):
        for code, change in ((143, {}), (False, {}), (0.0, {}), (0, {"OOMKilled": None})):
            with self.subTest(code=code, change=change):
                self._cleanup(exit_code=code, final_change=change, unclean=True)

    def _cleanup(self, *, exit_code, diagnostic=None, final_change=None, unclean=False):
        from scripts import ci_owned_postgres as module

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            data = root / "data"
            data.mkdir(mode=0o700)
            sentinel = data / "retained"
            sentinel.write_text("retained fixture bytes")
            state = {**self.state, "data": str(data)}
            module.private_write(root / "reservation.json", state)
            raw = copy.deepcopy(self.raw)
            raw["Mounts"][0]["Source"] = raw["Mounts"][0]["Destination"] = str(data)
            stopped = copy.deepcopy(raw)
            stopped["State"] = {"Running": False, "ExitCode": exit_code, "OOMKilled": False, "Error": ""}
            stopped["State"].update(final_change or {})
            calls = []

            def docker(args, **kwargs):
                calls.append(args)
                if diagnostic == "timeout" and args[0] == "logs":
                    raise __import__("subprocess").TimeoutExpired(["docker", "logs"], 30)
                return SimpleNamespace(stdout="fictional native log", stderr="", returncode=0)

            original_open = os.open

            def open_file(path, *args, **kwargs):
                if diagnostic == "write" and Path(path).name == "postgres.log":
                    raise PermissionError("fictional diagnostic write failure")
                return original_open(path, *args, **kwargs)

            caught = None
            with (
                patch.object(module, "inspect_container", side_effect=[raw, stopped, None]),
                patch.object(module, "docker", side_effect=docker),
                patch.object(module.socket, "socket"),
                patch("builtins.print"),
                patch.object(module.os, "open", side_effect=open_file),
            ):
                try:
                    module.stop(root)
                except BaseException as error:
                    caught = error
            self.assertEqual([call[0] for call in calls], ["stop", "logs", "rm"])
            if diagnostic or unclean or exit_code:
                self.assertIsInstance(caught, ValueError)
            else:
                self.assertIsNone(caught)
            self.assertTrue(all(call[-1] == "c" * 64 for call in calls))
            self.assertEqual(sentinel.read_text(), "retained fixture bytes")
            cleanup = json.loads((root / "cleanup.json").read_text())
            self.assertEqual(cleanup["clean_exit"], type(exit_code) is int and exit_code == 0 and not unclean)
            if diagnostic:
                self.assertIn("diagnostic", str(caught).lower())
                self.assertTrue(cleanup["diagnostics_unavailable"])
            self.assertFalse(cleanup["host_pid_signal_used"])

    def test_invalid_reservation_refuses_before_container_inspection(self):
        from scripts import ci_owned_postgres as module

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            state = {**self.state, "data": "/foreign/private/data"}
            module.private_write(root / "reservation.json", state)
            with patch.object(module, "docker") as docker, self.assertRaises(ValueError):
                module.stop(root)
            docker.assert_not_called()

    def test_receipts_publish_atomically_and_cannot_overwrite(self):
        from scripts import ci_owned_postgres as module

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder).resolve() / "receipt.json"
            module.private_write(path, {"known": True})
            self.assertEqual(json.loads(path.read_text()), {"known": True})
            with self.assertRaises(ValueError):
                module.private_write(path, {"known": False})
            self.assertEqual(json.loads(path.read_text()), {"known": True})

    def test_nonlinux_runner_refuses_before_any_docker_control(self):
        from scripts import ci_owned_postgres as module

        with (
            patch.object(module.sys, "platform", "darwin"),
            patch.object(module, "docker") as docker,
            self.assertRaisesRegex(ValueError, "Linux"),
        ):
            module.fixture_root()
        docker.assert_not_called()

    def test_workflow_keeps_full_pytest_and_always_cleanup_without_native_test_skips(self):
        text = (Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml").read_text()
        python = text.split("  python:\n", 1)[1].split("  studio:\n", 1)[0]
        self.assertIn("run: make python-check", python)
        self.assertIn("python3 scripts/ci_owned_postgres.py start", python)
        self.assertIn("if: ${{ always() }}\n        run: python3 scripts/ci_owned_postgres.py stop", python)
        self.assertNotIn("services:", python)
        self.assertNotIn("--ignore", python)
        self.assertIn("faulthandler_exit_on_timeout=False", python)


if __name__ == "__main__":
    unittest.main()
