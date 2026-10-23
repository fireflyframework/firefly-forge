# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Real stdio peers exercise metadata ordering, bounds and process cleanup."""

import asyncio
import json
import os
import sys

import pytest

from firefly_forge.providers import ProviderError, run_process


async def discard(_kind, _line):
    return None


async def test_metadata_rpc_waits_for_delayed_replies_before_closing_stdin(tmp_path):
    from firefly_forge.native_configuration_rpc import ConfigurationRPC

    peer = tmp_path / "peer.py"
    peer.write_text(
        "import json,select,sys,time\n"
        "first=json.loads(sys.stdin.readline())\n"
        "assert first['id']==1 and first['method']=='initialize'\n"
        "time.sleep(.03)\n"
        "print(json.dumps({'id':1,'result':{}}),flush=True)\n"
        "assert json.loads(sys.stdin.readline())['method']=='initialized'\n"
        "requests=[json.loads(sys.stdin.readline()),json.loads(sys.stdin.readline())]\n"
        "assert [r['method'] for r in requests]==['config/read','configRequirements/read']\n"
        "assert requests[0]['params']['cwd']==sys.argv[1]\n"
        "time.sleep(.03)\n"
        "if select.select([sys.stdin],[],[],0)[0]: sys.exit(32)\n"
        "print(json.dumps({'id':3,'result':{'requirements':None}}),flush=True)\n"
        "print(json.dumps({'id':2,'result':{'config':{'public_fixture':True}}}),flush=True)\n"
        "assert sys.stdin.read()==''\n"
    )
    rpc = ConfigurationRPC(tmp_path)
    result = await run_process(
        [sys.executable, "-I", "-B", str(peer), str(tmp_path.resolve())],
        tmp_path,
        discard,
        timeout=5,
        stdin_protocol=rpc,
    )
    assert result.returncode == 0
    assert rpc.results == {1: {}, 2: {"config": {"public_fixture": True}}, 3: {"requirements": None}}
    assert result.process_outcome.group_absent and result.process_outcome.streams_drained


@pytest.mark.parametrize(
    "packet",
    [
        {"id": True, "result": {}},
        {"id": 2.0, "result": {}},
        {"id": 4, "result": {}},
        {"id": 1, "error": {"message": "PRIVATE_TEST_VALUE"}},
        {"id": 1, "result": {}, "error": {}},
        {"id": 1},
        ["PRIVATE_TEST_VALUE"],
    ],
)
def test_metadata_rpc_rejects_invalid_envelopes_without_exposing_values(tmp_path, packet):
    from firefly_forge.native_configuration_rpc import ConfigurationRPC

    rpc = ConfigurationRPC(tmp_path)
    with pytest.raises(ValueError) as error:
        rpc.receive_stdout((json.dumps(packet) + "\n").encode())
    assert "PRIVATE_TEST_VALUE" not in str(error.value)


def test_metadata_rpc_bounds_packets_bytes_and_duplicate_ids(tmp_path):
    from firefly_forge.native_configuration_rpc import ConfigurationRPC

    rpc = ConfigurationRPC(tmp_path)
    rpc.receive_stdout(b'{"id":1,"result":{}}\n')
    with pytest.raises(ValueError):
        rpc.receive_stdout(b'{"id":1,"result":{}}\n')
    rpc = ConfigurationRPC(tmp_path)
    for _ in range(64):
        rpc.receive_stdout(b'{"method":"public/notification"}\n')
    with pytest.raises(ValueError):
        rpc.receive_stdout(b'{"method":"public/notification"}\n')
    with pytest.raises(ValueError):
        ConfigurationRPC(tmp_path).receive_stdout(b"x" * 1_000_001)


async def test_metadata_rpc_timeout_drains_owned_process(tmp_path):
    from firefly_forge.native_configuration_rpc import ConfigurationRPC

    pidfile = tmp_path / "pid"
    script = "import os,sys,time;open(sys.argv[1],'w').write(str(os.getpid()));time.sleep(10)"
    with pytest.raises(ProviderError, match="timed out"):
        await run_process(
            [sys.executable, "-I", "-B", "-c", script, str(pidfile)],
            tmp_path,
            discard,
            timeout=0.5,
            stdin_protocol=ConfigurationRPC(tmp_path),
        )
    with pytest.raises(ProcessLookupError):
        os.killpg(int(pidfile.read_text()), 0)


async def test_metadata_rpc_malformed_stream_interrupts_wait_without_timeout(tmp_path):
    from firefly_forge.native_configuration_rpc import ConfigurationRPC

    script = "import sys,time;print('PRIVATE_TEST_VALUE',flush=True);time.sleep(10)"
    with pytest.raises(ValueError) as error:
        async with asyncio.timeout(2):
            await run_process(
                [sys.executable, "-I", "-B", "-c", script],
                tmp_path,
                discard,
                timeout=5,
                stdin_protocol=ConfigurationRPC(tmp_path),
            )
    assert "PRIVATE_TEST_VALUE" not in str(error.value)


async def test_metadata_rpc_cannot_share_stdin_with_a_prompt(tmp_path):
    from firefly_forge.native_configuration_rpc import ConfigurationRPC

    with pytest.raises(ValueError, match="stdin"):
        await run_process(
            ["/path/that/must/not/start"],
            tmp_path,
            discard,
            timeout=1,
            stdin="unrelated prompt",
            stdin_protocol=ConfigurationRPC(tmp_path),
        )
