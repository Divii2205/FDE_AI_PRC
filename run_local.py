from __future__ import annotations

import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env", override=False)


API_HEALTH_URL = "http://127.0.0.1:8001/health"

# Local health checks must never go through a system/corporate proxy.
_local = requests.Session()
_local.trust_env = False


def start(cmd: list[str]) -> subprocess.Popen:
    return subprocess.Popen(cmd, cwd=ROOT)


def api_is_up(url: str = API_HEALTH_URL) -> bool:
    try:
        return _local.get(url, timeout=1).ok
    except requests.RequestException:
        return False


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def wait_for_api(url: str, proc: subprocess.Popen, timeout_seconds: float = 60.0) -> None:
    """Wait until the mock API is reachable or fail with a useful message.

    The first start on Windows can take well over 10 s while antivirus scans the new
    virtual environment, so the budget is generous and progress is reported.
    """
    start_time = time.monotonic()
    next_notice = start_time + 5
    while time.monotonic() - start_time < timeout_seconds:
        if proc.poll() is not None:
            raise RuntimeError(
                f"Vendor-risk API exited during startup with code {proc.returncode}. "
                "Check the terminal output above (a port conflict is a common cause)."
            )
        if api_is_up(url):
            return
        if time.monotonic() >= next_notice:
            print(f"  ... still starting ({time.monotonic() - start_time:.0f}s; first run can be slow on Windows)")
            next_notice += 5
        time.sleep(0.25)
    raise RuntimeError(f"Vendor-risk API did not become ready within {timeout_seconds:.0f}s: {url}")


def _handle_termination(signum: int, frame: object) -> None:
    """Route SIGTERM through normal cleanup (useful for IDE/terminal stop actions)."""
    raise KeyboardInterrupt


def main() -> None:
    signal.signal(signal.SIGTERM, _handle_termination)
    procs: list[subprocess.Popen] = []
    try:
        if api_is_up():
            print("Vendor-risk API is already running on http://127.0.0.1:8001 - reusing it.")
        elif port_in_use(8001):
            raise RuntimeError(
                "Port 8001 is in use by another program (not the vendor-risk API). Stop it, e.g. in PowerShell:\n"
                "  Get-NetTCPConnection -LocalPort 8001 | Select-Object -Expand OwningProcess | "
                "ForEach-Object { Stop-Process -Id $_ -Force }"
            )
        else:
            print("Starting vendor-risk API on http://127.0.0.1:8001 ...")
            api_proc = start(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "mock_api.app:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "8001",
                ]
            )
            procs.append(api_proc)
            wait_for_api(API_HEALTH_URL, api_proc)
            print("Vendor-risk API is ready.")
        if port_in_use(8501):
            raise RuntimeError("Port 8501 is already in use (an earlier UI may still be running). "
                               "Close it or stop the process using port 8501, then retry.")

        try:
            __import__("streamlit")
        except ImportError:
            print("Streamlit is not installed. Run: pip install -r requirements.txt")
            print("The mock API is still running. Press Ctrl+C to stop.")
        else:
            print("Starting UI on http://127.0.0.1:8501  (open this URL in your browser)")
            procs.append(
                start(
                    [
                        sys.executable,
                        "-m",
                        "streamlit",
                        "run",
                        "app.py",
                        "--server.port",
                        "8501",
                        # headless skips Streamlit's first-run email prompt, which blocks/crashes
                        # the UI process when the launcher runs non-interactively
                        "--server.headless",
                        "true",
                        "--browser.gatherUsageStats",
                        "false",
                    ]
                )
            )

        while True:
            time.sleep(1)
            for proc in procs:
                if proc.poll() is not None:
                    raise RuntimeError(f"A local process exited with code {proc.returncode}")
    except KeyboardInterrupt:
        print("\nStopping local services ...")
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.terminate()
        for proc in procs:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    main()
