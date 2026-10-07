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
import os, runpy, sys
_W = os.path.realpath(os.getcwd())
_READ = {os.path.realpath(p) for p in (sys.prefix, sys.base_prefix, sys.exec_prefix,
                                       sys.base_exec_prefix)} | {_W}
_DENY = {"socket.connect", "socket.bind", "socket.getaddrinfo", "socket.gethostbyname",
         "subprocess.Popen", "os.system", "os.exec", "os.spawn", "os.posix_spawn", "os.fork",
         "os.forkpty", "ctypes.dlopen", "ctypes.cdll", "winreg.OpenKey", "shutil.rmtree"}
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC
def _inside(p, roots):
    return any(p == r or p.startswith(r + os.sep) for r in roots)
def _hook(event, args):
    if event in _DENY:
        raise PermissionError("sandbox: " + event + " is blocked")
    if event == "open":
        path, mode, flags = args
        if isinstance(path, int):
            return
        p = os.path.realpath(os.fsdecode(path))
        write = (isinstance(mode, str) and any(c in mode for c in "wax+")) or \
                (isinstance(flags, int) and bool(flags & _WRITE_FLAGS))
        if not _inside(p, {_W} if write else _READ):
            raise PermissionError("sandbox: " + ("write" if write else "read") + " outside the workdir is blocked")
    if event == "import" and args[0] in ("ctypes", "_ctypes"):
        raise ImportError("sandbox: ctypes is blocked")
sys.addaudithook(_hook)
sys.argv = ["main.py"]
runpy.run_path("main.py", run_name="__main__")
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
        timed_out = False
        try:
            proc.wait(timeout=wall_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_tree(proc)
            if kind == "docker":
                subprocess.run(["docker", "kill", container], capture_output=True)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
        for t in threads:
            t.join(timeout=2)
        return {
            "profile": profile,
            "stdout": bytes(bufs["out"]).decode("utf-8", "replace"),
            "stderr": bytes(bufs["err"]).decode("utf-8", "replace"),
            "exit_code": proc.returncode,
            "timed_out": timed_out,
            "output_truncated": flag["truncated"],
            "elapsed_s": round(time.perf_counter() - started, 3),
        }
    finally:
        shutil.rmtree(work, ignore_errors=True)


# --------------------------------------------------------------------------- probes


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
            r = run_code(f"print('{token}')", prof, wall_seconds=20)
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
                out_file.unlink(missing_ok=True)
                r = run_code(atk["code"], prof, wall_seconds=wall_seconds, output_bytes=200_000)
                time.sleep(0.05)
                if atk["id"] in ("env_secret", "host_file_read", "spawn_process"):
                    got = token in r["stdout"]
                    ev = "secret appeared in output" if got else "no secret in output"
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
                if atk["id"] in ("runaway_loop", "output_flood"):
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
