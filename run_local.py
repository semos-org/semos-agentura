"""Start all agents and the UI for local development.

Usage:
    uv run python run_local.py

Starts:
    - email-agent on port 8001
    - document-agent on port 8002
    - agentura-ui on port 5006 (opens browser)
      - filesystem-agent on port 8003 (in-process, shared VFS)

Press Ctrl+C to stop everything.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import threading
import time

AGENTS = [
    ("semos-agentura-email", "semos.agentura.email.service:app", 8001),
    ("semos-agentura-document", "semos.agentura.document.service:app", 8002),
    # filesystem-agent is hosted in-process by the UI
    # (shares the same VFS instance for session:// files)
]
UI_PORT = 5006

# ANSI color codes for agent log prefixes
_COLORS = [
    "\033[36m",  # cyan
    "\033[33m",  # yellow
    "\033[35m",  # magenta
    "\033[32m",  # green
    "\033[34m",  # blue
    "\033[91m",  # bright red
]
_RESET = "\033[0m"


def _log_prefix(name: str, idx: int) -> str:
    color = _COLORS[idx % len(_COLORS)]
    return f"{color}[{name}]{_RESET}"


def _pipe_output(stream, prefix: str) -> None:
    """Read lines from a subprocess stream and print with a colored prefix."""
    try:
        for line in iter(stream.readline, ""):
            if line:
                print(f"{prefix} {line}", end="", flush=True)
    except (ValueError, OSError):
        pass  # stream closed


def _start_log_threads(proc: subprocess.Popen, prefix: str) -> list[threading.Thread]:
    """Start daemon threads to pipe stdout and stderr with a prefix."""
    threads = []
    for stream in (proc.stdout, proc.stderr):
        if stream:
            t = threading.Thread(target=_pipe_output, args=(stream, prefix), daemon=True)
            t.start()
            threads.append(t)
    return threads


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def _kill_port(port: int) -> bool:
    """Kill the process listening on a port. Returns True if killed."""
    if not _port_in_use(port):
        return False
    if sys.platform == "win32":
        out = subprocess.run(
            ["netstat", "-ano"],
            capture_output=True,
            text=True,
        )
        for line in out.stdout.splitlines():
            upper = line.upper()
            if f":{port}" in line and ("LISTEN" in upper or "ABH" in upper):
                pid = line.strip().split()[-1]
                subprocess.run(
                    ["taskkill", "/F", "/PID", pid],
                    capture_output=True,
                )
                print(f"  Killed PID {pid} on port {port}")
                time.sleep(0.5)
                return True
    else:
        out = subprocess.run(
            ["lsof", "-ti", f":{port}"],
            capture_output=True,
            text=True,
        )
        if out.stdout.strip():
            for pid in out.stdout.strip().split("\n"):
                subprocess.run(["kill", "-9", pid.strip()])
            print(f"  Killed process on port {port}")
            time.sleep(0.5)
            return True
    return False


def main():
    procs: list[subprocess.Popen] = []

    # Kill existing processes on our ports
    fs_port = int(os.environ.get("FILESYSTEM_AGENT_PORT", "8003"))
    all_ports = [p for _, _, p in AGENTS] + [UI_PORT, fs_port]
    for port in all_ports:
        _kill_port(port)

    # Start agents
    for i, (name, module, port) in enumerate(AGENTS):
        if _port_in_use(port):
            print(f"  {name}: port {port} still in use, skipping")
            continue
        prefix = _log_prefix(name, i)
        print(f"  Starting {name} on port {port}...")
        agent_dir = __import__("pathlib").Path(__file__).parent / "packages" / name
        p = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                module,
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--log-level",
                "info",
            ],
            cwd=str(agent_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        _start_log_threads(p, prefix)
        procs.append(p)

    # Wait for agents to be ready
    print("  Waiting for agents...")
    for _, _, port in AGENTS:
        for _ in range(50):
            if _port_in_use(port):
                break
            time.sleep(0.1)

    # Start UI
    if _port_in_use(UI_PORT):
        print(f"  UI: port {UI_PORT} already in use, skipping")
    else:
        ui_prefix = _log_prefix("ui", len(AGENTS))
        print(f"  Starting agentura-ui on port {UI_PORT}...")
        p = subprocess.Popen(
            [sys.executable, "-m", "semos.agentura.ui"],
            cwd=str(__import__("pathlib").Path(__file__).parent),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        _start_log_threads(p, ui_prefix)
        procs.append(p)

    print()
    print(f"  UI:               http://localhost:{UI_PORT}")
    print(f"  filesystem-agent   http://localhost:{fs_port} (in-process)")
    for name, _, port in AGENTS:
        print(f"  {name:16s}   http://localhost:{port}")
    print()
    print("  Press Ctrl+C to stop all.")

    # Wait for Ctrl+C
    def _shutdown(sig, frame):
        print("\n  Shutting down...")
        for p in procs:
            p.terminate()
        for p in procs:
            p.wait(timeout=10)
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # Keep alive
    try:
        for p in procs:
            p.wait()
    except KeyboardInterrupt:
        _shutdown(None, None)


if __name__ == "__main__":
    main()
