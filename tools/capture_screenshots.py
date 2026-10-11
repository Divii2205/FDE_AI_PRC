"""Capture full-page PNG screenshots of a running Streamlit app (Windows-friendly).

Drives an already-installed Microsoft Edge (or Google Chrome) in headless mode
over the Chrome DevTools Protocol (CDP). No Playwright/Selenium, no browser
download, no compiled extensions: only the standard library plus ``requests``
and ``websockets`` (both already in the venv as Streamlit dependencies). This
matters on machines where Windows Smart App Control blocks unsigned binaries.

Usage
-----
    .venv/Scripts/python tools/capture_screenshots.py \
        --base-url http://127.0.0.1:8501 --out docs/screenshots \
        --shot "01-home|" \
        --shot "02-good-to-go|?request=REQ-1001&view=single&run=1|What should happen next"

Each ``--shot`` is ``name|query-string|wait-for-text`` (query and wait text are
optional). Alternatively (or additionally) pass ``--shots-file shots.json``
containing a JSON list such as::

    [
      {"name": "01-home", "query": ""},
      {"name": "02-good-to-go", "query": "?request=REQ-1001&run=1",
       "wait_for": "What should happen next", "width": 1400,
       "scroll_to_text": "Decision", "viewport_only": false}
    ]

For each shot the tool navigates to ``base_url + query``, waits until the
wait-for text appears in ``document.body.innerText`` (if given) AND Streamlit
has finished running (main block present, no spinner / "running" status
widget), lets the page settle, then measures the real content height (Streamlit
scrolls inside an inner container) and resizes the emulated viewport to that
height before capturing, so the PNG contains the whole page (capped by
``--max-height``). ``--viewport-only`` captures just the window.

Options: ``--width`` (default 1400), ``--height`` (viewport, default 900),
``--timeout`` per shot in seconds (default 180), ``--settle`` seconds
(default 1.5), ``--allow-errors`` to capture pages showing a Streamlit exception
(by default such shots fail), ``--no-clean`` to keep Streamlit's toolbar/header, ``--browser``
to point at a specific msedge.exe/chrome.exe. Light colour scheme is always
emulated. Output: ``<out>/<name>.png``. Exit code is 1 if any shot failed.
"""
from __future__ import annotations

import argparse
import base64
import itertools
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urljoin

import requests
from websockets.sync.client import connect as ws_connect

BROWSER_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
]

CLEAN_CSS = """
header[data-testid="stHeader"], [data-testid="stToolbar"], [data-testid="stDecoration"],
[data-testid="stStatusWidget"], [data-testid="stAppDeployButton"], .stDeployButton,
#MainMenu, footer { display: none !important; visibility: hidden !important; }
"""

# Returns true once Streamlit has rendered and is idle.
READY_JS = """
(() => {
  const main = document.querySelector('[data-testid="stMain"], section.main, [data-testid="stAppViewContainer"]');
  if (!main) return false;
  if (!document.querySelector('[data-testid="stVerticalBlock"]')) return false;
  if (document.querySelector('[data-testid="stSpinner"]')) return false;
  if (document.querySelector('[data-testid="stSkeleton"]')) return false;
  const status = document.querySelector('[data-testid="stStatusWidget"]');
  if (status && /running|connecting/i.test(status.innerText || '')) return false;
  return true;
})()
"""

# Detects a Streamlit script error (error dialog or exception block); returns its text or "".
ERROR_JS = """
(() => {
  const el = document.querySelector('[data-testid="stException"]') ||
             [...document.querySelectorAll('[role="dialog"]')].find(d => /Script execution error|Traceback|Error/.test(d.innerText));
  return el ? el.innerText.slice(0, 300) : '';
})()
"""

# Full content height, accounting for Streamlit's inner scroll container.
HEIGHT_JS = """
(() => {
  const sels = ['[data-testid="stMain"]', '[data-testid="stAppViewContainer"]',
                'section.main', '[data-testid="stMainBlockContainer"]', '.block-container'];
  let h = Math.max(document.documentElement.scrollHeight, document.body.scrollHeight);
  for (const s of sels) {
    for (const el of document.querySelectorAll(s)) {
      h = Math.max(h, el.scrollHeight, el.getBoundingClientRect().top + el.scrollHeight);
    }
  }
  return Math.ceil(h);
})()
"""

SCROLL_TO_TEXT_JS = """
((needle) => {
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let n;
  while ((n = walker.nextNode())) {
    if (n.textContent.includes(needle)) {
      const el = n.parentElement;
      el.scrollIntoView({block: 'start'});
      return Math.max(0, Math.floor(el.getBoundingClientRect().top + window.scrollY) - 16);
    }
  }
  return -1;
})
"""


@dataclass
class Shot:
    name: str
    query: str = ""
    wait_for: str = ""
    width: Optional[int] = None
    height: Optional[int] = None
    scroll_to_text: str = ""
    viewport_only: Optional[bool] = None


class CDPError(RuntimeError):
    pass


class CDP:
    """Minimal synchronous CDP client over a page websocket."""

    def __init__(self, ws_url: str):
        kwargs = dict(max_size=None, open_timeout=30, compression=None, ping_interval=None)
        try:  # websockets >= 15: bypass system proxy, opt out of context-manager warning
            self.ws = ws_connect(ws_url, proxy=None, legacy=True, **kwargs)
        except TypeError:  # older websockets
            self.ws = ws_connect(ws_url, **kwargs)
        self._ids = itertools.count(1)

    def send(self, method: str, params: Optional[dict] = None, timeout: float = 60) -> dict:
        msg_id = next(self._ids)
        self.ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CDPError(f"timeout waiting for {method}")
            data = json.loads(self.ws.recv(timeout=remaining))
            if data.get("id") == msg_id:
                if "error" in data:
                    raise CDPError(f"{method}: {data['error']}")
                return data.get("result", {})
            # events are ignored

    def eval(self, expr: str) -> Any:
        res = self.send("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True})
        if "exceptionDetails" in res:
            raise CDPError(f"JS error: {res['exceptionDetails'].get('text')}")
        return res.get("result", {}).get("value")

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:
            pass


def find_browser(explicit: Optional[str]) -> str:
    for cand in ([explicit] if explicit else []) + BROWSER_CANDIDATES:
        if cand and Path(cand).is_file():
            return cand
    for name in ("msedge", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    raise SystemExit("No Edge/Chrome found; pass --browser PATH")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http() -> requests.Session:
    s = requests.Session()
    s.trust_env = False  # system proxies can break localhost
    return s


def launch_browser(exe: str, port: int, profile: str, width: int, height: int) -> subprocess.Popen:
    args = [
        exe, "--headless=new", f"--remote-debugging-port={port}", "--remote-allow-origins=*",
        f"--user-data-dir={profile}", f"--window-size={width},{height}",
        "--hide-scrollbars", "--no-first-run", "--no-default-browser-check", "--disable-gpu",
        "--disable-extensions", "--disable-background-networking", "--disable-sync",
        "--force-color-profile=srgb", "--blink-settings=preferredColorScheme=1", "about:blank",
    ]
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    return subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)


def page_ws_url(port: int, timeout: float = 30) -> str:
    sess = http()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            for target in sess.get(f"http://127.0.0.1:{port}/json", timeout=2).json():
                if target.get("type") == "page" and target.get("webSocketDebuggerUrl"):
                    return target["webSocketDebuggerUrl"]
        except (requests.RequestException, ValueError):
            pass
        time.sleep(0.3)
    raise SystemExit(f"Browser DevTools endpoint on port {port} did not come up")


def kill_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            proc.kill()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def set_viewport(cdp: CDP, width: int, height: int) -> None:
    cdp.send("Emulation.setDeviceMetricsOverride",
             {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": False})


def wait_ready(cdp: CDP, wait_for: str, timeout: float, settle: float, allow_errors: bool = False) -> None:
    deadline = time.monotonic() + timeout
    needle = json.dumps(wait_for)
    stable = 0
    while time.monotonic() < deadline:
        if not allow_errors:
            err = cdp.eval(ERROR_JS)
            if err:
                raise CDPError("Streamlit error on page: " + " ".join(err.split()))
        ok = cdp.eval(READY_JS)
        if ok and wait_for:
            ok = cdp.eval(f"document.body.innerText.includes({needle})")
        stable = stable + 1 if ok else 0
        if stable >= 3:  # idle for three consecutive polls (~1.5 s)
            time.sleep(settle)
            if cdp.eval(READY_JS) and (allow_errors or not cdp.eval(ERROR_JS)):
                return
            stable = 0
        time.sleep(0.5)
    raise CDPError(f"timed out after {timeout:.0f}s" + (f" waiting for text {wait_for!r}" if wait_for else ""))


def capture(cdp: CDP, base_url: str, shot: Shot, out_dir: Path, opts: argparse.Namespace) -> Path:
    width = shot.width or opts.width
    height = shot.height or opts.height
    viewport_only = opts.viewport_only if shot.viewport_only is None else shot.viewport_only
    set_viewport(cdp, width, height)
    url = urljoin(base_url.rstrip("/") + "/", shot.query.lstrip("/")) if not shot.query.startswith("?") \
        else base_url.rstrip("/") + "/" + shot.query
    cdp.send("Page.navigate", {"url": url})
    time.sleep(1.0)  # let the old document unload before polling
    wait_ready(cdp, shot.wait_for, opts.timeout, opts.settle, opts.allow_errors)

    clip = None
    if shot.scroll_to_text:
        top = cdp.eval(f"({SCROLL_TO_TEXT_JS})({json.dumps(shot.scroll_to_text)})")
        if top is None or top < 0:
            raise CDPError(f"scroll_to_text {shot.scroll_to_text!r} not found")
    if not viewport_only:
        full = min(max(cdp.eval(HEIGHT_JS) or height, height), opts.max_height)
        set_viewport(cdp, width, full)
        time.sleep(opts.settle)
        # Content may reflow at the new size; measure once more.
        full2 = min(max(cdp.eval(HEIGHT_JS) or full, height), opts.max_height)
        if full2 != full:
            set_viewport(cdp, width, full2)
            time.sleep(opts.settle)
        full = full2
        if shot.scroll_to_text:
            top = cdp.eval(f"({SCROLL_TO_TEXT_JS})({json.dumps(shot.scroll_to_text)})")
            clip = {"x": 0, "y": 0, "width": width, "height": max(1, min(height, full)), "scale": 1}
            # After resizing everything is visible, so crop from the element down.
            if top and top > 0:
                clip["y"] = top
                clip["height"] = max(1, min(height, full - top))
    params: dict = {"format": "png", "captureBeyondViewport": False}
    if clip:
        params["clip"] = clip
    data = cdp.send("Page.captureScreenshot", params, timeout=120)["data"]
    out = out_dir / f"{shot.name}.png"
    out.write_bytes(base64.b64decode(data))
    return out


def parse_shot(spec: str) -> Shot:
    parts = spec.split("|")
    if not parts[0].strip():
        raise argparse.ArgumentTypeError(f"shot needs a name: {spec!r}")
    parts += [""] * (3 - len(parts))
    return Shot(name=parts[0].strip(), query=parts[1].strip(), wait_for="|".join(parts[2:]).strip())


def load_shots_file(path: str) -> list[Shot]:
    items = json.loads(Path(path).read_text(encoding="utf-8"))
    fields = Shot.__dataclass_fields__
    return [Shot(**{k: v for k, v in item.items() if k in fields}) for item in items]


def png_size(path: Path) -> tuple[int, int]:
    head = path.read_bytes()[:24]
    return int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base-url", default="http://127.0.0.1:8501")
    p.add_argument("--out", default="docs/screenshots")
    p.add_argument("--shot", action="append", type=parse_shot, default=[],
                   help='"name|query|wait-for-text" (repeatable)')
    p.add_argument("--shots-file", help="JSON list of shot objects")
    p.add_argument("--width", type=int, default=1400)
    p.add_argument("--height", type=int, default=900, help="viewport height")
    p.add_argument("--max-height", type=int, default=6000, help="cap for full-page height")
    p.add_argument("--timeout", type=float, default=180, help="per-shot timeout, seconds")
    p.add_argument("--settle", type=float, default=1.5, help="settle delay, seconds")
    p.add_argument("--viewport-only", action="store_true")
    p.add_argument("--no-clean", dest="clean", action="store_false",
                   help="keep Streamlit toolbar/header/deploy button")
    p.add_argument("--allow-errors", action="store_true",
                   help="capture even if the page shows a Streamlit exception/error dialog")
    p.add_argument("--browser", help="path to msedge.exe / chrome.exe")
    opts = p.parse_args(argv)

    shots = list(opts.shot)
    if opts.shots_file:
        shots += load_shots_file(opts.shots_file)
    if not shots:
        p.error("no shots given (use --shot and/or --shots-file)")

    try:
        http().get(opts.base_url, timeout=10).raise_for_status()
    except requests.RequestException as exc:
        print(f"App not reachable at {opts.base_url}: {exc}", file=sys.stderr)
        return 2

    out_dir = Path(opts.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    exe = find_browser(opts.browser)
    port = free_port()
    profile = tempfile.mkdtemp(prefix="cdp-shots-")
    proc = launch_browser(exe, port, profile, opts.width, opts.height)
    cdp = None
    failures = 0
    print(f"Browser: {exe} (DevTools port {port})")
    try:
        cdp = CDP(page_ws_url(port))
        cdp.send("Page.enable")
        cdp.send("Runtime.enable")
        cdp.send("Emulation.setEmulatedMedia",
                 {"features": [{"name": "prefers-color-scheme", "value": "light"}]})
        if opts.clean:
            cdp.send("Page.addScriptToEvaluateOnNewDocument", {"source": (
                "document.addEventListener('DOMContentLoaded', () => {"
                "const s = document.createElement('style');"
                f"s.textContent = {json.dumps(CLEAN_CSS)};"
                "document.head.appendChild(s); });")})
        for shot in shots:
            t0 = time.monotonic()
            try:
                path = capture(cdp, opts.base_url, shot, out_dir, opts)
                w, h = png_size(path)
                print(f"  OK   {shot.name:<28} {w}x{h}px  {path.stat().st_size / 1024:7.0f} KB  "
                      f"{time.monotonic() - t0:5.1f}s  -> {path}")
            except Exception as exc:  # keep going with the remaining shots
                failures += 1
                print(f"  FAIL {shot.name:<28} {time.monotonic() - t0:5.1f}s  {exc}", file=sys.stderr)
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        failures += 1
    finally:
        if cdp:
            cdp.close()
        kill_tree(proc)
        shutil.rmtree(profile, ignore_errors=True)
    print(f"{len(shots) - failures}/{len(shots)} shots captured.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
