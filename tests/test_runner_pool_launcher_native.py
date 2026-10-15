# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""The actual pool PID1 must reap/drain descendants before publishing command status."""

import asyncio
import json
import os
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import pytest
from runner_test_support import installed_docker_binary

from firefly_forge.runner_backends import _bounded_process
from firefly_forge.runner_pool_artifacts import RunnerPoolArtifacts, source_tree_digest
from firefly_forge.runner_pool_dispatch import DockerPoolDispatcher, RunnerDockerAuthority
from firefly_forge.runner_pool_models import RunnerInvocation, RunnerPoolProfile
from firefly_forge.sandbox import pack_workspace

pytestmark = pytest.mark.skipif(
    not os.environ.get("FORGE_TEST_DOCKER_IMAGE"), reason="Native Docker image not configured"
)

ORPHAN = """import os,time
from pathlib import Path
read,write=os.pipe()
child=os.fork()
if child==0:
 os.close(read)
 grandchild=os.fork()
 if grandchild==0:
  os.close(write)
  {descendant}
  os._exit(0)
 os.write(write,str(grandchild).encode())
 os.close(write)
 os._exit(0)
os.close(write)
grandchild=int(os.read(read,64))
os.close(read)
os.waitpid(child,0)
Path('adopted.pid').write_text(str(grandchild))
{parent}
"""
DETACHED = """os.setsid()
  Path('writer-ready').write_text('ready')
  while not Path('after-receipt').exists(): time.sleep(.005)
  Path('late-write').write_text('writer ran after receipt')"""
READY = """until=time.monotonic()+2
while not Path('writer-ready').exists():
 if time.monotonic()>until: raise RuntimeError('Writer not ready')
 time.sleep(.005)"""
INSPECT = """import json,time
from pathlib import Path
pid=int(Path('/workspace/adopted.pid').read_text())
Path('/workspace/after-receipt').write_text('1')
until=time.monotonic()+.2
while time.monotonic()<until and not Path('/workspace/late-write').exists(): time.sleep(.005)
print(json.dumps({'descendant_present':Path('/proc/'+str(pid)).exists(),'late_write':Path('/workspace/late-write').exists()}))
"""


def docker_arguments(tmp_path):
    configuration_file = os.environ.get("FORGE_TEST_DOCKER_CONFIGURATION")
    if configuration_file:
        return json.loads(Path(configuration_file).read_text())["docker"]
    docker = installed_docker_binary()
    context = os.environ.get("FORGE_TEST_DOCKER_CONTEXT", "colima-forge-tests")
    configuration = tmp_path / "docker"
    configuration.mkdir(mode=0o700)
    exported = subprocess.check_output([str(docker), "context", "export", context, "-"])
    subprocess.run(
        [str(docker), "--config", str(configuration), "context", "import", context, "-"],
        input=exported,
        check=True,
        capture_output=True,
    )
    return [str(docker), "--config", str(configuration), "--context", context]


@asynccontextmanager
async def launch(tmp_path, program, *, timeout=10, pids_limit=128):
    cli = docker_arguments(tmp_path)
    assert len(cli) == 5 and cli[1] == "--config" and cli[3] == "--context"
    state = tmp_path / "artifacts"
    state.mkdir(mode=0o700)

    async def transport(argv, timeout, limit, input_data=None):
        return await _bounded_process([*cli, *argv], state, timeout, limit, input_data=input_data)

    async def control(argv, timeout=10, limit=65_536, input_data=None):
        code, output = await transport(argv, timeout, limit, input_data)
        if code:
            raise RuntimeError("Owned launcher operation failed: " + argv[0] + " code=" + str(code))
        return output

    image = os.environ["FORGE_TEST_DOCKER_IMAGE"]
    daemon = (await control(["info", "--format", "{{.ID}}"])).decode().strip()
    profile = RunnerPoolProfile(
        profile_id="owned-lifecycle",
        daemon_id=daemon,
        approved_images=(image,),
        transfer_image=image,
        max_source_bytes=4_194_304,
        max_memory_mb=256,
        memory_budget_mb=512,
        max_timeout=15,
    )
    dispatcher = DockerPoolDispatcher(
        RunnerDockerAuthority(
            profile, Path(cli[0]), cli[4], Path(cli[2]), state, "forge-runner-life-" + uuid4().hex[:12], 10001, 10001
        ),
        transport=transport,
    )
    await dispatcher.verify_images()
    source = tmp_path / "source"
    source.mkdir()
    (source / "project.py").write_text(program)
    raw, digest = pack_workspace(source, profile.max_source_bytes)
    invocation = RunnerInvocation(
        execution_id=uuid4().hex,
        profile_id=profile.profile_id,
        profile_revision=profile.revision,
        repository_id=uuid4(),
        run_id=uuid4(),
        image=image,
        argv=("/app/.venv/bin/python", "project.py"),
        timeout=timeout,
        pids_limit=pids_limit,
        memory_mb=256,
        source_digest=source_tree_digest(source, profile.max_source_bytes),
        archive_digest=digest,
        source_bytes=len(raw),
    )
    artifacts = RunnerPoolArtifacts(state)
    artifacts.prepare(profile, invocation, raw)
    artifacts.retain(profile, invocation, "stdin", b"")
    try:
        helper = (await control(dispatcher.transfer_plan(invocation))).decode().strip()
        await control(
            ["cp", "--archive", "-", helper + ":/workspace"], input_data=artifacts.input_archive(profile, invocation)
        )
        await control(["start", "--attach", helper])
        observed = await dispatcher.inspect(invocation, helper)
        assert observed["State"]["ExitCode"] == 0
        volumes = {mount["Destination"]: mount["Name"] for mount in observed["Mounts"]}
        cid = (
            (await control(dispatcher.container_plan(invocation, volumes["/workspace"], volumes["/forge-git"])))
            .decode()
            .strip()
        )
        await control(["start", cid])
        async with asyncio.timeout(10):
            while True:
                code, _ = await transport(
                    [
                        "exec",
                        cid,
                        "/app/.venv/bin/python",
                        "-I",
                        "-S",
                        "-c",
                        "import os,stat;assert stat.S_ISFIFO(os.lstat('/tmp/forge-start').st_mode)",
                    ],
                    3,
                    4096,
                )
                if code == 0:
                    break
                await asyncio.sleep(0.02)
        await control(dispatcher.release_plan(invocation, cid))

        async def result():
            async with asyncio.timeout(timeout + 5):
                while True:
                    code, data = await transport(dispatcher.result_plan(invocation, cid), 3, 4096)
                    if code == 0:
                        value = json.loads(data)
                        assert value["protocol"] == "forge-runner-exit-v1"
                        assert value["execution_id"] == invocation.execution_id
                        return value
                    current = await dispatcher.inspect(invocation, cid)
                    if current is None or not current["State"]["Running"]:
                        raise RuntimeError("Launcher exited without a command status receipt")
                    await asyncio.sleep(0.02)

        async def execute(script):
            return await control(["exec", cid, "/app/.venv/bin/python", "-I", "-S", "-c", script])

        yield result, execute, control, dispatcher, invocation, cid
    finally:
        for name in (dispatcher.name(invocation), dispatcher.name(invocation) + "-transfer"):
            current = await dispatcher.inspect(invocation, name)
            if current is not None:
                await control(["container", "rm", "--force", "--volumes", current["Id"]])
                assert await dispatcher.inspect(invocation, current["Id"]) is None


async def test_adopted_zombie_is_absent_before_authenticated_exit_receipt(tmp_path):
    program = ORPHAN.format(descendant="pass", parent="time.sleep(.05)")
    async with launch(tmp_path, program) as (result, execute, *_):
        assert (await result())["exit_code"] == 0
        assert json.loads(await execute(INSPECT)) == {"descendant_present": False, "late_write": False}


async def test_detached_writer_cannot_run_after_authenticated_exit_receipt(tmp_path):
    program = ORPHAN.format(descendant=DETACHED, parent=READY)
    async with launch(tmp_path, program) as (result, execute, *_):
        assert (await result())["exit_code"] == 0
        assert json.loads(await execute(INSPECT)) == {"descendant_present": False, "late_write": False}


async def test_pid_quota_can_be_reused_while_preserving_nonzero_command_status(tmp_path):
    program = """import os,time
from pathlib import Path
for index in range(48):
 read,write=os.pipe()
 child=os.fork()
 if child==0:
  os.close(read)
  grandchild=os.fork()
  if grandchild==0: os._exit(0)
  os.write(write,str(grandchild).encode());os.close(write);os._exit(0)
 os.close(write);grandchild=int(os.read(read,64));os.close(read);os.waitpid(child,0)
 until=time.monotonic()+2
 while Path('/proc/'+str(grandchild)).exists():
  if time.monotonic()>until: raise RuntimeError('Adopted child was not reaped')
  time.sleep(.005)
raise SystemExit(7)
"""
    async with launch(tmp_path, program, pids_limit=32) as (result, *_):
        assert (await result())["exit_code"] == 7


async def test_timeout_preserves_status_and_drains_detached_children(tmp_path):
    program = ORPHAN.format(descendant=DETACHED, parent=READY + "\ntime.sleep(60)")
    async with launch(tmp_path, program, timeout=1) as (result, execute, *_):
        assert (await result())["exit_code"] == 124
        assert json.loads(await execute(INSPECT)) == {"descendant_present": False, "late_write": False}


@pytest.mark.parametrize("exit_code", [0, 7, 23])
async def test_direct_command_exit_status_is_preserved(tmp_path, exit_code):
    async with launch(tmp_path, "raise SystemExit(" + str(exit_code) + ")\n") as (result, *_):
        assert (await result())["exit_code"] == exit_code


async def test_pid1_termination_does_not_forge_a_completion_receipt(tmp_path):
    program = "from pathlib import Path\nimport time\nPath('started').touch()\ntime.sleep(60)\n"
    async with launch(tmp_path, program) as (result, execute, control, dispatcher, invocation, cid):
        await execute(
            "from pathlib import Path\nimport time\nuntil=time.monotonic()+2\n"
            "while not Path('/workspace/started').exists():\n"
            " if time.monotonic()>until: raise RuntimeError('Project did not start')\n time.sleep(.005)"
        )
        await control(["kill", "--signal=TERM", cid])
        with pytest.raises(RuntimeError, match="without a command status"):
            await result()
        observed = await dispatcher.inspect(invocation, cid)
        assert observed["State"]["ExitCode"] == 143
