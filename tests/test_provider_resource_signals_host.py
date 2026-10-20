# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native resource-limit probes stay in disposable processes, never the test runner."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from firefly_forge import providers

pytestmark = pytest.mark.skipif(os.name != "posix" or not hasattr(signal, "SIGXFSZ"), reason="POSIX SIGXFSZ")


@pytest.fixture
def oversized_truncate(tmp_path):
    compiler = shutil.which("cc")
    if compiler is None:
        pytest.skip("A C compiler is required for the native resource-limit fixture")
    source = tmp_path / "truncate.c"
    source.write_text("""
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <signal.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <unistd.h>
int main(int argc, char **argv) {
    struct rlimit limit;
    struct stat info;
    if (getrlimit(RLIMIT_FSIZE, &limit) != 0) return 10;
    if (argc > 1) {
        if (limit.rlim_cur != RLIM_INFINITY || signal(SIGXFSZ, SIG_DFL) != SIG_DFL) return 14;
        puts("unlimited-default-signal");
        return 0;
    }
    int fd = open("bounded-file", O_RDWR);
    if (fd < 0) return 11;
    int result = ftruncate(fd, (off_t)limit.rlim_cur + 1);
    int error = errno;
    if (fstat(fd, &info) != 0 || close(fd) != 0) return 12;
    printf("%llu %llu %d %d %lld\\n", (unsigned long long)limit.rlim_cur,
           (unsigned long long)limit.rlim_max, result, error, (long long)info.st_size);
    return result == -1 && error == EFBIG && info.st_size == 7 ? 0 : 13;
}
""")
    binary = tmp_path / "truncate-fixture"
    subprocess.run([compiler, "-Wall", "-Werror", str(source), "-o", str(binary)], check=True, timeout=20)
    (tmp_path / "bounded-file").write_bytes(b"before\n")
    return binary


@pytest.mark.parametrize(
    ("launcher", "ignored", "limited", "expected_exit"),
    [
        ("trusted", True, True, 0),
        ("trusted", False, True, -getattr(signal, "SIGXFSZ", 25)),
        ("untrusted", True, True, -getattr(signal, "SIGXFSZ", 25)),
        ("trusted", True, False, 0),
    ],
)
def test_trusted_cli_preserves_ignored_fsize_signal_without_relaxing_kernel_limit(
    tmp_path, oversized_truncate, launcher, ignored, limited, expected_exit
):
    if not limited:
        import resource

        if resource.getrlimit(resource.RLIMIT_FSIZE)[1] != resource.RLIM_INFINITY:
            pytest.skip("The test parent cannot provide an unlimited native control")
    # Imports precede the resource limit; only the native fixture attempts an oversized write.
    probe = """
import asyncio,json,resource,signal,sys
from pathlib import Path
from firefly_forge.providers import run_process
from firefly_forge.process import execute
async def emit(*args): pass
resource.setrlimit(resource.RLIMIT_CORE,(0,0))
limited=sys.argv[4]=='True'
limit=(131072,131072) if limited else (resource.RLIM_INFINITY,resource.RLIM_INFINITY)
resource.setrlimit(resource.RLIMIT_FSIZE,limit)
signal.signal(signal.SIGXFSZ,signal.SIG_IGN if sys.argv[2]=='True' else signal.SIG_DFL)
async def main():
    command=[sys.argv[3]]+([] if limited else ['disposition'])
    if sys.argv[1]=='trusted':
        r=await run_process(command,Path.cwd(),emit,timeout=5,env={})
        value={'exit':r.returncode,'output':r.stdout,'outcome':r.process_outcome.model_dump()}
    else:
        r=await execute(command,Path.cwd(),timeout=5,env={})
        value={'exit':r.exit_code,'output':r.output}
    value['parent_limit']=list(resource.getrlimit(resource.RLIMIT_FSIZE))
    print(json.dumps(value))
asyncio.run(main())
"""
    environment = {
        "PATH": os.defpath,
        "HOME": str(tmp_path),
        "TMPDIR": str(tmp_path),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": os.pathsep.join([str(Path(providers.__file__).parents[1]), *sys.path]),
    }
    result = subprocess.run(
        [sys.executable, "-B", "-s", "-c", probe, launcher, str(ignored), str(oversized_truncate), str(limited)],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert observed["exit"] == expected_exit, observed
    assert observed["parent_limit"] == ([131072, 131072] if limited else [resource.RLIM_INFINITY] * 2)
    assert (tmp_path / "bounded-file").read_bytes() == b"before\n"
    if expected_exit == 0 and limited:
        import errno

        assert list(map(int, observed["output"].split())) == [131072, 131072, -1, errno.EFBIG, 7]
    if not limited:
        assert observed["output"] == "unlimited-default-signal\n"
    if launcher == "trusted":
        assert observed["outcome"] == {
            "exit_code": expected_exit,
            "leader_waited": True,
            "streams_drained": True,
            "group_absent": True,
        }
