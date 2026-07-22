#!/usr/bin/env python3
"""
Hard supervisor for Gemini API on Windows.
- Spawns api_server.py as a child process (no stdout pipe — no hang)
- Restarts immediately if child exits
- Writes PID/status files for keep_alive.ps1
- Survives as long as THIS process is alive (Task Scheduler keeps it up)

Run:
  python supervisor.py
  python supervisor.py --background   # detach on Windows
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)
STATUS_FILE = LOG_DIR / "supervisor.status"
PID_FILE = LOG_DIR / "api_server.pid"
SUPERVISOR_PID = LOG_DIR / "supervisor.pid"
LOG_FILE = LOG_DIR / "supervisor.log"

RESTART_DELAY = 2.0
PORT = os.environ.get("PORT", "8000")


def log(msg: str) -> None:
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    print(line, flush=True)


def write_status(state: str, **extra) -> None:
    data = {
        "state": state,
        "ts": datetime.now().isoformat(timespec="seconds"),
        "port": PORT,
        **extra,
    }
    try:
        STATUS_FILE.write_text(
            "\n".join(f"{k}={v}" for k, v in data.items()) + "\n",
            encoding="utf-8",
        )
    except Exception:
        pass


def stop_docker() -> None:
    try:
        subprocess.run(
            ["docker", "stop", "gemini-api"],
            capture_output=True,
            timeout=30,
        )
    except Exception:
        pass


def kill_stale_api_servers(exclude: int | None = None) -> None:
    """Kill orphaned api_server.py processes (not the supervisor)."""
    if os.name != "nt":
        return
    try:
        # Prefer PowerShell CIM (wmic is removed on newer Windows)
        ps = (
            "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
            "Where-Object { $_.CommandLine -and $_.CommandLine -match 'api_server\\.py' "
            "-and $_.CommandLine -notmatch 'supervisor\\.py' } | "
            "Select-Object -ExpandProperty ProcessId"
        )
        out = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command", ps],
            text=True,
            errors="ignore",
            timeout=20,
        )
        for line in out.splitlines():
            line = line.strip()
            if not line.isdigit():
                continue
            pid = int(line)
            if exclude and pid == exclude:
                continue
            log(f"Killing stale api_server PID {pid}")
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/F"],
                    capture_output=True,
                    timeout=10,
                )
            except Exception:
                pass
    except Exception as e:
        log(f"kill_stale warning: {e}")


def spawn_api() -> subprocess.Popen:
    env = os.environ.copy()
    env["PORT"] = PORT
    env["ACCESS_LOG"] = env.get("ACCESS_LOG", "0")
    env["PYTHONUNBUFFERED"] = "1"
    # Detach-ish on Windows: no pipe, new process group
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
        # DETACHED_PROCESS = 0x00000008 | CREATE_NO_WINDOW = 0x08000000
        creationflags |= 0x00000008 | 0x08000000

    # Log child stdout/stderr to rotating files via shell append to avoid pipe deadlock:
    # use DEVNULL for stability; app logs via loguru to stderr file optionally later
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "api_server.py")],
        cwd=str(ROOT),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags if os.name == "nt" else 0,
        close_fds=True,
    )
    try:
        PID_FILE.write_text(str(proc.pid), encoding="utf-8")
    except Exception:
        pass
    return proc


def main_loop() -> None:
    SUPERVISOR_PID.write_text(str(os.getpid()), encoding="utf-8")
    log(f"Supervisor started PID={os.getpid()} port={PORT}")
    write_status("starting", supervisor_pid=os.getpid())
    stop_docker()
    kill_stale_api_servers()

    restarts = 0
    while True:
        restarts += 1
        log(f"Spawning api_server (cycle={restarts})")
        try:
            proc = spawn_api()
        except Exception as e:
            log(f"Spawn failed: {e}")
            write_status("spawn_failed", error=str(e), restarts=restarts)
            time.sleep(RESTART_DELAY)
            continue

        log(f"api_server running PID={proc.pid}")
        write_status("running", api_pid=proc.pid, restarts=restarts)

        # Wait for child exit (this is the only wait — restart immediately on death)
        code = proc.wait()
        log(f"api_server exited code={code} — restarting in {RESTART_DELAY}s")
        write_status("restarting", exit_code=code, restarts=restarts)
        try:
            if PID_FILE.exists():
                PID_FILE.unlink()
        except Exception:
            pass
        time.sleep(RESTART_DELAY)
        kill_stale_api_servers(exclude=os.getpid())


def detach_and_exit() -> None:
    """Re-launch supervisor detached then exit (Windows)."""
    if os.name == "nt":
        creationflags = 0x00000008 | 0x08000000 | subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore
        subprocess.Popen(
            [sys.executable, str(ROOT / "supervisor.py")],
            cwd=str(ROOT),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
            close_fds=True,
        )
        print("Supervisor detached. Check logs/supervisor.log")
    else:
        subprocess.Popen(
            [sys.executable, str(ROOT / "supervisor.py")],
            cwd=str(ROOT),
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        print("Supervisor detached.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--background", action="store_true")
    args = parser.parse_args()
    if args.background:
        detach_and_exit()
        sys.exit(0)

    # Ignore Ctrl+C in child wait loops lightly
    def _sig(_s, _f):
        log("Supervisor signal — exiting (children may remain; keep_alive will clean)")
        sys.exit(0)

    try:
        signal.signal(signal.SIGINT, _sig)
        signal.signal(signal.SIGTERM, _sig)
    except Exception:
        pass

    main_loop()
