# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Publication must reject artifacts from another source or a changed Studio."""

import base64
import csv
import hashlib
import io
import json
import runpy
import subprocess
import tarfile
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path

verify_payloads = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/check_release_payloads.py"))[
    "verify_payloads"
]


class ReleasePayloadTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "source"
        self.root.mkdir()
        self.dist = Path(self.temporary.name) / "dist"
        self.dist.mkdir()
        files = {
            "pyproject.toml": (
                '[project]\nname="firefly-forge"\nversion="0.2.10"\n'
                'requires-python=">=3.13"\nlicense="Apache-2.0"\n'
                'dependencies=["httpx>=0.28,<1"]\n[project.scripts]\n'
                'forge="firefly_forge.cli:main"\n'
            ),
            ".gitignore": "studio/dist/\n",
            "README.md": "# A fictional factory\n",
            "LICENSE": "Apache license fixture\n",
            "NOTICE": "Foundation fixture\n",
            "src/firefly_forge/__init__.py": '__version__="0.2.10"\n',
            "src/firefly_forge/resources/default.yml": "enabled: true\n",
            "assets/fonts/manrope/font.woff2": "font fixture",
            "studio/dist/index.html": '<script src="/assets/app.js"></script>',
            "studio/dist/assets/app.js": "console.log('fixture')",
        }
        for name, content in files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        subprocess.run(["git", "-C", str(self.root), "add", "."], check=True)
        self.tracked = {name: (self.root / name).read_bytes() for name in files if not name.startswith("studio/dist/")}
        self.studio = {
            name.removeprefix("studio/dist/"): content.encode()
            for name, content in files.items()
            if name.startswith("studio/dist/")
        }
        self.metadata = (
            b"Metadata-Version: 2.4\nName: firefly-forge\nVersion: 0.2.10\n"
            b"Requires-Python: >=3.13\nLicense-Expression: Apache-2.0\n"
            b"Requires-Dist: httpx>=0.28,<1\n\n# A fictional factory\n"
        )
        self.prefix = "firefly_forge-0.2.10.dist-info/"
        self.wheel = self.dist / "firefly_forge-0.2.10-py3-none-any.whl"
        self.source = self.dist / "firefly_forge-0.2.10.tar.gz"
        self.wheel_files = {
            **{name.removeprefix("src/"): data for name, data in self.tracked.items() if name.startswith("src/")},
            **{"firefly_forge/static/" + name: data for name, data in self.studio.items()},
            "firefly_forge/fonts/manrope/font.woff2": b"font fixture",
            self.prefix + "METADATA": self.metadata,
            self.prefix + "WHEEL": b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
            self.prefix + "entry_points.txt": b"[console_scripts]\nforge = firefly_forge.cli:main\n",
            self.prefix + "licenses/LICENSE": self.tracked["LICENSE"],
            self.prefix + "licenses/NOTICE": self.tracked["NOTICE"],
        }
        self.source_files = {
            **self.tracked,
            **{"studio/dist/" + key: value for key, value in self.studio.items()},
            "PKG-INFO": self.metadata,
        }
        self.write_wheel()
        self.write_source()

    def write_wheel(self, duplicate=False):
        record = self.prefix + "RECORD"
        rows = [
            [
                name,
                "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip("="),
                str(len(data)),
            ]
            for name, data in self.wheel_files.items()
        ] + [[record, "", ""]]
        output = io.StringIO()
        csv.writer(output).writerows(rows)
        with zipfile.ZipFile(self.wheel, "w") as archive:
            for name, data in {**self.wheel_files, record: output.getvalue().encode()}.items():
                archive.writestr(name, data)
            if duplicate:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", UserWarning)
                    archive.writestr("firefly_forge/static/index.html", b"different")

    def write_source(self, link=False):
        with tarfile.open(self.source, "w:gz") as archive:
            for name, data in self.source_files.items():
                item = tarfile.TarInfo("firefly_forge-0.2.10/" + name)
                item.size = len(data)
                archive.addfile(item, io.BytesIO(data))
            if link:
                item = tarfile.TarInfo("firefly_forge-0.2.10/link")
                item.type = tarfile.SYMTYPE
                item.linkname = "../outside"
                archive.addfile(item)

    def test_source_matched_artifacts_and_studio_are_accepted(self):
        verify_payloads(self.dist, self.root, tag="v0.2.10")

    def optional_metadata(self, requirement, emitted):
        project = (
            self.tracked["pyproject.toml"]
            + ("\n[project.optional-dependencies]\nanalytics=" + json.dumps([requirement]) + "\n").encode()
        )
        (self.root / "pyproject.toml").write_bytes(project)
        self.tracked["pyproject.toml"] = self.source_files["pyproject.toml"] = project
        metadata = self.metadata.replace(
            b"\n\n", ("\nProvides-Extra: analytics\nRequires-Dist: " + emitted + "\n\n").encode(), 1
        )
        self.wheel_files[self.prefix + "METADATA"] = self.source_files["PKG-INFO"] = metadata
        self.write_wheel()
        self.write_source()
        return metadata

    def test_declared_optional_dependency_is_accepted_with_its_extra_marker(self):
        requirement = (
            "flyquery-sdk @ https://github.com/fireflyframework/flyquery/releases/"
            "download/v26.10.4/flyquery_sdk-26.10.4-py3-none-any.whl"
        )
        self.optional_metadata(requirement, requirement + " ; extra == 'analytics'")
        verify_payloads(self.dist, self.root)

    def test_optional_dependency_keeps_its_original_conditional_marker(self):
        requirement = "example[client]>=1,<2; sys_platform == 'linux' or python_version < '3.14'"
        self.optional_metadata(
            requirement,
            "example[client]<2,>=1; (sys_platform == 'linux' or python_version < '3.14') and extra == 'analytics'",
        )
        verify_payloads(self.dist, self.root)

    def test_optional_metadata_cannot_drop_or_broaden_the_declared_dependency(self):
        metadata = self.optional_metadata(
            "example[client]>=1,<2; python_version < '3.14'",
            "example[client]<2,>=1; (python_version < '3.14') and extra == 'analytics'",
        )
        verify_payloads(self.dist, self.root)
        changes = [
            metadata.replace(b"Provides-Extra: analytics\n", b""),
            metadata.replace(b"Provides-Extra: analytics", b"Provides-Extra: other"),
            metadata.replace(b"Provides-Extra: analytics", b"Provides-Extra: analytics\nProvides-Extra: analytics"),
            metadata.replace(
                b"Requires-Dist: example[client]<2,>=1; (python_version < '3.14') and extra == 'analytics'\n", b""
            ),
            metadata.replace(b"example[client]", b"another[client]"),
            metadata.replace(b"[client]", b"[server]"),
            metadata.replace(b">=1", b">=0"),
            metadata.replace(b"python_version < '3.14'", b"python_version < '3.15'"),
            metadata.replace(b" and extra == 'analytics'", b""),
            metadata.replace(b" and extra == 'analytics'", b" or extra == 'analytics'"),
            metadata.replace(b"extra == 'analytics'", b"extra == 'other'"),
        ]
        for changed in changes:
            for artifact in ("wheel", "source"):
                with self.subTest(artifact=artifact, metadata=changed):
                    self.wheel_files[self.prefix + "METADATA"] = changed if artifact == "wheel" else metadata
                    self.source_files["PKG-INFO"] = changed if artifact == "source" else metadata
                    self.write_wheel()
                    self.write_source()
                    with self.assertRaises(ValueError):
                        verify_payloads(self.dist, self.root)

    def test_optional_direct_reference_cannot_change_its_url(self):
        metadata = self.optional_metadata(
            "example @ https://example.test/approved.whl",
            "example @ https://example.test/approved.whl ; extra == 'analytics'",
        )
        verify_payloads(self.dist, self.root)
        self.wheel_files[self.prefix + "METADATA"] = metadata.replace(b"approved.whl", b"different.whl")
        self.write_wheel()
        with self.assertRaises(ValueError):
            verify_payloads(self.dist, self.root)

    def test_undeclared_extra_cannot_be_advertised(self):
        self.wheel_files[self.prefix + "METADATA"] = self.metadata.replace(
            b"\n\n", b"\nProvides-Extra: unexpected\n\n", 1
        )
        self.write_wheel()
        with self.assertRaises(ValueError):
            verify_payloads(self.dist, self.root)

    def test_another_tag_cannot_publish_this_version(self):
        with self.assertRaises(ValueError):
            verify_payloads(self.dist, self.root, tag="v0.2.11")

    def test_valid_record_does_not_authorize_changed_runtime_or_studio(self):
        for name in (
            "firefly_forge/__init__.py",
            "firefly_forge/static/index.html",
            "firefly_forge/fonts/manrope/font.woff2",
            self.prefix + "licenses/NOTICE",
        ):
            with self.subTest(name=name):
                original = self.wheel_files[name]
                self.wheel_files[name] = b"changed"
                self.write_wheel()
                with self.assertRaises(ValueError):
                    verify_payloads(self.dist, self.root)
                self.wheel_files[name] = original

    def test_source_distribution_must_match_every_tracked_input(self):
        self.source_files["README.md"] = b"another source"
        self.write_source()
        with self.assertRaises(ValueError):
            verify_payloads(self.dist, self.root)

    def test_package_metadata_and_entrypoint_cannot_change(self):
        changes = {
            self.prefix + "METADATA": self.metadata.replace(b"httpx>=0.28,<1", b"httpx>=0.27,<1"),
            self.prefix + "entry_points.txt": b"[console_scripts]\nforge = unexpected:main\n",
        }
        for name, data in changes.items():
            with self.subTest(name=name):
                original = self.wheel_files[name]
                self.wheel_files[name] = data
                self.write_wheel()
                with self.assertRaises(ValueError):
                    verify_payloads(self.dist, self.root)
                self.wheel_files[name] = original

    def test_additional_files_and_duplicate_or_unsafe_members_are_refused(self):
        for name in (
            "../escape",
            "/absolute",
            "firefly_forge/untracked.py",
            "firefly_forge/static/../escape",
            "firefly_forge/static/file\\name",
        ):
            with self.subTest(name=name):
                self.wheel_files[name] = b"untracked"
                self.write_wheel()
                with self.assertRaises(ValueError):
                    verify_payloads(self.dist, self.root)
                del self.wheel_files[name]
        self.write_wheel(duplicate=True)
        with self.assertRaises(ValueError):
            verify_payloads(self.dist, self.root)
        self.write_wheel()
        self.write_source(link=True)
        with self.assertRaises(ValueError):
            verify_payloads(self.dist, self.root)

    def test_missing_artifacts_cannot_be_published(self):
        self.wheel.unlink()
        with self.assertRaises(ValueError):
            verify_payloads(self.dist, self.root)
