"""Start all agents and the UI for local development.

Usage:
    uv run python run_local.py

Starts:
    - email-agent on port 8001
    - document-agent on port 8002
    - agentura-ui on port 5006 (opens browser)

Press Ctrl+C to stop everything.
"""

from __future__ import annotations

import signal
import socket
import subprocess
import sys
import time

AGENTS = [
    ("email-agent", "email_agent.service:app", 8001),
    ("document-agent", "document_agent.service:app", 8002),
]
UI_PORT = 5006


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def _kill_port(port: int) -> bool:
    """Kill the process listening on a port. Returns True if killed."""
    if not _port_in_use(port):
        return False
    if sys.platform == "win32":
        # netstat -ano | findstr :PORT
        out = subprocess.run(
            ["netstat", "-ano"],
            capture_output=True, text=True,
        )
        for line in out.stdout.splitlines():
            # "LISTENING" (en) or "ABHREN" (de)
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
        # lsof -ti :PORT | xargs kill
        out = subprocess.run(
            ["lsof", "-ti", f":{port}"],
            capture_output=True, text=True,
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
    all_ports = [p for _, _, p in AGENTS] + [UI_PORT]
    for port in all_ports:
        _kill_port(port)

    # Start agents
    for name, module, port in AGENTS:
        if _port_in_use(port):
            print(f"  {name}: port {port} still in use, skipping")
            continue
        print(f"  Starting {name} on port {port}...")
        agent_dir = __import__("pathlib").Path(__file__).parent / name
        p = subprocess.Popen(
            [
                sys.executable, "-m", "uvicorn",
                module,
                "--host", "127.0.0.1",
                "--port", str(port),
                "--log-level", "info",
            ],
            cwd=str(agent_dir),
        )
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
        print(f"  Starting agentura-ui on port {UI_PORT}...")
        p = subprocess.Popen(
            [sys.executable, "-m", "agentura_ui"],
            cwd=str(__import__("pathlib").Path(__file__).parent),
        )
        procs.append(p)

    print()
    print(f"  UI:             http://localhost:{UI_PORT}")
    for name, _, port in AGENTS:
        print(f"  {name:16s} http://localhost:{port}")
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
