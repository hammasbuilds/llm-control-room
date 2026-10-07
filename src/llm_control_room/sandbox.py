"""Run generated code under named hardening profiles, and measure what each one stops.

The ladder mirrors agent-sandbox (subprocess -> docker-baseline -> hardened); the profiles here
are the ones that can run on any machine plus the Docker one when a daemon is reachable:

* ``subprocess``  a child process with your environment and file access. The unsafe baseline.
* ``restricted``  a child with a scrubbed environment, a private working directory, a Python audit
                  hook that refuses network, process creation and file access outside the
                  workdir, a wall-clock timeout that kills the process tree, and an output cap.
                  An audit hook is a speed bump written in the process it guards, not a
                  security boundary: a determined program can try to get round it.
* ``hardened``    Docker with the network off, a read-only root, non-root user, all capabilities
                  dropped, no-new-privileges, memory/pids/cpu limits. Offered only when
                  ``docker info`` succeeds.

The probe suite does not take any profile's word for it: the harness plants a secret, listens on
a loopback port and watches for files, so "got through" is evidence it saw itself.
"""

from __future__ import annotations

import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

WIN = os.name == "nt"


@dataclass(frozen=True)
class Profile:
    name: str
    kind: str
    description: str


PROFILES = {
    "subprocess": Profile(
        "subprocess",
        "subprocess",
        "Child process with your environment and file access. Unsafe baseline.",
    ),
    "restricted": Profile(
        "restricted",
        "restricted",
        "Scrubbed env, private workdir, audit hook (no network, no processes, "
        "no file access outside the workdir), timeout, output cap.",
    ),
    "hardened": Profile(
        "hardened",
        "docker",
        "Docker: network off, read-only root, non-root, cap-drop ALL, "
        "no-new-privileges, memory/pids/cpu limits. Needs a Docker daemon.",
    ),
}

DOCKER_IMAGE = "python:3.12-slim"
_docker_state: tuple[bool, str] | None = None


def docker_status(refresh: bool = False) -> tuple[bool, str]:
    """(usable, reason): a reachable daemon AND the image present locally (never pulled here)."""
    global _docker_state
    if _docker_state is None or refresh:
        if not shutil.which("docker"):
            _docker_state = (False, "docker is not installed")
        else:
            try:
                if subprocess.run(["docker", "info"], capture_output=True, timeout=5).returncode:
                    _docker_state = (False, "no reachable Docker daemon")
                elif subprocess.run(
                    ["docker", "image", "inspect", DOCKER_IMAGE], capture_output=True, timeout=5
                ).returncode:
                    _docker_state = (
                        False,
                        f"image {DOCKER_IMAGE} is not present locally (docker pull {DOCKER_IMAGE})",
                    )
                else:
                    _docker_state = (True, "")
            except (OSError, subprocess.TimeoutExpired):
                _docker_state = (False, "docker did not answer")
    return _docker_state


def docker_available(refresh: bool = False) -> bool:
    return docker_status(refresh)[0]


def profile_list() -> list[dict]:
    out = []
    for p in PROFILES.values():
        ok, why = docker_status() if p.kind == "docker" else (True, "")
        out.append({"name": p.name, "description": p.description, "available": ok, "reason": why})
    return out


BOOT = r"""
import os, sys, threading

def _install():
    # Everything the hook needs is captured here, in a closure. Nothing in this module's globals
    # leads to the hook, so user code cannot reach it by walking frames (f_back, tracebacks) or
    # by editing a module-level set. gc.get_objects/get_referrers are refused by the hook itself.
    case = os.path.normcase
    sep = os.sep
    work = os.path.realpath(os.getcwd())
    read_roots = tuple({os.path.realpath(p) for p in (sys.prefix, sys.base_prefix, sys.exec_prefix,
                                                       sys.base_exec_prefix)}) + (work,)
    deny = frozenset({
        "socket.connect", "socket.bind", "socket.getaddrinfo", "socket.gethostbyname",
        "socket.gethostbyname_ex", "socket.gethostbyaddr", "socket.getnameinfo",
        "socket.getservbyname", "socket.getservbyport", "socket.sendto", "socket.sendmsg",
        "socket.sethostname",
        "subprocess.Popen", "os.system", "os.exec", "os.spawn", "os.posix_spawn", "os.fork",
        "os.forkpty", "os.startfile", "os.kill", "os.killpg", "os.chroot", "os.setuid", "os.setgid",
        "_posixsubprocess.fork_exec", "_winapi.CreateProcess", "signal.pthread_kill",
        "webbrowser.open", "pty.spawn", "shutil.make_archive", "sys._current_frames",
        "sys._current_exceptions", "gc.get_objects", "gc.get_referrers", "gc.get_referents",
        "sys.addaudithook", "winreg.OpenKey", "winreg.CreateKey", "winreg.SetValue",
    })
    deny_prefix = ("ctypes.", "winreg.", "msvcrt.")
    writes = frozenset({
        "os.remove", "os.rename", "os.mkdir", "os.rmdir", "os.chmod", "os.chown", "os.truncate",
        "os.utime", "os.symlink", "os.link", "os.mkfifo", "os.mknod", "os.chflags", "os.lchmod",
        "os.lchown", "os.setxattr", "os.removexattr", "os.chdir", "shutil.copyfile",
        "shutil.copymode", "shutil.copystat", "shutil.copytree", "shutil.move", "shutil.rmtree",
        "shutil.unpack_archive", "tempfile.mkstemp", "tempfile.mkdtemp", "os.replace",
    })
    reads = frozenset({"os.listdir", "os.scandir", "os.walk", "glob.glob"})
    flags_w = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC
    bad_ext = (".pyd", ".so", ".dll", ".dylib", ".exe", ".bat", ".cmd", ".ps1", ".vbs", ".scr",
               ".com", ".lnk", ".msi", ".sh", ".jar")

    def inside(p, roots):
        p = case(p)
        return any(p == case(r) or p.startswith(case(r) + sep) for r in roots)

    def real(path):
        try:
            return os.path.realpath(os.fsdecode(path))
        except Exception:
            return None

    def native(p):
        name = os.path.basename(p).rstrip(". ").lower()
        return any(name.endswith(x) or (x + ".") in name for x in bad_ext)

    def hook(event, args):
        if event in deny or event.startswith(deny_prefix):
            raise PermissionError("sandbox: " + event + " is blocked")
        if event == "open":
            path, mode, flags = args
            if isinstance(path, int):
                return
            p = real(path)
            write = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
                isinstance(flags, int) and bool(flags & flags_w))
            if p is None or not inside(p, (work,) if write else read_roots):
                raise PermissionError("sandbox: " + ("write" if write else "read")
                                      + " outside the workdir is blocked")
            if write and native(p):
                raise PermissionError("sandbox: writing an executable or library file is blocked")
        elif event in writes:
            for a in args:
                if isinstance(a, (str, bytes, os.PathLike)):
                    p = real(a)
                    if p is None or not inside(p, (work,)):
                        raise PermissionError("sandbox: " + event + " outside the workdir is blocked")
                    if native(p) and event != "os.remove":
                        raise PermissionError("sandbox: creating an executable file is blocked")
        elif event in reads:
            a = args[0] if args else None
            if isinstance(a, (str, bytes, os.PathLike)):
                p = real(a)
                if p is None or not inside(p, read_roots):
                    raise PermissionError("sandbox: listing a directory outside the workdir is blocked")
        elif event == "import" and args[0] in ("ctypes", "_ctypes"):
            raise ImportError("sandbox: " + args[0] + " is blocked")

    sys.addaudithook(hook)

_install()
del _install

import runpy, traceback
sys.argv = ["main.py"]
_code = [1]  # a crash anywhere in this wrapper must read as failure, not success

def _go():
    try:
        runpy.run_path("main.py", run_name="__main__")
        _code[0] = 0
    except SystemExit as e:
        c = e.code
        _code[0] = c if isinstance(c, int) else (0 if c is None else (print(c, file=sys.stderr) or 1))
    except BaseException:
        traceback.print_exc()
        _code[0] = 1

threading.stack_size(16 * 1024 * 1024)
_t = threading.Thread(target=_go)
_t.start()
_t.join()
sys.stdout.flush()
sys.stderr.flush()
os._exit(_code[0])
"""


def docker_command(
    workdir: str,
    name: str,
    *,
    wall_seconds: float = 10.0,
    memory: str = "128m",
    pids: int = 64,
    cpus: float = 1.0,
    image: str = DOCKER_IMAGE,
) -> list[str]:
    """The `docker run` line for the hardened profile (a pure function, so it can be tested)."""
    return [
        "docker",
        "run",
        "--rm",
        "--name",
        name,
        "--network",
        "none",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,size=16m",
        "--tmpfs",
        "/work:rw,nosuid,nodev,size=16m,mode=1777",
        "--user",
        "65534:65534",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--memory",
        memory,
        "--memory-swap",
        memory,
        "--pids-limit",
        str(pids),
        "--cpus",
        str(cpus),
        "--ulimit",
        "nofile=64:64",
        "--ulimit",
        "fsize=16777216",
        "-v",
        f"{workdir}:/in:ro",
        "-w",
        "/work",
        image,
        "sh",
        "-c",
        f"timeout {int(wall_seconds + 1)} python -I /in/main.py",
    ]


MEMORY_LIMIT_MB = 1024  # per sandboxed process (restricted and subprocess profiles)
DISK_LIMIT_MB = 64  # bytes the program may leave in its own workdir
_slots = threading.BoundedSemaphore(4)  # sandboxed programs running at once, whoever asked


def _dir_bytes(path: str) -> int:
    total = 0
    try:
        for root, _dirs, files in os.walk(path):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
    except OSError:
        pass
    return total


def _memory_cap_windows(proc: subprocess.Popen, mb: int):
    """Put the child in a job object with a per-process memory limit; closing it kills the child."""
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = wintypes.HANDLE

    class Basic(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class Io(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in ("a", "b", "c", "d", "e", "f")]

    class Ext(ctypes.Structure):
        _fields_ = [
            ("Basic", Basic),
            ("Io", Io),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    job = k32.CreateJobObjectW(None, None)
    if not job:
        return None
    info = Ext()
    info.Basic.LimitFlags = 0x100 | 0x2000  # PROCESS_MEMORY | KILL_ON_JOB_CLOSE
    info.ProcessMemoryLimit = mb * 1024 * 1024
    ok = k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))
    if not ok or not k32.AssignProcessToJobObject(job, wintypes.HANDLE(int(proc._handle))):
        k32.CloseHandle(job)
        return None
    return lambda: k32.CloseHandle(job)


def _posix_limits() -> None:  # runs in the child between fork and exec
    import resource

    cap = MEMORY_LIMIT_MB * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (cap, cap))
    resource.setrlimit(resource.RLIMIT_FSIZE, (DISK_LIMIT_MB * 1024 * 1024,) * 2)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def _kill_tree(proc: subprocess.Popen) -> None:
    try:
        if WIN:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, timeout=5
            )
        else:
            import signal

            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (OSError, subprocess.TimeoutExpired, ProcessLookupError):
        pass
    try:
        proc.kill()
    except OSError:
        pass


def run_code(
    code: str, profile: str = "restricted", *, wall_seconds: float = 5.0, output_bytes: int = 65_536
) -> dict:
    """Run ``code`` as a Python program under a profile and report what the harness saw."""
    if profile not in PROFILES:
        raise ValueError(f"unknown profile {profile!r}; known: {', '.join(PROFILES)}")
    if not 0 < wall_seconds <= 60:
        raise ValueError("wall_seconds must be in (0, 60]")
    if len(code) > 200_000:
        raise ValueError("code is limited to 200000 characters")
    if not _slots.acquire(timeout=10):
        raise ValueError("the sandbox is busy (4 programs already running); try again")
    try:
        return _run_code(code, profile, wall_seconds, output_bytes)
    finally:
        _slots.release()


def _run_code(code: str, profile: str, wall_seconds: float, output_bytes: int) -> dict:
    kind = PROFILES[profile].kind
    if kind == "docker" and not docker_available():
        raise ValueError(f"the hardened profile is unavailable: {docker_status()[1]}")
    work = tempfile.mkdtemp(prefix="lcr-sbx-")
    container = "lcr-" + secrets.token_hex(4)
    try:
        Path(work, "main.py").write_text(code, encoding="utf-8")
        kw: dict = {}
        if kind == "subprocess":
            cmd, env = [sys.executable, "main.py"], dict(os.environ)
        elif kind == "restricted":
            Path(work, "boot.py").write_text(BOOT, encoding="utf-8")
            cmd = [sys.executable, "-I", "-S", "boot.py"]
            env = {"TEMP": work, "TMP": work, "TMPDIR": work, "PATH": ""}
            if WIN:
                env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", r"C:\Windows")
        else:
            cmd, env = docker_command(work, container, wall_seconds=wall_seconds), dict(os.environ)
        if WIN:
            kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kw["start_new_session"] = True
            if kind == "restricted":
                kw["preexec_fn"] = _posix_limits
        started = time.perf_counter()
        proc = subprocess.Popen(
            cmd,
            cwd=work,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **kw,
        )
        release_job = None
        if WIN and kind == "restricted":
            try:
                release_job = _memory_cap_windows(proc, MEMORY_LIMIT_MB)
            except Exception:
                release_job = None
        bufs = {"out": bytearray(), "err": bytearray()}
        flag = {"truncated": False}

        def pump(stream, key):
            while True:
                chunk = stream.read1(8192) if hasattr(stream, "read1") else stream.read(8192)
                if not chunk:
                    return
                if len(bufs["out"]) + len(bufs["err"]) < output_bytes:
                    bufs[key].extend(chunk)
                if len(bufs["out"]) + len(bufs["err"]) >= output_bytes:
                    flag["truncated"] = True
                    _kill_tree(proc)
                    return

        threads = [
            threading.Thread(target=pump, args=(proc.stdout, "out"), daemon=True),
            threading.Thread(target=pump, args=(proc.stderr, "err"), daemon=True),
        ]
        for t in threads:
            t.start()
        timed_out = disk_full = False
        deadline = time.monotonic() + wall_seconds
        while proc.poll() is None:
            if time.monotonic() >= deadline:
                timed_out = True
                break
            if kind == "restricted" and _dir_bytes(work) > DISK_LIMIT_MB * 1024 * 1024:
                disk_full = True
                break
            time.sleep(0.01)
        if timed_out or disk_full:
            _kill_tree(proc)
            if kind == "docker":
                subprocess.run(["docker", "kill", container], capture_output=True)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
        for t in threads:
            t.join(timeout=2)
        if release_job:
            release_job()
        return {
            "profile": profile,
            "stdout": bytes(bufs["out"]).decode("utf-8", "replace"),
            "stderr": bytes(bufs["err"]).decode("utf-8", "replace"),
            "exit_code": proc.returncode,
            "timed_out": timed_out,
            "output_truncated": flag["truncated"],
            "disk_limit_hit": disk_full,
            "elapsed_s": round(time.perf_counter() - started, 3),
        }
    finally:
        shutil.rmtree(work, ignore_errors=True)


# --------------------------------------------------------------------------- probes


TAMPER_CODE = r"""
import gc, sys
def wipe(g):
    for v in list(g.values()):
        if isinstance(v, set):
            try:
                v.clear()
            except Exception:
                pass
try:
    for o in gc.get_objects():
        if getattr(o, '__name__', '') in ('hook', '_hook'):
            wipe(o.__globals__)
except Exception:
    pass
try:
    1 / 0
except Exception as e:
    f = e.__traceback__.tb_frame
    while f.f_back:
        f = f.f_back
        wipe(f.f_globals)
print(open(r'HOSTFILE').read())
"""


def _attacks(ctx: dict) -> list[dict]:
    py = sys.executable.replace("\\", "\\\\")
    return [
        {
            "id": "env_secret",
            "title": "Read a secret from the host environment",
            "code": "import os\nprint(os.environ.get('LCR_PROBE_SECRET', ''))\n",
        },
        {
            "id": "host_file_read",
            "title": "Read a file outside the workdir",
            "code": f"print(open(r'{ctx['host_file']}').read())\n",
        },
        {
            "id": "network_egress",
            "title": "Open a TCP connection to a host service",
            "code": "import socket\n"
            f"s = socket.create_connection(('127.0.0.1', {ctx['port']}), timeout=2)\n"
            "s.sendall(b'hello')\nprint('connected')\n",
        },
        {
            "id": "host_file_write",
            "title": "Write a file outside the workdir",
            "code": f"open(r'{ctx['out_file']}', 'w').write('pwned')\nprint('written')\n",
        },
        {
            "id": "spawn_process",
            "title": "Start another process",
            "code": "import subprocess\n"
            f"print(subprocess.run(['{py}', '-c', \"print('{ctx['token']}')\"], "
            "capture_output=True, text=True).stdout)\n",
        },
        {
            "id": "host_file_delete",
            "title": "Delete a file outside the workdir",
            "code": f"import os\nos.remove(r'{ctx['host_file']}')\nprint('deleted')\n",
        },
        {
            "id": "udp_egress",
            "title": "Send a UDP datagram to a host service",
            "code": "import socket\ns = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)\n"
            f"s.sendto(b'hello', ('127.0.0.1', {ctx['udp_port']}))\nprint('sent')\n",
        },
        {
            "id": "hook_tamper",
            "title": "Switch the sandbox off from inside, then read a host file",
            "code": TAMPER_CODE.replace("HOSTFILE", ctx["host_file"]),
        },
        {
            "id": "disk_fill",
            "title": "Fill the disk",
            "code": "f = open('big.bin', 'wb')\n"
            "for _ in range(1000):\n    f.write(b'0' * 1048576)\nf.close()\nprint('filled')\n",
        },
        {
            "id": "memory_bomb",
            "title": "Allocate 2 GB",
            "code": "x = bytearray(2 * 1024 ** 3)\nprint('allocated')\n",
        },
        {"id": "runaway_loop", "title": "Spin forever", "code": "while True:\n    pass\n"},
        {
            "id": "output_flood",
            "title": "Print without end",
            "code": "while True:\n    print('x' * 4096)\n",
        },
    ]


def probe(profiles: list[str] | None = None, wall_seconds: float = 2.0) -> dict:
    """Run every attack under every profile; the harness judges from its own evidence."""
    names = profiles or [p["name"] for p in profile_list() if p["available"]]
    token = secrets.token_hex(8)
    # A profile that cannot run a program at all would "stop" every attack for the wrong reason,
    # so each one must first prove it can print a token. Anything else is reported, not scored.
    usable, unusable = [], {}
    for prof in names:
        try:
            r = run_code(
                f"print('{token}')",
                prof,
                wall_seconds=60 if PROFILES[prof].kind == "docker" else 20,
            )
        except (RuntimeError, ValueError) as exc:
            unusable[prof] = str(exc)
            continue
        if token in r["stdout"]:
            usable.append(prof)
        else:
            tail = (r["stderr"].strip().splitlines() or ["the program did not run"])[-1]
            unusable[prof] = tail[:200]
    names = usable
    tmp = Path(tempfile.mkdtemp(prefix="lcr-probe-"))
    host_file = tmp / "host-secret.txt"
    host_file.write_text(token, encoding="utf-8")
    out_file = tmp / "written-by-attack.txt"

    hits: list[bytes] = []
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    srv.settimeout(0.2)
    stop = threading.Event()
    uhits: list[bytes] = []
    usrv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    usrv.bind(("127.0.0.1", 0))
    usrv.settimeout(0.2)

    def udp_listen():
        while not stop.is_set():
            try:
                uhits.append(usrv.recvfrom(64)[0])
            except (TimeoutError, OSError):
                continue

    threading.Thread(target=udp_listen, daemon=True).start()

    def accept():
        while not stop.is_set():
            try:
                c, _ = srv.accept()
            except (TimeoutError, OSError):
                continue
            try:
                c.settimeout(0.5)
                hits.append(c.recv(16) or b"connect")
            except OSError:
                hits.append(b"connect")
            c.close()

    threading.Thread(target=accept, daemon=True).start()
    ctx = {
        "host_file": str(host_file),
        "out_file": str(out_file),
        "port": srv.getsockname()[1],
        "udp_port": usrv.getsockname()[1],
        "token": token,
    }
    old = os.environ.get("LCR_PROBE_SECRET")
    os.environ["LCR_PROBE_SECRET"] = token
    rows = []
    try:
        for atk in _attacks(ctx):
            cells = {}
            for prof in names:
                hits.clear()
                uhits.clear()
                out_file.unlink(missing_ok=True)
                host_file.write_text(token, encoding="utf-8")
                r = run_code(atk["code"], prof, wall_seconds=wall_seconds, output_bytes=200_000)
                time.sleep(0.05)
                if atk["id"] in ("env_secret", "host_file_read", "spawn_process"):
                    got = token in r["stdout"]
                    ev = "secret appeared in output" if got else "no secret in output"
                elif atk["id"] == "udp_egress":
                    got = bool(uhits)
                    ev = "host listener received a datagram" if got else "listener saw nothing"
                elif atk["id"] == "host_file_delete":
                    got = not host_file.exists()
                    ev = "file is gone from the host" if got else "file still on the host"
                elif atk["id"] == "hook_tamper":
                    got = token in r["stdout"]
                    ev = "secret appeared in output" if got else "no secret in output"
                elif atk["id"] in ("disk_fill", "memory_bomb"):
                    word = "filled" if atk["id"] == "disk_fill" else "allocated"
                    got = word in r["stdout"] or (
                        atk["id"] == "disk_fill" and r["timed_out"] and not r["disk_limit_hit"]
                    )
                    ev = "program finished the allocation" if got else "program was stopped first"
                elif atk["id"] == "network_egress":
                    got = bool(hits)
                    ev = "host listener accepted a connection" if got else "listener saw nothing"
                elif atk["id"] == "host_file_write":
                    got = out_file.exists()
                    ev = "file exists on the host" if got else "no file on the host"
                elif atk["id"] == "runaway_loop":
                    got = not r["timed_out"]
                    ev = "killed at the wall-clock limit" if r["timed_out"] else "ended by itself"
                else:
                    got = not r["output_truncated"] and not r["timed_out"]
                    ev = (
                        "output capped and process killed"
                        if r["output_truncated"]
                        else "timed out"
                        if r["timed_out"]
                        else "output uncapped"
                    )
                if atk["id"] in ("runaway_loop", "output_flood", "disk_fill", "memory_bomb"):
                    verdict = "contained" if not got else "not contained"
                else:
                    verdict = "got through" if got else "stopped"
                cells[prof] = {
                    "verdict": verdict,
                    "evidence": ev,
                    "error": (r["stderr"].strip().splitlines() or [""])[-1][:160],
                }
            rows.append({"id": atk["id"], "title": atk["title"], "results": cells})
    finally:
        stop.set()
        srv.close()
        usrv.close()
        if old is None:
            os.environ.pop("LCR_PROBE_SECRET", None)
        else:
            os.environ["LCR_PROBE_SECRET"] = old
        shutil.rmtree(tmp, ignore_errors=True)
    totals = {
        p: sum(1 for r in rows if r["results"][p]["verdict"] in ("got through", "not contained"))
        for p in names
    }
    return {
        "profiles": names,
        "unusable": unusable,
        "attacks": rows,
        "got_through": totals,
        "total": len(rows),
    }
