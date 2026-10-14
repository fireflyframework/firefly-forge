# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Synthesize bounded Git object closure from sanitized source without host Git authority."""

from __future__ import annotations

import asyncio
import io
import os
import re
import shutil
import stat
import tarfile
import tempfile
from pathlib import Path

from pyfly.container import service

from firefly_forge.git_workspace import git_environment, trusted_git
from firefly_forge.runner_backends import _bounded_process
from firefly_forge.runner_pool_artifacts import source_archive_digest, source_tree_digest
from firefly_forge.sandbox import pack_workspace, unpack_workspace


@service
class RunnerGitSnapshot:
    def __init__(self, git_binary: Path) -> None:
        info = git_binary.stat(follow_symlinks=False)
        if (
            not git_binary.is_absolute()
            or not stat.S_ISREG(info.st_mode)
            or info.st_uid not in {0, os.getuid()}
            or info.st_mode & 0o022
        ):
            raise ValueError("Git snapshot requires an administrator-controlled installed executable")
        self.git_binary = git_binary

    async def validate(self, source: bytes, metadata: bytes, parent: Path, limit: int) -> bytes:
        expected = await self.build(source, parent, limit)
        if source_archive_digest(metadata) != source_archive_digest(expected):
            raise ValueError("Project metadata differs from the reconstructed minimum Git snapshot")
        return expected

    async def build(self, source: bytes, parent: Path, limit: int) -> bytes:
        info = parent.stat(follow_symlinks=False)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError("Git snapshot staging must be private and owner-controlled")
        temporary = Path(tempfile.mkdtemp(prefix=".forge-git-snapshot-", dir=parent))
        try:
            workspace, template = temporary / "workspace", temporary / "template"
            unpack_workspace(source, workspace, limit)
            if source_archive_digest(source) != source_tree_digest(workspace, limit):
                raise ValueError("Git metadata requires an already sanitized canonical source archive")
            template.mkdir(mode=0o700)
            environment = git_environment(
                {
                    "PATH": os.defpath,
                    "HOME": str(temporary),
                    "GIT_ATTR_NOSYSTEM": "1",
                    "GIT_TERMINAL_PROMPT": "0",
                    "GIT_AUTHOR_NAME": "Forge source snapshot",
                    "GIT_COMMITTER_NAME": "Forge source snapshot",
                    "GIT_AUTHOR_EMAIL": "forge-snapshot@localhost",
                    "GIT_COMMITTER_EMAIL": "forge-snapshot@localhost",
                    "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z",
                    "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z",
                }
            )

            async def git(argv: list[str], data: bytes | None = None) -> bytes:
                command = trusted_git(["git", "-c", "commit.gpgSign=false", *argv])
                command[0] = str(self.git_binary)
                code, output = await _bounded_process(
                    command, workspace, 5, 4096, input_data=data, environment=environment
                )
                if code:
                    raise RuntimeError("Fixed source snapshot Git operation failed")
                return output.strip()

            async with asyncio.timeout(30):
                await git(
                    [
                        "init",
                        "--quiet",
                        "--object-format=sha1",
                        "--initial-branch=forge-snapshot",
                        "--template=" + str(template),
                    ]
                )
                entries = bytearray()
                with tarfile.open(fileobj=io.BytesIO(source), mode="r:") as stream:
                    for member in stream:
                        if not member.isfile():
                            continue
                        payload = stream.extractfile(member)
                        assert payload is not None
                        digest = await git(["hash-object", "-w", "--stdin", "--no-filters"], payload.read())
                        if not re.fullmatch(rb"[a-f0-9]{40}", digest):
                            raise RuntimeError("Git did not return an exact source object identity")
                        mode = b"100755" if member.mode & 0o111 else b"100644"
                        entries.extend(mode + b" " + digest + b"\t" + member.name.encode() + b"\0")
                await git(["update-index", "-z", "--index-info"], bytes(entries))
                tree = await git(["write-tree"])
                if not re.fullmatch(rb"[a-f0-9]{40}", tree):
                    raise RuntimeError("Git did not return an exact source tree identity")
                commit = await git(["commit-tree", tree.decode()], b"Frozen approved source snapshot\n")
                if not re.fullmatch(rb"[a-f0-9]{40}", commit):
                    raise RuntimeError("Git did not return an exact source commit identity")
                await git(["update-ref", "refs/heads/forge-snapshot", commit.decode()])
            metadata, _ = pack_workspace(workspace / ".git", limit, metadata=True)
            return metadata
        finally:
            shutil.rmtree(temporary)
