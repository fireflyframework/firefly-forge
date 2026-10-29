# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Derived build outputs permit copy metadata, never stale data or private inputs."""

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from firefly_forge.verification_scope import build_profile


@pytest.mark.skipif(sys.platform != "darwin", reason="Native scoped file access requires Darwin")
async def test_node_copy_and_enumeration_can_build_dist_without_reading_stale_or_private_data(tmp_path):
    executable = os.environ.get("FORGE_TEST_NODE") or shutil.which("node")
    if executable is None:
        pytest.skip("This native Node copy fixture requires Node")
    node = Path(executable).resolve()
    workspace, scratch = tmp_path / "workspace", tmp_path / "scratch"
    source = workspace / "public" / "brand" / "FONT-LICENSE.txt"
    source.parent.mkdir(parents=True)
    source.write_text("public fictional license\n")
    destination = workspace / "frontend" / "dist" / "brand" / source.name
    destination.parent.mkdir(parents=True)
    stale = destination.parent / "stale.txt"
    stale.write_text("old output must not become a build input\n")
    private = destination.parent / ".npmrc"
    private.write_text("fictional private configuration\n")
    outside = tmp_path / "outside.txt"
    outside.write_text("fictional external input\n")
    alias = destination.parent / "external-alias"
    alias.symlink_to(outside)
    scratch.mkdir()
    code = """const fs=require('node:fs');
const [source,destination,stale,secret,outside,alias]=process.argv.slice(1);
fs.copyFileSync(source,destination);
if(fs.statSync(destination).size!==25)throw Error('Copy metadata did not settle');
const directory=require('node:path').dirname(destination);
if(!fs.readdirSync(directory).includes('FONT-LICENSE.txt'))throw Error('Output enumeration failed');
function denied(operation){
try{operation()}catch(error){if(error.code==='EPERM'||error.code==='EACCES')return;throw error}
throw Error('Unbound input escaped')}
for(const file of [destination,stale,secret,outside,alias])denied(()=>fs.readFileSync(file));
denied(()=>fs.statSync(secret));
denied(()=>fs.statSync(outside));
denied(()=>fs.writeFileSync(outside,'changed'));
console.log(JSON.stringify({copied:true,enumerated:true,staleDataDenied:true,privateMetadataDenied:true,externalDenied:true}));
"""
    profile = build_profile(workspace, [Path(executable), node, node.parent.parent], scratch, fresh_build=True)
    process = await asyncio.create_subprocess_exec(
        "/usr/bin/sandbox-exec",
        "-p",
        profile,
        str(node),
        "-e",
        code,
        *map(str, (source, destination, stale, private, outside, alias)),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    assert process.returncode == 0, stderr.decode()
    assert json.loads(stdout) == {
        "copied": True,
        "enumerated": True,
        "staleDataDenied": True,
        "privateMetadataDenied": True,
        "externalDenied": True,
    }
    assert destination.read_text() == "public fictional license\n"
    assert outside.read_text() == "fictional external input\n"


@pytest.mark.skipif(sys.platform != "darwin", reason="Native scoped file access requires Darwin")
async def test_reusable_profile_cannot_observe_dist_metadata_or_membership(tmp_path):
    executable = os.environ.get("FORGE_TEST_NODE") or shutil.which("node")
    if executable is None:
        pytest.skip("This native Node metadata fixture requires Node")
    node = Path(executable).resolve()
    workspace, scratch = tmp_path / "workspace", tmp_path / "scratch"
    dist = workspace / "frontend" / "dist"
    dist.mkdir(parents=True)
    stale = dist / "passed-verification.flag"
    stale.write_text("unbound mutable output\n")
    scratch.mkdir()
    code = """const fs=require('node:fs');
for(const operation of [()=>fs.statSync(process.argv[1]),()=>fs.readdirSync(process.argv[2])]){
try{operation()}catch(error){if(error.code==='EPERM'||error.code==='EACCES')continue;throw error}
throw Error('Reusable check observed mutable derived metadata')}
console.log('REUSABLE_METADATA_DENIED');
"""
    process = await asyncio.create_subprocess_exec(
        "/usr/bin/sandbox-exec",
        "-p",
        build_profile(workspace, [Path(executable), node, node.parent.parent], scratch),
        str(node),
        "-e",
        code,
        str(stale),
        str(dist),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    assert process.returncode == 0, stderr.decode()
    assert stdout == b"REUSABLE_METADATA_DENIED\n"
