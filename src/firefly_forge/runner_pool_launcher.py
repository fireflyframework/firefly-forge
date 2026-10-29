# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Fixed approved-image PID1 supervisor and bounded authenticated local status reader."""

# No project strings or credentials are interpolated into these programs. The
# controller copies a bounded invocation.json beside source into a read-only volume.
LAUNCHER = """import ctypes,json,os,shutil,signal,socket,subprocess
PR_SET_DUMPABLE=4
assert os.getpid()==1
assert ctypes.CDLL(None).prctl(PR_SET_DUMPABLE,0,0,0,0)==0
def terminate(signum,frame): os._exit(124 if signum==signal.SIGALRM else 143)
for signum in (signal.SIGALRM,signal.SIGTERM,signal.SIGINT): signal.signal(signum,terminate)
signal.alarm(120)
assert b'GNU coreutils' in subprocess.check_output(['/usr/bin/timeout','--version'],timeout=5)
with open('/forge-input/invocation.json','r') as source: config=json.load(source)
shutil.copytree('/forge-input/source','/workspace',dirs_exist_ok=True,symlinks=True)
os.mkfifo('/tmp/forge-start',0o600)
with open('/tmp/forge-start','rb',buffering=0) as gate:
 if gate.read(1)!=b'1': os._exit(125)
os.unlink('/tmp/forge-start')
signal.alarm(config['timeout']+3)
stdin=open('/forge-input/stdin.bin','rb')
process=subprocess.Popen(['/usr/bin/timeout','--signal=TERM','--kill-after=2s',str(config['timeout']),*config['argv']],cwd='/workspace/'+config['cwd'],stdin=stdin,start_new_session=True)
while True:
 child,wait_status=os.waitpid(-1,0)
 if child==process.pid:
  code=process.returncode=os.waitstatus_to_exitcode(wait_status)
  break
try: os.killpg(process.pid,signal.SIGKILL)
except ProcessLookupError: pass
# PID1 alone reaps these children, preventing PID reuse between listing and kill.
while True:
 with open('/proc/self/task/1/children') as children: adopted=children.read().split()
 for child in adopted:
  try: os.kill(int(child),signal.SIGKILL)
  except ProcessLookupError: pass
 try: os.waitpid(-1,0)
 except ChildProcessError: break
signal.alarm(60)
status=json.dumps({'protocol':'forge-runner-exit-v1','execution_id':config['execution_id'],'exit_code':code}).encode()
server=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
server.bind('/tmp/forge-result.sock')
server.listen(8)
while True:
 connection,_=server.accept()
 connection.settimeout(1)
 try: connection.sendall(status)
 except (BrokenPipeError,TimeoutError): pass
 finally: connection.close()
"""

RESULT_READER = """import json,os,socket,struct
client=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
client.settimeout(1)
client.connect('/tmp/forge-result.sock')
pid,uid,gid=struct.unpack('3i',client.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
assert pid==1 and uid==os.getuid() and gid==os.getgid()
data=bytearray()
while chunk:=client.recv(4096):
 data.extend(chunk)
 assert len(data)<=4096
value=json.loads(data)
assert value['protocol']=='forge-runner-exit-v1'
print(json.dumps(value))
"""

OWNER_READER = """import ctypes,json,os,socket,struct,sys
assert ctypes.CDLL(None).prctl(4,0,0,0,0)==0
payload=sys.stdin.buffer.read(4097)
assert 0<len(payload)<=4096
client=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
client.settimeout(1)
client.connect('/tmp/forge-owner.sock')
pid,uid,gid=struct.unpack('3i',client.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
assert pid==1 and uid==os.getuid() and gid==os.getgid()
client.sendall(payload)
client.shutdown(socket.SHUT_WR)
data=bytearray()
while chunk:=client.recv(4096):
 data.extend(chunk)
 assert len(data)<=4096
value=json.loads(data)
assert value['protocol']=='forge-runner-owner-status-v2'
print(json.dumps(value))
"""


def owned_launcher() -> str:
    """Embed the tested ownership reducer; no project imports or keys enter argv."""
    import inspect

    from firefly_forge.runner_pool_owner import OwnerLiveness, _owner_body

    prefix = (
        "from __future__ import annotations\n"
        "import ctypes,hashlib,hmac,json,os,re,secrets,shutil,signal,socket,subprocess,sys,threading,time\n"
    )
    reducer = inspect.getsource(_owner_body) + "\n" + inspect.getsource(OwnerLiveness)
    program = """
assert os.getpid()==1
assert ctypes.CDLL(None).prctl(4,0,0,0,0)==0
def terminate(signum,frame): os._exit(124 if signum==signal.SIGALRM else 143)
for signum in (signal.SIGALRM,signal.SIGTERM,signal.SIGINT): signal.signal(signum,terminate)
signal.alarm(120)
assert b'GNU coreutils' in subprocess.check_output(['/usr/bin/timeout','--version'],timeout=5)
with open('/forge-input/invocation.json','r') as source: config=json.load(source)
assert config['work_duration']=={'protocol':'forge-work-duration-v2','timeout_seconds':config['timeout']}
assert type(config['max_output_bytes']) is int and 1024<=config['max_output_bytes']<=2000000
shutil.copytree('/forge-input/source','/workspace',dirs_exist_ok=True,symlinks=True)
owner=OwnerLiveness(config['execution_id'],secrets.token_bytes(32),config['owner_grace_seconds'],signal.alarm)
released=threading.Event()
server=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
server.bind('/tmp/forge-owner.sock')
os.chmod('/tmp/forge-owner.sock',0o600)
server.listen(8)
def serve():
 while True:
  connection,_=server.accept()
  connection.settimeout(1)
  try:
   data=bytearray()
   while chunk:=connection.recv(4096):
    data.extend(chunk)
    if len(data)>4096: raise ValueError('Owner message exceeds its bound')
   reply=owner.handle(json.loads(data))
   connection.sendall(json.dumps(reply).encode())
   if reply['state']=='running': released.set()
  except (ValueError,KeyError,TypeError,OSError): pass
  finally: connection.close()
threading.Thread(target=serve,daemon=True).start()
released.wait()
argv=config['argv']
if config['timeout'] is not None:
 assert type(config['timeout']) is int and config['timeout']>0
 argv=['/usr/bin/timeout','--signal=TERM','--kill-after=2s',str(config['timeout']),*argv]
stdin=open('/forge-input/stdin.bin','rb')
process=subprocess.Popen(argv,cwd='/workspace/'+config['cwd'],stdin=stdin,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,start_new_session=True)
output_failed=threading.Event()
def output():
 try:
  size=0
  while chunk:=process.stdout.read(65536):
   remaining=config['max_output_bytes']-size
   sys.stdout.buffer.write(chunk[:remaining]); sys.stdout.buffer.flush()
   size+=len(chunk)
   if size>config['max_output_bytes']:
    try: os.killpg(process.pid,signal.SIGKILL)
    except ProcessLookupError: pass
    return
 except BaseException:
  output_failed.set()
  try: os.killpg(process.pid,signal.SIGKILL)
  except ProcessLookupError: pass
reader=threading.Thread(target=output,daemon=True)
reader.start()
while True:
 child,wait_status=os.waitpid(-1,0)
 if child==process.pid:
  code=process.returncode=os.waitstatus_to_exitcode(wait_status)
  break
owner.begin_drain()
try: os.killpg(process.pid,signal.SIGKILL)
except ProcessLookupError: pass
while True:
 with open('/proc/self/task/1/children') as children: adopted=children.read().split()
 for child in adopted:
  try: os.kill(int(child),signal.SIGKILL)
  except ProcessLookupError: pass
 try: os.waitpid(-1,0)
 except ChildProcessError: break
reader.join(timeout=5)
if reader.is_alive() or output_failed.is_set(): os._exit(125)
owner.finish(code)
while True: signal.pause()
"""
    return prefix + reducer + program


def workspace_collector(limit: int) -> str:
    """Use the audited reader itself, without importing image or project packages."""
    import inspect

    from firefly_forge import credential_text_stream, sandbox, secrets, workspace_tools

    functions = (
        workspace_tools._private_name,
        sandbox._private_path,
        sandbox._directory_fd,
        sandbox._regular_data,
        secrets.sensitive_name,
        secrets.credential_representations,
        secrets.redact_environment,
        secrets.credential_values,
        credential_text_stream.text_chunks,
        credential_text_stream._scalar,
        credential_text_stream.raw_documents,
        secrets.KnownCredentialScan,
        secrets.contains_known_credential,
        sandbox.pack_workspace,
    )
    prefix = (
        "from __future__ import annotations\n"
        "import hashlib,io,json,math,os,re,stat,sys,tarfile\n"
        "from pathlib import Path\n"
        "from typing import Any\n"
        "from urllib.parse import unquote,urlsplit\n"
        "from contextvars import ContextVar\n"
        "_ephemeral=ContextVar('forge_ephemeral_credentials',default=())\n"
        f"TEXT_CHARS={credential_text_stream.TEXT_CHARS!r}\n"
        f"PRIVATE_BASENAMES=frozenset({sorted(workspace_tools.PRIVATE_BASENAMES)!r})\n"
        f"PUBLIC_ENV_SUFFIXES={workspace_tools.PUBLIC_ENV_SUFFIXES!r}\n"
        f"PRIVATE_KEY_SUFFIXES={workspace_tools.PRIVATE_KEY_SUFFIXES!r}\n"
    )
    return (
        prefix
        + "\n".join(inspect.getsource(function) for function in functions)
        + ("\ndata,_=pack_workspace(Path('/workspace')," + str(limit) + ")\nsys.stdout.buffer.write(data)\n")
    )
