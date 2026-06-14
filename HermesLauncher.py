#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Hermes Launcher
一键启动 Hermes Agent & WebUI
Win11 风格现代化桌面应用 — 免安装，文件保存在主程序目录下
"""

import os
import sys
import json
import threading
import subprocess
import queue
import time
import webbrowser
import zipfile
import io
import re
import shutil
import signal
import tempfile
import ssl
from datetime import datetime
from pathlib import Path
from typing import Optional

import customtkinter as ctk
from urllib.request import urlopen, Request
from urllib.error import URLError

# ─── Constants ───────────────────────────────────────────────────────────────

APP_NAME = "Hermes Launcher"
APP_VERSION = "1.0.2"

# Detect PyInstaller frozen mode (sys.executable is the launcher EXE, not Python)
FROZEN = getattr(sys, 'frozen', False)

# In PyInstaller --onefile mode, resolve BASE_DIR from the actual exe location
if FROZEN:
    BASE_DIR = Path(os.path.realpath(sys.argv[0])).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent


def _resolve_webui_dir() -> Path:
    """Return the hermes-webui directory, preferring external alongside exe.

    In PyInstaller --add-data builds, the bundled copy lives under sys._MEIPASS.
    The external directory alongside the EXE takes precedence so that runtime
    downloads and updates continue to work.
    """
    base = Path(os.path.realpath(sys.argv[0])).resolve().parent
    external = base / "hermes-webui"
    if (external / "bootstrap.py").exists():
        return external

    # Fallback: PyInstaller bundled resources via --add-data
    meipass = getattr(sys, '_MEIPASS', None)
    if meipass:
        bundled = Path(meipass) / "hermes-webui"
        if (bundled / "bootstrap.py").exists():
            return bundled

    # Return the default (may not exist yet — user can download later)
    return external


CONFIG_PATH = BASE_DIR / "config.json"
WEBUI_DIR = _resolve_webui_dir()
WEBUI_BOOTSTRAP = WEBUI_DIR / "bootstrap.py"
GITHUB_API = "https://api.github.com/repos/nesquena/hermes-webui/releases/latest"

# Default config
DEFAULT_CONFIG = {
    "theme": "system",           # "system" | "dark" | "light"
    "webui_port": 8787,
    "webui_host": "127.0.0.1",
    "auto_open_browser": True,
    "auto_download_webui": True,
    "python_path": "",           # empty = auto-detect
    "hermes_path": "",           # empty = auto-detect
    "webui_version": "",         # saved after download
}


# ─── Config Manager ───────────────────────────────────────────────────────────

def load_config() -> dict:
    cfg = DEFAULT_CONFIG.copy()
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            cfg.update(data)
        except (json.JSONDecodeError, OSError):
            pass
    return cfg


def save_config(cfg: dict):
    CONFIG_PATH.write_text(
        json.dumps(cfg, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


# ─── Utility Functions ───────────────────────────────────────────────────────

def log_time() -> str:
    return datetime.now().strftime("%H:%M:%S")


def find_python() -> Optional[str]:
    """Auto-detect a Python interpreter that can run the WebUI.

    In PyInstaller frozen mode, NEVER returns sys.executable (which is the
    launcher EXE itself).  Returns None when no valid Python is found.
    """
    import glob as _glob

    cfg = load_config()

    # ── helper: is this executable a real Python interpreter? ──────────────
    def _is_real_python(exe_path: str) -> bool:
        """Run exe --version and check that the output contains 'Python'."""
        try:
            result = subprocess.run(
                [exe_path, "--version"],
                capture_output=True, text=True, timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            out = (result.stdout + result.stderr)
            return result.returncode == 0 and "Python" in out
        except (OSError, subprocess.TimeoutExpired):
            return False

    # ── helper: can this Python import customtkinter? ─────────────────────
    def _can_import_ctk(exe_path: str) -> bool:
        """Only used for the config override — user-set paths get strict validation."""
        try:
            result = subprocess.run(
                [exe_path, "-c", "import customtkinter"],
                capture_output=True, text=True, timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            return result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    # 1) Check config override — strict validation (needs customtkinter)
    if cfg.get("python_path"):
        exe = cfg["python_path"]
        if Path(exe).exists() and _can_import_ctk(exe):
            return exe

    # 2) Check PATH
    candidates = []
    for name in ["python3", "python"]:
        exe = shutil.which(name)
        if exe and exe not in candidates:
            candidates.append(exe)

    # 3) Glob-based discovery — any Python3* install, newest first
    home = os.path.expanduser("~")
    search_roots = [
        os.path.join(home, "AppData", "Local", "Programs", "Python"),
        os.path.join(os.environ.get("ProgramFiles", "C:\\Program Files")),
        os.path.join(os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)")),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Python"),
        "C:\\Python",
        "C:\\Python3",
        # conda / miniconda / miniforge roots
        os.path.join(home, "miniconda3"),
        os.path.join(home, "miniforge3"),
        os.path.join(home, "anaconda3"),
        os.path.join(home, "AppData", "Local", "miniconda3"),
        os.path.join(home, "AppData", "Local", "miniforge3"),
        os.path.join(home, "AppData", "Local", "anaconda3"),
        os.path.join(home, "AppData", "Roaming", "miniconda3"),
        os.path.join(home, "AppData", "Roaming", "miniforge3"),
    ]

    seen = {os.path.normcase(c) for c in candidates}
    for root in search_roots:
        if not root or not os.path.isdir(root):
            continue
        try:
            # Standard install: <root>/Python3*/python.exe  (newest first)
            patterns = [
                os.path.join(root, "Python3*", "python.exe"),
                os.path.join(root, "python.exe"),  # conda puts python.exe in root
            ]
            for pat in patterns:
                for exe_path in sorted(_glob.glob(pat), reverse=True):
                    norm = os.path.normcase(exe_path)
                    if norm not in seen:
                        seen.add(norm)
                        candidates.append(exe_path)
        except Exception:
            continue

    # 4) Validate each candidate — just check it's a real Python interpreter.
    #    The WebUI manages its own dependencies via venv, so customtkinter
    #    is NOT required (it's already bundled in frozen builds).
    for exe in candidates:
        if _is_real_python(exe):
            return exe

    # 5) Last resort — NEVER use sys.executable when frozen (it's the EXE itself).
    #    In source mode, sys.executable is the real Python, so it's a safe fallback.
    if not FROZEN and _is_real_python(sys.executable):
        return sys.executable

    return None


def find_hermes() -> Optional[str]:
    """Auto-detect hermes CLI. Checks config, PATH, then known install locations."""
    cfg = load_config()
    if cfg.get("hermes_path") and Path(cfg["hermes_path"]).exists():
        return cfg["hermes_path"]

    # 1) Check PATH — prefer .exe on Windows to avoid .cmd/.bat wrappers
    exe = shutil.which("hermes.exe")
    if not exe:
        exe = shutil.which("hermes")
    if exe:
        return exe

    # 2) Check common Windows-specific locations
    home = str(Path.home())
    localappdata = os.environ.get("LOCALAPPDATA", "")
    candidates = []

    # Build a comprehensive list of possible Hermes install locations
    base_paths = [
        home,
        os.path.join(home, ".hermes"),
        os.path.join(home, "AppData", "Local", "hermes"),
        localappdata,
        os.path.join(localappdata, "hermes"),
    ]

    # Filter out empty/duplicate paths
    seen = set()
    unique_bases = []
    for p in base_paths:
        if p and p not in seen:
            seen.add(p)
            unique_bases.append(p)

    for base in unique_bases:
        for agent_dir in ["hermes-agent", ""]:
            for venv in ["venv", ".venv"]:
                for script in ["hermes.exe", "hermes.cmd", "hermes.bat", "hermes"]:
                    candidate = Path(base) / agent_dir / venv / "Scripts" / script
                    if candidate.exists():
                        return str(candidate.resolve())

    # 3) Also check straight Scripts paths (hermes may be installed directly)
    for base in unique_bases:
        for script in ["hermes.exe", "hermes.cmd", "hermes"]:
            candidate = Path(base) / "Scripts" / script
            if candidate.exists():
                return str(candidate.resolve())

    # 4) Legacy: ~/.local/bin
    for script in ["hermes.exe", "hermes"]:
        candidate = Path(home) / ".local" / "bin" / script
        if candidate.exists():
            return str(candidate.resolve())

    return None


def _run_cmd_capture(cmd, *, timeout=15) -> str:
    """Run a command and capture its output using multiple strategies.

    On Windows, GUI-subsystem executables and certain conda/pip shims don't
    produce stdout when spawned via ``subprocess.run(capture_output=True)``
    because they lack an attached console.  We try several methods in order:
    1. ``subprocess.run`` with ``capture_output=True, text=True``
    2. ``subprocess.Popen`` + ``.communicate()``  (bypasses some console-attach quirks)
    3. ``subprocess.run`` with ``shell=True`` (uses ``cmd.exe`` on Windows)
    4. ``subprocess.check_output`` (different internal CreateProcess flags)
    5. ``subprocess.Popen`` + raw bytes, decoded manually
    """
    # Helper: build command string for shell=True on Windows
    def _to_cmd_str(c):
        if isinstance(c, str):
            return c
        return " ".join(f'"{x}"' if " " in x else x for x in c)

    # Method 1: subprocess.run with capture_output
    try:
        if isinstance(cmd, list):
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, shell=True,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode == 0:
            out = (result.stdout or "").strip() or (result.stderr or "").strip()
            if out:
                return out
    except Exception:
        pass

    # Method 2: Popen + communicate (different internal pipe setup)
    try:
        args = cmd if isinstance(cmd, list) else _to_cmd_str(cmd)
        proc = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            shell=isinstance(cmd, str),
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        stdout, stderr = proc.communicate(timeout=timeout)
        if proc.returncode == 0:
            out = (stdout or "").strip() or (stderr or "").strip()
            if out:
                return out
    except Exception:
        pass

    # Method 3: shell=True via subprocess.run (cmd.exe mediation)
    try:
        cmd_str = _to_cmd_str(cmd)
        result = subprocess.run(
            cmd_str, capture_output=True, text=True, timeout=timeout, shell=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if result.returncode == 0:
            out = (result.stdout or "").strip() or (result.stderr or "").strip()
            if out:
                return out
    except Exception:
        pass

    # Method 4: check_output (uses different CreateProcess flags)
    if isinstance(cmd, list):
        try:
            out = subprocess.check_output(
                cmd, stderr=subprocess.STDOUT, timeout=timeout, text=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            if out and out.strip():
                return out.strip()
        except Exception:
            pass

    # Method 5: raw bytes via Popen, decode manually
    try:
        args = cmd if isinstance(cmd, list) else _to_cmd_str(cmd)
        proc = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=isinstance(cmd, str),
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        stdout_bytes, stderr_bytes = proc.communicate(timeout=timeout)
        if proc.returncode == 0:
            out = ""
            if stdout_bytes:
                out = stdout_bytes.decode("utf-8", errors="replace").strip()
            if not out and stderr_bytes:
                out = stderr_bytes.decode("utf-8", errors="replace").strip()
            if out:
                return out
    except Exception:
        pass

    return ""


def check_hermes_installed() -> tuple[bool, str]:
    """Check if hermes CLI is available. Returns (ok, detail)."""
    hermes = find_hermes()
    if not hermes:
        return False, "未找到 hermes 命令，请先安装 Hermes Agent"
    try:
        # Try with full path
        output = _run_cmd_capture([hermes, "--version"])
        if output:
            return True, output

        # Try with just "hermes --version" via shell (PATH resolution)
        output = _run_cmd_capture(f'"{hermes}" --version')
        if output:
            return True, output

        # Try without quotes
        output = _run_cmd_capture(f'{hermes} --version')
        if output:
            return True, output

        # Executable found but produced no output
        return True, "Hermes Agent 已就绪"
    except Exception as e:
        return False, str(e)


def extract_agent_version(detail: str) -> str:
    """Extract version string like 'v0.16.0' from hermes --version output."""
    if not detail:
        return ""
    # Try "Hermes Agent vX.Y.Z" or "Hermes Agent X.Y.Z" (case-insensitive)
    m = re.search(r'(?i)Hermes\s+Agent[,\s]*v?(\d+\.\d+\.\d+)', detail)
    if m:
        return "v" + m.group(1) if not m.group(1).startswith("v") else m.group(1)
    # Try "hermes version vX.Y.Z" or similar
    m = re.search(r'(?i)hermes\s+version\s+(v?\d+\.\d+\.\d+)', detail)
    if m:
        return m.group(1)
    # Fallback: try to find any version-like pattern (prefer v-prefixed)
    m = re.search(r'(v\d+\.\d+\.\d+)', detail)
    if m:
        return m.group(1)
    m = re.search(r'(\d+\.\d+\.\d+)', detail)
    if m:
        return "v" + m.group(1)
    return ""


def get_webui_version() -> str:
    """Get the installed WebUI version tag from CHANGELOG.md, or saved config as fallback."""
    # Primary: parse latest release version from CHANGELOG.md
    changelog = WEBUI_DIR / "CHANGELOG.md"
    if changelog.exists():
        try:
            content = changelog.read_text(encoding="utf-8", errors="replace")
            # Look for first "## [vX.Y.Z]" pattern (skip "## [Unreleased]")
            m = re.search(r'^##\s*\[(v?\d+\.\d+\.\d+)\]', content, re.MULTILINE)
            if m:
                return m.group(1)
        except Exception:
            pass

    # Fallback: use saved config value
    cfg = load_config()
    saved = cfg.get("webui_version", "")
    if saved:
        return saved

    # Last resort: try to detect from installed files
    if WEBUI_DIR.exists():
        for pattern in ["version.txt", "VERSION", "package.json", "pyproject.toml"]:
            for f in WEBUI_DIR.rglob(pattern):
                try:
                    content = f.read_text(encoding="utf-8", errors="replace")
                    m = re.search(r'(v?\d+\.\d+\.\d+(?:\.\d+)?)', content)
                    if m:
                        return m.group(1)
                except Exception:
                    continue
    return ""


def download_with_progress(url: str, dest: Path, callback=None):
    """Download a file with optional progress callback (bytes_downloaded, total)."""
    req = Request(url, headers={
        "User-Agent": f"{APP_NAME}/{APP_VERSION}",
        "Accept": "application/octet-stream",
    })
    # Create SSL context that doesn't verify (some corporate/VPN setups)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    resp = urlopen(req, timeout=30, context=ctx)
    total = int(resp.headers.get("Content-Length", 0))
    downloaded = 0
    chunk_size = 64 * 1024

    with open(dest, "wb") as f:
        while True:
            chunk = resp.read(chunk_size)
            if not chunk:
                break
            f.write(chunk)
            downloaded += len(chunk)
            if callback:
                callback(downloaded, total)


def get_latest_webui_release() -> Optional[dict]:
    """Fetch latest release info from GitHub API. Returns dict with tag, zipball_url.

    Falls back to master branch archive when API rate-limited.
    """
    req = Request(GITHUB_API, headers={
        "User-Agent": f"{APP_NAME}/{APP_VERSION}",
        "Accept": "application/vnd.github.v3+json",
    })
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        resp = urlopen(req, timeout=15, context=ctx)
        data = json.loads(resp.read().decode("utf-8"))
        return {
            "tag": data.get("tag_name", "latest"),
            "zipball_url": data.get("zipball_url"),
            "html_url": data.get("html_url", ""),
        }
    except URLError as e:
        # Rate limited or network issue — fall back to master branch archive
        if hasattr(e, "code") and e.code == 403:
            return {
                "tag": "master (latest)",
                "zipball_url": "https://github.com/nesquena/hermes-webui/archive/master.zip",
                "html_url": "https://github.com/nesquena/hermes-webui",
            }
        return None
    except Exception:
        # Last resort: try master branch
        return {
            "tag": "master (latest)",
            "zipball_url": "https://github.com/nesquena/hermes-webui/archive/master.zip",
            "html_url": "https://github.com/nesquena/hermes-webui",
        }


def extract_zip_with_top_dir(zip_path: Path, extract_to: Path) -> Path:
    """Extract a GitHub archive zip, stripping the top-level directory."""
    # GitHub archives have a top dir like "nesquena-hermes-webui-<sha>"
    # We need to extract contents into extract_to
    if extract_to.exists():
        shutil.rmtree(extract_to)

    with zipfile.ZipFile(zip_path, "r") as zf:
        # Find the top-level dir from the first entry
        top_dir = None
        entries = []
        for name in zf.namelist():
            # Normalize path separators
            parts = name.replace("\\", "/").split("/")
            if len(parts) > 1 and parts[0]:
                top_dir = parts[0]
                break
        if top_dir:
            # Extract all files, stripping the top dir
            extract_to.mkdir(parents=True, exist_ok=True)
            for name in zf.namelist():
                # Skip directory entries
                if name.endswith("/") or name.endswith("\\"):
                    continue
                # Strip top-level directory
                parts = name.replace("\\", "/").split("/", 1)
                if len(parts) < 2:
                    continue
                rel_path = parts[1]
                target = extract_to / rel_path
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(name) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
        else:
            # Flat zip, extract directly
            zf.extractall(extract_to)

    return extract_to


# ─── Service Manager ─────────────────────────────────────────────────────────

class ServiceState:
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    ERROR = "error"


class ServiceManager:
    """Manages subprocess lifecycle for Hermes Agent and WebUI."""

    def __init__(self, log_callback):
        self.log_callback = log_callback
        self._processes: dict[str, Optional[subprocess.Popen]] = {
            "agent": None,
            "webui": None,
        }
        self._threads: dict[str, Optional[threading.Thread]] = {
            "agent": None,
            "webui": None,
        }
        self._state: dict[str, str] = {
            "agent": ServiceState.STOPPED,
            "webui": ServiceState.STOPPED,
        }
        self._stop_events: dict[str, threading.Event] = {
            "agent": threading.Event(),
            "webui": threading.Event(),
        }
        self._lock = threading.Lock()
        self._webui_url: Optional[str] = None

    def get_state(self, name: str) -> str:
        return self._state.get(name, ServiceState.STOPPED)

    def set_state(self, name: str, state: str):
        with self._lock:
            self._state[name] = state
        self.log_callback("system", f"状态变更 [{name}]: {state}")

    def get_webui_url(self) -> Optional[str]:
        return self._webui_url

    def log(self, source: str, msg: str):
        self.log_callback(source, msg)

    # ── Agent ──

    def start_agent(self) -> bool:
        """Start Hermes Agent CLI and verify it's working."""
        if self.get_state("agent") in (ServiceState.STARTING, ServiceState.RUNNING):
            self.log("agent", "Hermes Agent 已在运行中")
            return True

        hermes = find_hermes()
        if not hermes:
            self.log("agent", "✗ 未找到 Hermes Agent，请先安装")
            self.set_state("agent", ServiceState.ERROR)
            return False

        self.set_state("agent", ServiceState.STARTING)
        self._stop_events["agent"].clear()

        def _run():
            try:
                # Run hermes --version to verify CLI works
                self.log("agent", f"正在验证 Hermes CLI: {hermes}")
                self.log("agent", f"   > {hermes} --version")

                result = subprocess.run(
                    [hermes, "--version"],
                    capture_output=True, text=True, timeout=30,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                if result.returncode == 0:
                    version = (result.stdout or result.stderr).strip()
                    self.log("agent", f"✓ Hermes Agent {version}")
                    self.set_state("agent", ServiceState.RUNNING)
                else:
                    err = (result.stderr or result.stdout).strip()
                    self.log("agent", f"✗ hermes 返回错误 (code {result.returncode}): {err}")
                    self.set_state("agent", ServiceState.ERROR)
            except FileNotFoundError:
                self.log("agent", f"✗ 找不到命令: {hermes}")
                self.set_state("agent", ServiceState.ERROR)
            except subprocess.TimeoutExpired:
                self.log("agent", "✗ hermes 命令执行超时")
                self.set_state("agent", ServiceState.ERROR)
            except Exception as e:
                self.log("agent", f"✗ 启动失败: {e}")
                self.set_state("agent", ServiceState.ERROR)

        thread = threading.Thread(target=_run, daemon=True)
        self._threads["agent"] = thread
        thread.start()
        return True

    def stop_agent(self):
        if self._processes.get("agent"):
            try:
                self._processes["agent"].terminate()
                self._processes["agent"].wait(timeout=5)
            except Exception:
                pass
            self._processes["agent"] = None
        self._stop_events["agent"].set()
        self.set_state("agent", ServiceState.STOPPED)
        self.log("agent", "■ Hermes Agent 已停止")

    # ── WebUI ──

    def start_webui(self) -> bool:
        """Start Hermes WebUI via bootstrap.py."""
        if self.get_state("webui") in (ServiceState.STARTING, ServiceState.RUNNING):
            self.log("webui", "WebUI 已在运行中")
            return True

        if not WEBUI_BOOTSTRAP.exists():
            self.log("webui", f"✗ 未找到 {WEBUI_BOOTSTRAP}")
            self.log("webui", "请点击「下载 WebUI」或手动将 hermes-webui 放置在主目录下")
            self.set_state("webui", ServiceState.ERROR)
            return False

        cfg = load_config()
        python_exe = find_python()

        # Guard: if no valid Python interpreter was found, show a clear error
        # instead of silently spawning a duplicate launcher window.
        if python_exe is None:
            self.log("webui", "✗ 未找到有效的 Python 解释器！")
            self.log("webui", "Hermes WebUI 需要 Python 3.9+ 才能启动")
            self.log("webui", "请安装 Python 3.9+ 到默认路径，或点击「⚙ 设置」手动指定 Python 路径")
            self.log("webui", "推荐下载: https://www.python.org/downloads/")
            self.set_state("webui", ServiceState.ERROR)
            return False

        port = cfg.get("webui_port", 8787)
        host = cfg.get("webui_host", "127.0.0.1")
        auto_browser = cfg.get("auto_open_browser", True)

        self.set_state("webui", ServiceState.STARTING)
        self._stop_events["webui"].clear()

        def _run():
            try:
                cmd = [
                    python_exe,
                    str(WEBUI_BOOTSTRAP),
                    str(port),
                    "--host", host,
                ]
                if not auto_browser:
                    cmd.append("--no-browser")

                self.log("webui", f"正在启动 WebUI...")
                self.log("webui", f"   > {python_exe} bootstrap.py {port} --host {host}")

                # Determine Hermes Agent directory for bootstrap.py
                hermes_exe = find_hermes()
                agent_dir = None
                if hermes_exe:
                    # Resolve agent dir from hermes executable
                    p = Path(hermes_exe).resolve()
                    # Look for run_agent.py in parent/grandparent
                    for parent in [p.parent, p.parent.parent, p.parent.parent.parent]:
                        if (parent / "run_agent.py").exists():
                            agent_dir = str(parent)
                            break

                # Set environment so bootstrap.py can find the agent
                env = os.environ.copy()
                if agent_dir:
                    env["HERMES_WEBUI_AGENT_DIR"] = agent_dir
                    self.log("webui", f"   Agent目录: {agent_dir}")
                # Ensure HERMES_HOME is set for Windows path conventions
                hermes_home = os.environ.get("HERMES_HOME")
                if not hermes_home:
                    # Default Hermes data location on Windows
                    candidate = Path.home() / ".hermes"
                    if candidate.exists():
                        env["HERMES_HOME"] = str(candidate)
                    else:
                        alt = Path(os.environ.get("LOCALAPPDATA", "")) / "hermes"
                        if alt.exists():
                            env["HERMES_HOME"] = str(alt)

                # bootstrap.py handles health check and browser launch internally
                proc = subprocess.Popen(
                    cmd,
                    cwd=str(WEBUI_DIR),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    env=env,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                self._processes["webui"] = proc

                # Read output line by line for real-time logging
                url = None
                for line in iter(proc.stdout.readline, ""):
                    if self._stop_events["webui"].is_set():
                        proc.terminate()
                        break
                    line = line.strip()
                    if line:
                        self.log("webui", line)
                    # Detect the URL from bootstrap output
                    if "ready" in line.lower() and "http" in line.lower():
                        m = re.search(r'http[s]?://[^\s]+', line)
                        if m:
                            url = m.group(0)
                            self._webui_url = url
                    if "http://" in line and ":" in line:
                        m = re.search(r'http[s]?://[^\s]+', line)
                        if m:
                            url = m.group(0)
                            self._webui_url = url

                proc.wait()
                if proc.returncode == 0 and url:
                    self.log("webui", f"✓ WebUI 已就绪 — {url}")
                    self.set_state("webui", ServiceState.RUNNING)
                    # bootstrap.py already opens the browser if --no-browser not set
                elif proc.returncode != 0:
                    self.log("webui", f"✗ WebUI 退出 (code {proc.returncode})")
                    self.set_state("webui", ServiceState.ERROR)
                else:
                    self.log("webui", "✓ WebUI 已就绪")
                    self.set_state("webui", ServiceState.RUNNING)

            except FileNotFoundError:
                self.log("webui", f"✗ 找不到 Python: {python_exe}")
                self.set_state("webui", ServiceState.ERROR)
            except Exception as e:
                self.log("webui", f"✗ 启动失败: {e}")
                self.set_state("webui", ServiceState.ERROR)

        thread = threading.Thread(target=_run, daemon=True)
        self._threads["webui"] = thread
        thread.start()
        return True

    def stop_webui(self):
        if self._processes.get("webui"):
            try:
                self._processes["webui"].terminate()
                self._processes["webui"].wait(timeout=10)
            except Exception:
                try:
                    self._processes["webui"].kill()
                except Exception:
                    pass
            self._processes["webui"] = None
        self._stop_events["webui"].set()
        self.set_state("webui", ServiceState.STOPPED)
        self.log("webui", "■ WebUI 已停止")
        self._webui_url = None

    def stop_all(self):
        self.stop_webui()
        self.stop_agent()

    def start_all(self):
        """Start agent first, then webui after confirmation."""
        def _sequence():
            self.log("system", "══════ 启动全部 ══════")

            # Step 0: Check Python is available before anything else
            python_exe = find_python()
            if python_exe is None:
                self.log("webui", "✗ 未找到 Python 解释器，WebUI 无法启动")
                self.log("webui", "请安装 Python 3.9+ 或点击「⚙ 设置」配置 Python 路径")
                self.log("system", "══════ 启动失败 ══════")
                return

            # Step 1: Start/verify agent
            ok, msg = check_hermes_installed()
            self.log("agent", f"检查 Hermes Agent: {msg}")
            if not ok:
                self.log("agent", f"✗ {msg}")
                self.set_state("agent", ServiceState.ERROR)
                self.log("system", "══════ 启动失败 ══════")
                return

            self.set_state("agent", ServiceState.RUNNING)
            self.log("agent", "✓ Hermes Agent 已就绪")

            # Step 2: Check if webui is downloaded
            if not WEBUI_BOOTSTRAP.exists():
                self.log("webui", "WebUI 尚未下载，正在自动下载...")
                self._download_webui_internal()

            if not WEBUI_BOOTSTRAP.exists():
                self.log("webui", "✗ WebUI 下载失败，无法启动")
                self.log("system", "══════ 启动失败 ══════")
                return

            # Step 3: Start webui
            self.start_webui()
            self.log("system", "══════ 启动序列完成 ══════")

        thread = threading.Thread(target=_sequence, daemon=True)
        thread.start()

    # ── Download WebUI ──

    def download_webui(self):
        thread = threading.Thread(target=self._download_webui_internal, daemon=True)
        thread.start()

    def _download_webui_internal(self):
        self.log("webui", "正在获取最新版本信息...")
        release = get_latest_webui_release()
        if not release:
            self.log("webui", "✗ 无法获取版本信息，请检查网络连接")
            self.set_state("webui", ServiceState.ERROR)
            return

        tag = release["tag"]
        zip_url = release.get("zipball_url")
        if not zip_url:
            self.log("webui", "✗ 无法获取下载链接")
            self.set_state("webui", ServiceState.ERROR)
            return

        self.log("webui", f"发现最新版本: {tag}")
        self.log("webui", f"正在下载: {tag} ...")

        # Download to temp file
        try:
            tmp_dir = Path(tempfile.mkdtemp())
            tmp_zip = tmp_dir / "webui.zip"

            # Download with progress
            last_pct = [0]

            def _progress(downloaded, total):
                if total > 0:
                    pct = int(downloaded * 100 / total)
                    if pct > last_pct[0] and pct % 10 == 0:
                        last_pct[0] = pct
                        self.log("webui", f"   下载进度: {pct}% ({downloaded//1024}KB / {total//1024}KB)")

            download_with_progress(zip_url, tmp_zip, _progress)
            size_mb = tmp_zip.stat().st_size / (1024 * 1024)
            self.log("webui", f"下载完成 ({size_mb:.1f} MB)")

            # Extract
            self.log("webui", "正在解压到 hermes-webui/ ...")
            if WEBUI_DIR.exists():
                shutil.rmtree(WEBUI_DIR)
            extract_zip_with_top_dir(tmp_zip, WEBUI_DIR)
            self.log("webui", f"✓ 解压完成 → {WEBUI_DIR}")

            # Verify bootstrap.py exists
            if WEBUI_BOOTSTRAP.exists():
                self.log("webui", f"✓ WebUI {tag} 安装成功！")
                # Save version to config
                self.cfg = load_config()
                self.cfg["webui_version"] = tag
                save_config(self.cfg)
                # Update UI detail text
                if hasattr(self, "_webui_detail"):
                    self._webui_detail.configure(text=f"WebUI版本：{tag}")
            else:
                self.log("webui", "⚠ 解压完成，但未找到 bootstrap.py，结构可能已变化")

            # Cleanup
            shutil.rmtree(tmp_dir, ignore_errors=True)

        except Exception as e:
            self.log("webui", f"✗ 下载/解压失败: {e}")
            self.set_state("webui", ServiceState.ERROR)
            import traceback
            self.log("webui", traceback.format_exc())


# ─── GUI Application ─────────────────────────────────────────────────────────

class HermesLauncherApp(ctk.CTk):
    """Main application window — Win11 style modern GUI."""

    def __init__(self):
        super().__init__()

        self.cfg = load_config()

        # ── Window setup ──
        self.title(f"{APP_NAME} v{APP_VERSION}")
        self.geometry("820x620")
        self.minsize(700, 520)

        # Center on screen
        self.update_idletasks()
        w = self.winfo_width()
        h = self.winfo_height()
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        x = (sw - w) // 2
        y = (sh - h) // 2
        self.geometry(f"+{x}+{y}")

        # ── Theme ──
        theme = self.cfg.get("theme", "system")
        ctk.set_appearance_mode(theme)
        ctk.set_default_color_theme("blue")

        # ── Service Manager ──
        self.service_mgr = ServiceManager(self._on_log)

        # ── Build UI ──
        self._build_ui()

        # ── Bind close event ──
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # ── Initial check ──
        self.after(500, self._initial_check)

    # ── UI Builder ──

    def _build_ui(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        # ── Header ──
        header = ctk.CTkFrame(self, height=60, corner_radius=0)
        header.grid(row=0, column=0, sticky="ew")
        header.grid_columnconfigure(0, weight=1)
        header.grid_propagate(False)

        ctk.CTkLabel(
            header, text=f"  {APP_NAME}",
            font=ctk.CTkFont(size=20, weight="bold"),
            anchor="w",
        ).grid(row=0, column=0, padx=(20, 0), pady=(8, 0), sticky="w")

        ctk.CTkLabel(
            header, text="一键启动 Hermes Agent 和 WebUI",
            font=ctk.CTkFont(size=12),
            text_color="gray",
            anchor="w",
        ).grid(row=1, column=0, padx=(20, 0), pady=(0, 8), sticky="w")

        # Theme toggle
        self._theme_btn = ctk.CTkButton(
            header, text="🌓", width=40,
            command=self._toggle_theme,
            fg_color="transparent",
            hover_color=("gray85", "gray20"),
        )
        self._theme_btn.grid(row=0, column=1, rowspan=2, padx=(0, 10))

        # ── Status Cards ──
        cards_frame = ctk.CTkFrame(self, fg_color="transparent")
        cards_frame.grid(row=1, column=0, padx=15, pady=(15, 5), sticky="ew")
        cards_frame.grid_columnconfigure(0, weight=1, uniform="card")
        cards_frame.grid_columnconfigure(1, weight=1, uniform="card")

        # Agent Card
        self._agent_card = self._make_card(
            cards_frame, "🤖  Hermes Agent",
            row=0, column=0,
        )
        self._agent_status = ctk.CTkLabel(
            self._agent_card, text="— 未检测 —",
            font=ctk.CTkFont(size=13),
            text_color="gray",
        )
        self._agent_status.grid(row=1, column=0, columnspan=2, padx=15, pady=(0, 5), sticky="w")

        self._agent_detail = ctk.CTkLabel(
            self._agent_card, text="",
            font=ctk.CTkFont(size=11),
            text_color="gray",
        )
        self._agent_detail.grid(row=2, column=0, columnspan=2, padx=15, pady=(0, 8), sticky="w")

        agent_btn_frame = ctk.CTkFrame(self._agent_card, fg_color="transparent")
        agent_btn_frame.grid(row=3, column=0, columnspan=2, padx=15, pady=(0, 12), sticky="ew")
        agent_btn_frame.grid_columnconfigure(0, weight=1)
        agent_btn_frame.grid_columnconfigure(1, weight=1)

        self._agent_start_btn = ctk.CTkButton(
            agent_btn_frame, text="▶ 启动 Agent",
            command=self._start_agent,
            fg_color="#2b7a3a", hover_color="#1f5c2a",
        )
        self._agent_start_btn.grid(row=0, column=0, padx=(0, 4), sticky="ew")

        self._agent_stop_btn = ctk.CTkButton(
            agent_btn_frame, text="■ 停止",
            command=self._stop_agent,
            state="disabled",
            fg_color="#7a2b2b", hover_color="#5c1f1f",
        )
        self._agent_stop_btn.grid(row=0, column=1, padx=(4, 0), sticky="ew")

        # WebUI Card
        self._webui_card = self._make_card(
            cards_frame, "🌐  Hermes WebUI",
            row=0, column=1,
        )
        self._webui_status = ctk.CTkLabel(
            self._webui_card, text="— 未检测 —",
            font=ctk.CTkFont(size=13),
            text_color="gray",
        )
        self._webui_status.grid(row=1, column=0, columnspan=2, padx=15, pady=(0, 5), sticky="w")

        self._webui_detail = ctk.CTkLabel(
            self._webui_card, text="",
            font=ctk.CTkFont(size=11),
            text_color="gray",
        )
        self._webui_detail.grid(row=2, column=0, columnspan=2, padx=15, pady=(0, 8), sticky="w")

        webui_btn_frame = ctk.CTkFrame(self._webui_card, fg_color="transparent")
        webui_btn_frame.grid(row=3, column=0, columnspan=2, padx=15, pady=(0, 12), sticky="ew")
        webui_btn_frame.grid_columnconfigure(0, weight=1)
        webui_btn_frame.grid_columnconfigure(1, weight=1)

        self._webui_start_btn = ctk.CTkButton(
            webui_btn_frame, text="▶ 启动 WebUI",
            command=self._start_webui,
            fg_color="#2b5f7a", hover_color="#1f425c",
        )
        self._webui_start_btn.grid(row=0, column=0, padx=(0, 4), sticky="ew")

        self._webui_open_btn = ctk.CTkButton(
            webui_btn_frame, text="↗ 打开",
            command=self._open_webui,
            state="disabled",
            fg_color="#5a4a2b", hover_color="#42361f",
        )
        self._webui_open_btn.grid(row=0, column=1, padx=(4, 0), sticky="ew")

        # ── Log Output ──
        log_frame = ctk.CTkFrame(self)
        log_frame.grid(row=2, column=0, padx=15, pady=(8, 5), sticky="nsew")
        log_frame.grid_columnconfigure(0, weight=1)
        log_frame.grid_rowconfigure(1, weight=1)

        log_header = ctk.CTkFrame(log_frame, fg_color="transparent")
        log_header.grid(row=0, column=0, padx=10, pady=(8, 4), sticky="ew")
        log_header.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            log_header, text="📋 输出日志",
            font=ctk.CTkFont(size=13, weight="bold"),
        ).grid(row=0, column=0, sticky="w")

        self._log_clear_btn = ctk.CTkButton(
            log_header, text="清空", width=60,
            command=self._clear_log,
            fg_color="transparent",
            border_width=1,
            text_color=("gray10", "gray90"),
        )
        self._log_clear_btn.grid(row=0, column=1, padx=(0, 0))

        self._log_text = ctk.CTkTextbox(
            log_frame,
            font=ctk.CTkFont(family="Consolas", size=12),
            wrap="word",
        )
        self._log_text.grid(row=1, column=0, padx=10, pady=(0, 10), sticky="nsew")
        self._log_text.configure(state="disabled")

        # ── Bottom Action Bar ──
        action_frame = ctk.CTkFrame(self, fg_color="transparent")
        action_frame.grid(row=3, column=0, padx=15, pady=(3, 15), sticky="ew")
        action_frame.grid_columnconfigure(1, weight=1)

        self._start_all_btn = ctk.CTkButton(
            action_frame, text="▶ 启动全部",
            command=self._start_all,
            fg_color="#2b7a3a", hover_color="#1f5c2a",
            font=ctk.CTkFont(size=14, weight="bold"),
            height=36,
        )
        self._start_all_btn.grid(row=0, column=0, padx=(0, 6))

        self._stop_all_btn = ctk.CTkButton(
            action_frame, text="■ 停止全部",
            command=self._stop_all,
            fg_color="#7a2b2b", hover_color="#5c1f1f",
            font=ctk.CTkFont(size=14),
            height=36,
        )
        self._stop_all_btn.grid(row=0, column=1, padx=(6, 6), sticky="w")

        self._download_btn = ctk.CTkButton(
            action_frame, text="📥 下载/更新 WebUI",
            command=self._download_webui,
            fg_color="transparent",
            border_width=1,
            text_color=("gray10", "gray90"),
        )
        self._download_btn.grid(row=0, column=2, padx=(6, 6))

        self._settings_btn = ctk.CTkButton(
            action_frame, text="⚙ 设置",
            command=self._open_settings,
            fg_color="transparent",
            border_width=1,
            text_color=("gray10", "gray90"),
            width=80,
        )
        self._settings_btn.grid(row=0, column=3, padx=(6, 0))

    def _make_card(self, parent, title, row, column):
        """Create a status card frame."""
        card = ctk.CTkFrame(parent, corner_radius=12)
        card.grid(row=row, column=column, padx=6, pady=6, sticky="nsew")

        title_label = ctk.CTkLabel(
            card, text=title,
            font=ctk.CTkFont(size=16, weight="bold"),
            anchor="w",
        )
        title_label.grid(row=0, column=0, columnspan=2, padx=15, pady=(12, 4), sticky="w")

        return card

    # ── Periodic Polling ──

    def _poll_states(self):
        """Periodically update UI from service states."""
        # Agent
        agent_state = self.service_mgr.get_state("agent")
        self._update_agent_ui(agent_state)

        # WebUI
        webui_state = self.service_mgr.get_state("webui")
        self._update_webui_ui(webui_state)

        self.after(500, self._poll_states)

    def _update_agent_ui(self, state):
        is_running = state == ServiceState.RUNNING
        is_starting = state == ServiceState.STARTING
        is_error = state == ServiceState.ERROR

        if is_running:
            self._agent_status.configure(text="● 运行中", text_color="#4caf50")
            self._agent_start_btn.configure(state="disabled")
            self._agent_stop_btn.configure(state="normal")
        elif is_starting:
            self._agent_status.configure(text="⟳ 启动中...", text_color="#ff9800")
            self._agent_start_btn.configure(state="disabled")
            self._agent_stop_btn.configure(state="disabled")
        elif is_error:
            self._agent_status.configure(text="✗ 错误", text_color="#f44336")
            self._agent_start_btn.configure(state="normal")
            self._agent_stop_btn.configure(state="disabled")
        else:
            self._agent_status.configure(text="○ 已停止", text_color="gray")
            self._agent_start_btn.configure(state="normal")
            self._agent_stop_btn.configure(state="disabled")

    def _update_webui_ui(self, state):
        is_running = state == ServiceState.RUNNING
        is_starting = state == ServiceState.STARTING
        is_error = state == ServiceState.ERROR

        if is_running:
            self._webui_status.configure(text="● 运行中", text_color="#4caf50")
            self._webui_start_btn.configure(state="disabled")
            self._webui_open_btn.configure(state="normal")
        elif is_starting:
            self._webui_status.configure(text="⟳ 启动中...", text_color="#ff9800")
            self._webui_start_btn.configure(state="disabled")
            self._webui_open_btn.configure(state="disabled")
        elif is_error:
            self._webui_status.configure(text="✗ 错误", text_color="#f44336")
            self._webui_start_btn.configure(state="normal")
            self._webui_open_btn.configure(state="disabled")
        else:
            self._webui_status.configure(text="○ 已停止", text_color="gray")
            self._webui_start_btn.configure(state="normal")
            if WEBUI_BOOTSTRAP.exists():
                self._webui_open_btn.configure(state="disabled")
            else:
                self._webui_open_btn.configure(state="disabled")

    # ── Handlers ──

    def _start_agent(self):
        self.service_mgr.start_agent()

    def _stop_agent(self):
        self.service_mgr.stop_agent()

    def _start_webui(self):
        if not WEBUI_BOOTSTRAP.exists():
            self._log("webui", "⚠ WebUI 未安装，请先点击「下载/更新 WebUI」")
            return
        self.service_mgr.start_webui()

    def _open_webui(self):
        url = self.service_mgr.get_webui_url()
        if url:
            webbrowser.open(url)
        else:
            # Default URL
            port = self.cfg.get("webui_port", 8787)
            webbrowser.open(f"http://127.0.0.1:{port}")

    def _start_all(self):
        self.service_mgr.start_all()

    def _stop_all(self):
        self.service_mgr.stop_all()

    def _download_webui(self):
        self.service_mgr.download_webui()

    def _clear_log(self):
        self._log_text.configure(state="normal")
        self._log_text.delete("0.0", "end")
        self._log_text.configure(state="disabled")

    def _toggle_theme(self):
        current = ctk.get_appearance_mode()
        new = "Dark" if current == "Light" else "Light"
        ctk.set_appearance_mode(new)
        self.cfg["theme"] = new.lower()
        save_config(self.cfg)

    def _initial_check(self):
        """Run initial checks on startup."""
        # Check Hermes
        ok, detail = check_hermes_installed()
        if ok:
            ver = extract_agent_version(detail)
            display = f"Agent版本：{ver}" if ver else "Agent版本：已就绪"
            self._agent_detail.configure(text=display)
            self._log("agent", f"✓ Hermes Agent: {detail[:80]}…")
        else:
            self._agent_detail.configure(text="Agent版本：未安装")
            self._log("agent", f"⚠ {detail}")

        # Check WebUI
        if WEBUI_BOOTSTRAP.exists():
            wver = get_webui_version()
            if wver:
                self._webui_detail.configure(text=f"WebUI版本：{wver}")
                # Persist detected version to config
                if wver != self.cfg.get("webui_version", ""):
                    self.cfg["webui_version"] = wver
                    save_config(self.cfg)
            else:
                self._webui_detail.configure(text="WebUI版本：已安装")
            self._log("webui", "✓ WebUI 已安装")
        else:
            self._webui_detail.configure(text="WebUI版本：未安装")
            self._log("webui", "ℹ WebUI 未安装，可点击「下载/更新 WebUI」")

        # Start polling
        self._poll_states()

    def _on_log(self, source: str, msg: str):
        """Thread-safe log callback."""
        self.after(0, self._append_log, source, msg)

    def _append_log(self, source: str, msg: str):
        tag_map = {
            "agent": "🤖",
            "webui": "🌐",
            "system": "🔧",
        }
        emoji = tag_map.get(source, "•")
        ts = log_time()
        line = f"[{ts}] {emoji} {msg}\n"

        self._log_text.configure(state="normal")
        self._log_text.insert("end", line)
        self._log_text.see("end")
        self._log_text.configure(state="disabled")

    def _log(self, source: str, msg: str):
        self._append_log(source, msg)

    def _open_settings(self):
        SettingsDialog(self)

    def _on_close(self):
        """Clean shutdown."""
        self.service_mgr.stop_all()
        self.destroy()

    def save_settings(self, new_cfg: dict):
        self.cfg.update(new_cfg)
        save_config(self.cfg)
        # Apply theme change immediately
        ctk.set_appearance_mode(self.cfg.get("theme", "system"))


# ─── Settings Dialog ─────────────────────────────────────────────────────────

class SettingsDialog(ctk.CTkToplevel):
    """Settings dialog for Hermes Launcher."""

    def __init__(self, parent: HermesLauncherApp):
        super().__init__(parent)
        self.parent = parent
        self.cfg = parent.cfg.copy()

        self.title("设置")
        self.geometry("480x420")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        # Center on parent
        self.update_idletasks()
        px = parent.winfo_x()
        py = parent.winfo_y()
        pw = parent.winfo_width()
        ph = parent.winfo_height()
        w = self.winfo_width()
        h = self.winfo_height()
        self.geometry(f"+{px + (pw - w) // 2}+{py + (ph - h) // 2}")

        self._build_ui()

    def _build_ui(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        main = ctk.CTkScrollableFrame(self)
        main.grid(row=0, column=0, padx=20, pady=20, sticky="nsew")
        main.grid_columnconfigure(1, weight=1)

        row = [0]

        def section(title):
            ctk.CTkLabel(
                main, text=title,
                font=ctk.CTkFont(size=14, weight="bold"),
            ).grid(row=row[0], column=0, columnspan=2, padx=0, pady=(12, 6), sticky="w")
            row[0] += 1

        def label(text):
            ctk.CTkLabel(
                main, text=text,
                anchor="w",
            ).grid(row=row[0], column=0, padx=(0, 10), pady=4, sticky="w")
            row[0] += 1

        def spacer():
            row[0] += 1

        # ── Theme ──
        section("外观")
        self._theme_var = ctk.StringVar(value=self.cfg.get("theme", "system"))
        theme_menu = ctk.CTkOptionMenu(
            main, values=["system", "dark", "light"],
            variable=self._theme_var,
        )
        theme_menu.grid(row=row[0], column=0, columnspan=2, pady=(0, 4), sticky="w")
        row[0] += 1
        spacer()

        # ── WebUI ──
        section("WebUI 设置")
        ctk.CTkLabel(main, text="端口:").grid(row=row[0], column=0, padx=(0, 10), pady=4, sticky="w")
        self._port_var = ctk.StringVar(value=str(self.cfg.get("webui_port", 8787)))
        port_entry = ctk.CTkEntry(main, textvariable=self._port_var, width=100)
        port_entry.grid(row=row[0], column=1, pady=4, sticky="w")
        row[0] += 1

        ctk.CTkLabel(main, text="主机:").grid(row=row[0], column=0, padx=(0, 10), pady=4, sticky="w")
        self._host_var = ctk.StringVar(value=self.cfg.get("webui_host", "127.0.0.1"))
        host_entry = ctk.CTkEntry(main, textvariable=self._host_var, width=180)
        host_entry.grid(row=row[0], column=1, pady=4, sticky="w")
        row[0] += 1

        self._browser_var = ctk.BooleanVar(value=self.cfg.get("auto_open_browser", True))
        browser_check = ctk.CTkCheckBox(
            main, text="启动时自动打开浏览器",
            variable=self._browser_var,
        )
        browser_check.grid(row=row[0], column=0, columnspan=2, pady=4, sticky="w")
        row[0] += 1
        spacer()

        # ── Paths ──
        section("路径设置（留空 = 自动检测）")
        ctk.CTkLabel(main, text="Python 路径:").grid(row=row[0], column=0, padx=(0, 10), pady=4, sticky="w")
        self._python_var = ctk.StringVar(value=self.cfg.get("python_path", ""))
        python_entry = ctk.CTkEntry(main, textvariable=self._python_var)
        python_entry.grid(row=row[0], column=1, pady=4, sticky="ew")
        row[0] += 1

        ctk.CTkLabel(main, text="Hermes 路径:").grid(row=row[0], column=0, padx=(0, 10), pady=4, sticky="w")
        self._hermes_var = ctk.StringVar(value=self.cfg.get("hermes_path", ""))
        hermes_entry = ctk.CTkEntry(main, textvariable=self._hermes_var)
        hermes_entry.grid(row=row[0], column=1, pady=4, sticky="ew")
        row[0] += 1
        spacer()

        # ── Buttons ──
        btn_frame = ctk.CTkFrame(main, fg_color="transparent")
        btn_frame.grid(row=row[0], column=0, columnspan=2, pady=(10, 0), sticky="ew")
        btn_frame.grid_columnconfigure(0, weight=1)
        btn_frame.grid_columnconfigure(1, weight=1)

        ctk.CTkButton(
            btn_frame, text="保存",
            command=self._save,
            fg_color="#2b7a3a", hover_color="#1f5c2a",
        ).grid(row=0, column=0, padx=(0, 4), sticky="ew")

        ctk.CTkButton(
            btn_frame, text="取消",
            command=self.destroy,
            fg_color="transparent",
            border_width=1,
            text_color=("gray10", "gray90"),
        ).grid(row=0, column=1, padx=(4, 0), sticky="ew")

    def _save(self):
        """Validate and save settings."""
        try:
            port = int(self._port_var.get())
            if port < 1 or port > 65535:
                raise ValueError
        except ValueError:
            self._port_var.set("8787")
            port = 8787

        self.cfg.update({
            "theme": self._theme_var.get(),
            "webui_port": port,
            "webui_host": self._host_var.get().strip() or "127.0.0.1",
            "auto_open_browser": self._browser_var.get(),
            "python_path": self._python_var.get().strip(),
            "hermes_path": self._hermes_var.get().strip(),
        })
        self.parent.save_settings(self.cfg)
        self.destroy()


# ─── Entry Point ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Ensure customtkinter is installed
    try:
        import customtkinter
    except ImportError:
        print("正在安装 customtkinter...")
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "customtkinter"],
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        import customtkinter

    app = HermesLauncherApp()
    app.mainloop()
