#!/usr/bin/env python3

import sys
import os
import json
import logging
import platform
import re
import stat
import subprocess
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin

import psutil
import requests
from PyQt5 import QtWidgets, QtGui, QtCore
from PyQt5.QtWidgets import QSystemTrayIcon, QMenu, QApplication

try:
    from packaging import version as pkg_version
except Exception:
    pkg_version = None

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BUNDLE_DIR = getattr(sys, "_MEIPASS", SCRIPT_DIR)
RUNTIME_DIR = os.path.dirname(os.path.abspath(sys.executable)) if getattr(sys, "frozen", False) else SCRIPT_DIR


def first_existing_path(paths: List[str]) -> Optional[str]:
    seen = set()
    for candidate in paths:
        normalized = os.path.normpath(candidate)
        if normalized in seen:
            continue
        seen.add(normalized)
        if os.path.exists(normalized):
            return normalized
    return None


def resolve_client_config_path() -> str:
    search_roots = [RUNTIME_DIR, SCRIPT_DIR, os.getcwd()]
    candidates: List[str] = []

    for root in search_roots:
        current = os.path.abspath(root)
        for _ in range(6):
            candidates.append(os.path.join(current, "client_config.json"))
            parent = os.path.dirname(current)
            if parent == current:
                break
            current = parent

    fallback_path = os.path.join(RUNTIME_DIR, "..", "client_config.json")
    return first_existing_path(candidates) or os.path.normpath(fallback_path)


CONFIG_PATH = resolve_client_config_path()
ICON_PATH = first_existing_path([
    os.path.join(BUNDLE_DIR, "icon.png"),
    os.path.join(SCRIPT_DIR, "icon.png"),
    os.path.join(RUNTIME_DIR, "icon.png"),
]) or os.path.join(BUNDLE_DIR, "icon.png")
LOG_DIR = os.path.join(RUNTIME_DIR, "logs")
LOG_PATH = os.path.join(LOG_DIR, "tray.log")

UPDATES_INDEX_URL = "https://updates.breakeventx.com"
MANIFEST_NAME = "sudo_manifest.json"
DOWNLOAD_BASE_DEFAULT = "https://data.breakeventx.com:64444/content-cache/updates"
MANIFEST_URL_DEFAULT = f"{DOWNLOAD_BASE_DEFAULT}/{MANIFEST_NAME}"
UPDATE_REFRESH_SECONDS = 15 * 60

SLAVE_SERVICE_FALLBACK = {
    "windows": "BreakEvenSlave",
    "linux": "breakeven-slave.service",
    "macos": "com.breakeven.slave",
}

DASHBOARD_PROC_TOKENS = [
    "breakeven dashboard",
    "breakeven.exe",
    "breakeven",
    "electron",
]

SLAVE_PROC_TOKENS = [
    "breakeven_slave",
    "breakevenslaveservicehost",
    "slave.exe",
    "slave.py",
]


def compact_process_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def setup_logger() -> logging.Logger:
    os.makedirs(LOG_DIR, exist_ok=True)
    logger = logging.getLogger("breakeven_tray")
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return logger

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    return logger


LOGGER = setup_logger()


def log_info(message: str) -> None:
    LOGGER.info(message)


def log_error(message: str) -> None:
    LOGGER.error(message)


def get_os_type() -> str:
    os_name = platform.system().lower()
    if os_name == "windows":
        return "windows"
    if os_name == "linux":
        return "linux"
    if os_name == "darwin":
        return "macos"
    raise RuntimeError(f"Unsupported OS: {os_name}")


def read_json(path: str) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return None


def parse_version(text: str):
    if pkg_version is not None:
        return pkg_version.parse(text)
    parts = [int(part) if part.isdigit() else part for part in re.split(r"[.-]", text)]
    return tuple(parts)


def resolve_manifest_url() -> str:
    try:
        response = requests.get(UPDATES_INDEX_URL, timeout=12)
        response.raise_for_status()
        html = response.text

        href_match = re.search(
            rf'href=["\\\']([^"\\\']*{re.escape(MANIFEST_NAME)}[^"\\\']*)["\\\']',
            html,
            flags=re.IGNORECASE,
        )
        if href_match:
            return urljoin(UPDATES_INDEX_URL + "/", href_match.group(1).strip())

        endpoint_match = re.search(
            r"Endpoint:\s*(https?://[^<\s]+)",
            html,
            flags=re.IGNORECASE,
        )
        if endpoint_match:
            endpoint = endpoint_match.group(1).strip().rstrip("/")
            return f"{endpoint}/content-cache/updates/{MANIFEST_NAME}"
    except Exception as exc:
        log_error(f"Manifest URL resolution failed: {exc}")

    return MANIFEST_URL_DEFAULT


@dataclass
class UpdateState:
    checked_at: float = 0.0
    local_version: str = "unknown"
    latest_version: str = "unknown"
    up_to_date: Optional[bool] = None
    error: Optional[str] = None


@dataclass
class TrayState:
    dashboard_running: bool = False
    slave_running: bool = False
    update_state: UpdateState = field(default_factory=UpdateState)


class BackendController:
    def __init__(self) -> None:
        self._dashboard_handle: Optional[subprocess.Popen] = None
        self._state = TrayState()
        self._state_lock = threading.Lock()
        log_info(f"Tray script dir: {SCRIPT_DIR}")
        log_info(f"Tray bundle dir: {BUNDLE_DIR}")
        log_info(f"Tray runtime dir: {RUNTIME_DIR}")
        log_info(f"Tray config path: {CONFIG_PATH}")
        log_info(f"Platform: {platform.platform()}")

    def load_config(self) -> dict:
        config = read_json(CONFIG_PATH)
        if not isinstance(config, dict):
            raise RuntimeError(f"Unable to read client config at {CONFIG_PATH}")
        return config

    def service_manifest_paths(self, config: dict) -> List[str]:
        service_install_path = config.get("serviceInstallPath") or config.get("installPath")
        if not service_install_path:
            return []
        base = os.path.join(service_install_path, "client_service")
        return [
            os.path.join(base, "service_manifest_slave.json"),
            os.path.join(base, "service_manifest.json"),
        ]

    def resolve_slave_control_commands(self, config: dict) -> Dict[str, List[str]]:
        manifest_data = None
        for path in self.service_manifest_paths(config):
            data = read_json(path)
            if isinstance(data, dict):
                manifest_data = data
                break

        if isinstance(manifest_data, dict):
            control = manifest_data.get("control")
            commands = control.get("commands") if isinstance(control, dict) else None
            if isinstance(commands, dict):
                parsed: Dict[str, List[str]] = {}
                for key in ("start", "stop", "status"):
                    value = commands.get(key)
                    if isinstance(value, list) and value:
                        parsed[key] = [str(item) for item in value]
                if parsed:
                    return parsed

        os_type = get_os_type()
        identifier = SLAVE_SERVICE_FALLBACK[os_type]
        if os_type == "windows":
            return {
                "start": [
                    "powershell.exe",
                    "-NoLogo",
                    "-NoProfile",
                    "-Command",
                    f"Start-Service -Name '{identifier}'",
                ],
                "stop": [
                    "powershell.exe",
                    "-NoLogo",
                    "-NoProfile",
                    "-Command",
                    f"Stop-Service -Name '{identifier}'",
                ],
                "status": [
                    "powershell.exe",
                    "-NoLogo",
                    "-NoProfile",
                    "-Command",
                    f"(Get-Service -Name '{identifier}').Status",
                ],
            }
        if os_type == "linux":
            return {
                "start": ["systemctl", "--user", "start", identifier],
                "stop": ["systemctl", "--user", "stop", identifier],
                "status": ["systemctl", "--user", "is-active", identifier],
            }
        return {
            "start": ["launchctl", "start", identifier],
            "stop": ["launchctl", "stop", identifier],
            "status": ["launchctl", "list", identifier],
        }

    def run_command(self, command: List[str], timeout: int = 15) -> Tuple[int, str, str]:
        log_info(f"Executing command: {' '.join(command)}")
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
        )
        return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()

    def _process_contains_tokens(self, proc: psutil.Process, tokens: List[str]) -> bool:
        try:
            name = (proc.name() or "").lower()
            cmdline = " ".join(proc.cmdline() or []).lower()
            compact_name = compact_process_text(name)
            compact_cmdline = compact_process_text(cmdline)
            for token in tokens:
                token_lower = token.lower()
                compact_token = compact_process_text(token_lower)
                if token_lower in name or token_lower in cmdline:
                    return True
                if compact_token and (compact_token in compact_name or compact_token in compact_cmdline):
                    return True
            return False
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            return False

    def dashboard_image_names(self, launch_target: Optional[str]) -> List[str]:
        names: List[str] = []
        if launch_target:
            base_name = os.path.basename(launch_target)
            stem, ext = os.path.splitext(base_name)
            names.extend([base_name, stem])
            compact_stem = compact_process_text(stem)
            if compact_stem:
                names.append(compact_stem)
                if ext:
                    names.append(f"{compact_stem}{ext.lower()}")

        names.extend([
            "BreakEven Dashboard.exe",
            "BreakEven Dashboard",
            "BreakEven.exe",
            "BreakEven",
            "breakevendashboard.exe",
            "breakevendashboard",
        ])

        deduped: List[str] = []
        seen = set()
        for name in names:
            key = name.lower()
            if name and key not in seen:
                seen.add(key)
                deduped.append(name)
        return deduped

    def _process_matches_path(self, proc: psutil.Process, target_path: Optional[str]) -> bool:
        if not target_path:
            return False
        try:
            exe_path = proc.exe()
            if exe_path and os.path.normcase(exe_path) == os.path.normcase(target_path):
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            return False
        except Exception:
            return False
        return False

    def _terminate_process_tree(self, proc: psutil.Process) -> bool:
        terminated = False
        try:
            children = proc.children(recursive=True)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            return False

        for child in reversed(children):
            try:
                child.terminate()
                terminated = True
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue

        for child in reversed(children):
            try:
                child.wait(timeout=3)
            except (psutil.TimeoutExpired, psutil.NoSuchProcess):
                try:
                    child.kill()
                    terminated = True
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    pass
            except (psutil.AccessDenied, psutil.ZombieProcess):
                pass

        try:
            proc.terminate()
            terminated = True
            proc.wait(timeout=5)
        except (psutil.TimeoutExpired, psutil.NoSuchProcess):
            try:
                proc.kill()
                terminated = True
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                pass
        except (psutil.AccessDenied, psutil.ZombieProcess):
            pass

        return terminated

    def resolve_dashboard_candidate(self, install_path: str) -> Optional[str]:
        dashboard_dir = os.path.join(install_path, "dashboard_gui")
        os_type = get_os_type()
        candidates: List[str] = []
        if os_type == "windows":
            candidates = [
                os.path.join(dashboard_dir, "BreakEven Dashboard.exe"),
                os.path.join(dashboard_dir, "BreakEven.exe"),
            ]
        elif os_type == "linux":
            candidates = [
                os.path.join(dashboard_dir, "BreakEven.AppImage"),
                os.path.join(dashboard_dir, "BreakEven.deb"),
                os.path.join(dashboard_dir, "BreakEven.rpm"),
            ]
        elif os_type == "macos":
            candidates = [
                os.path.join(dashboard_dir, "BreakEven.app"),
                os.path.join(dashboard_dir, "BreakEven.dmg"),
            ]

        for candidate in candidates:
            if os.path.exists(candidate):
                return candidate
        return None

    def find_dashboard_processes(self, launch_target: Optional[str]) -> List[psutil.Process]:
        tokens = list(DASHBOARD_PROC_TOKENS)
        if launch_target:
            target_name = os.path.basename(launch_target).lower()
            tokens.append(target_name)
            if target_name.endswith(".exe"):
                tokens.append(target_name.replace(".exe", ""))
            compact_target = compact_process_text(target_name)
            if compact_target:
                tokens.append(compact_target)

        matches: List[psutil.Process] = []
        seen_pids = set()
        for proc in psutil.process_iter(attrs=[]):
            if self._process_matches_path(proc, launch_target) or self._process_contains_tokens(proc, tokens):
                if proc.pid in seen_pids:
                    continue
                seen_pids.add(proc.pid)
                matches.append(proc)
        return matches

    def find_slave_processes(self) -> List[psutil.Process]:
        matches: List[psutil.Process] = []
        for proc in psutil.process_iter(attrs=[]):
            if self._process_contains_tokens(proc, SLAVE_PROC_TOKENS):
                matches.append(proc)
        return matches

    def is_dashboard_running(self) -> bool:
        try:
            config = self.load_config()
            launch_target = self.resolve_dashboard_candidate(config.get("installPath", ""))
        except Exception:
            launch_target = None

        if self._dashboard_handle is not None and self._dashboard_handle.poll() is None:
            return True
        return bool(self.find_dashboard_processes(launch_target))

    def open_dashboard(self) -> None:
        config = self.load_config()
        install_path = config.get("installPath")
        if not install_path:
            raise RuntimeError("installPath missing in client_config.json")

        launch_target = self.resolve_dashboard_candidate(install_path)
        if not launch_target:
            raise RuntimeError("No dashboard build found in installPath/dashboard_gui")

        os_type = get_os_type()
        if os_type == "windows":
            self._dashboard_handle = subprocess.Popen(
                [launch_target],
                cwd=os.path.dirname(launch_target),
                shell=False,
            )
            return

        if os_type == "linux":
            if launch_target.endswith(".AppImage"):
                mode = os.stat(launch_target).st_mode
                os.chmod(launch_target, mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
                self._dashboard_handle = subprocess.Popen(
                    [launch_target],
                    cwd=os.path.dirname(launch_target),
                    shell=False,
                )
                return
            subprocess.Popen(["xdg-open", launch_target], shell=False)
            return

        subprocess.Popen(["open", launch_target], shell=False)

    def close_dashboard(self) -> None:
        log_info("close_dashboard invoked")
        terminated = False
        os_type = get_os_type()
        if os_type == "windows":
            try:
                direct_kill = subprocess.run(
                    ["taskkill", "/IM", "breakevendashboard.exe", "/T", "/F"],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    shell=False,
                )
                log_info(
                    "Direct dashboard image kill result: "
                    f"code={direct_kill.returncode} stdout={(direct_kill.stdout or '').strip()} "
                    f"stderr={(direct_kill.stderr or '').strip()}"
                )
                if direct_kill.returncode == 0:
                    self._dashboard_handle = None
                    return
            except Exception as exc:
                log_error(f"Direct dashboard image kill failed: {exc}")

        handle_pid = None
        if self._dashboard_handle is not None:
            handle_pid = self._dashboard_handle.pid
            log_info(f"Dashboard handle present with pid={handle_pid}")
            if os_type != "windows" and self._dashboard_handle.poll() is None:
                try:
                    self._dashboard_handle.terminate()
                    self._dashboard_handle.wait(timeout=8)
                    terminated = True
                except Exception:
                    try:
                        self._dashboard_handle.kill()
                        terminated = True
                    except Exception:
                        pass
            self._dashboard_handle = None

        try:
            config = self.load_config()
            launch_target = self.resolve_dashboard_candidate(config.get("installPath", ""))
        except Exception:
            launch_target = None

        matched_processes = self.find_dashboard_processes(launch_target)
        log_info(f"Dashboard process matches found: {len(matched_processes)}")
        if matched_processes:
            log_info(
                "Closing dashboard processes: "
                + ", ".join(f"{proc.pid}:{proc.name()}" for proc in matched_processes)
            )
        for proc in matched_processes:
            if os_type == "windows":
                try:
                    log_info(f"Attempting taskkill for dashboard pid={proc.pid} name={proc.name()}")
                    kill_proc = subprocess.run(
                        ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                        capture_output=True,
                        text=True,
                        timeout=10,
                        shell=False,
                    )
                    if kill_proc.returncode == 0:
                        log_info(f"taskkill succeeded for dashboard pid={proc.pid} name={proc.name()}")
                        terminated = True
                        continue
                    log_info(
                        f"taskkill returned code={kill_proc.returncode} for dashboard pid={proc.pid}: "
                        f"stdout={(kill_proc.stdout or '').strip()} stderr={(kill_proc.stderr or '').strip()}"
                    )
                except Exception as exc:
                    log_error(f"taskkill failed for dashboard pid={proc.pid}: {exc}")
            terminated = self._terminate_process_tree(proc) or terminated

        if os_type == "windows" and handle_pid is not None and not matched_processes:
            try:
                log_info(f"Attempting fallback taskkill for stored handle pid={handle_pid}")
                handle_kill = subprocess.run(
                    ["taskkill", "/PID", str(handle_pid), "/T", "/F"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    shell=False,
                )
                if handle_kill.returncode == 0:
                    log_info(f"Fallback taskkill succeeded for stored handle pid={handle_pid}")
                    terminated = True
                else:
                    log_info(
                        f"Fallback taskkill returned code={handle_kill.returncode} for stored handle pid={handle_pid}: "
                        f"stdout={(handle_kill.stdout or '').strip()} stderr={(handle_kill.stderr or '').strip()}"
                    )
            except Exception as exc:
                log_error(f"Fallback taskkill failed for stored handle pid={handle_pid}: {exc}")

        if os_type == "windows":
            image_names = self.dashboard_image_names(launch_target)
            for image_name in image_names:
                try:
                    proc = subprocess.run(
                        ["taskkill", "/IM", image_name, "/T", "/F"],
                        capture_output=True,
                        text=True,
                        timeout=10,
                        shell=False,
                    )
                    if proc.returncode == 0:
                        log_info(f"taskkill succeeded for dashboard image: {image_name}")
                    if proc.returncode == 0:
                        terminated = True
                except Exception:
                    pass

            try:
                ps_kill = subprocess.run(
                    [
                        "powershell.exe",
                        "-NoLogo",
                        "-NoProfile",
                        "-Command",
                        "$killed = @(Get-Process | Where-Object { $_.ProcessName -match 'breakeven.*dashboard|dashboard.*breakeven' }); "
                        "if ($killed.Count -gt 0) { $killed | Stop-Process -Force; Write-Output ($killed | ForEach-Object { $_.ProcessName } | Sort-Object -Unique | Join-String -Separator ',') }",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    shell=False,
                )
                if ps_kill.returncode == 0 and (ps_kill.stdout or "").strip():
                    log_info(f"PowerShell dashboard kill matched: {(ps_kill.stdout or '').strip()}")
                    terminated = True
            except Exception as exc:
                log_error(f"PowerShell dashboard kill failed: {exc}")

        time.sleep(1.0)
        if launch_target and self.is_dashboard_running():
            log_error("Dashboard still detected as running after close attempt")
            raise RuntimeError("Failed to close dashboard")

        if not terminated and launch_target:
            raise RuntimeError("Dashboard is not running.")

    def is_slave_process_running(self) -> bool:
        return bool(self.find_slave_processes())

    def is_slave_running(self) -> bool:
        config = self.load_config()
        commands = self.resolve_slave_control_commands(config)
        status_cmd = commands.get("status")
        service_running = False

        if status_cmd:
            try:
                code, stdout, stderr = self.run_command(status_cmd)
                text = f"{stdout}\n{stderr}".lower()
                os_type = get_os_type()
                if os_type == "windows":
                    service_running = "running" in text and "stopped" not in text
                elif os_type == "linux":
                    service_running = code == 0 and "active" in text
                else:
                    service_running = code == 0 and "could not find service" not in text
            except Exception as exc:
                log_error(f"Slave status lookup failed: {exc}")

        if service_running:
            return True
        return self.is_slave_process_running()

    def start_slave(self) -> None:
        config = self.load_config()
        commands = self.resolve_slave_control_commands(config)
        start_cmd = commands.get("start")
        if not start_cmd:
            raise RuntimeError("No slave start command found")
        code, _stdout, stderr = self.run_command(start_cmd)
        if code != 0:
            raise RuntimeError(stderr or f"Failed to start slave service (code={code})")

    def stop_slave(self) -> None:
        config = self.load_config()
        commands = self.resolve_slave_control_commands(config)
        stop_cmd = commands.get("stop")
        errors: List[str] = []
        if stop_cmd:
            try:
                code, _stdout, stderr = self.run_command(stop_cmd)
                if code != 0:
                    errors.append(stderr or f"Failed to stop slave service (code={code})")
            except Exception as exc:
                errors.append(str(exc))

        processes = self.find_slave_processes()
        for proc in processes:
            try:
                proc.terminate()
            except Exception as exc:
                errors.append(str(exc))

        if get_os_type() == "windows":
            for proc in self.find_slave_processes():
                try:
                    subprocess.run(
                        ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                        capture_output=True,
                        text=True,
                        timeout=10,
                        shell=False,
                    )
                except Exception as exc:
                    errors.append(str(exc))

        time.sleep(1.0)
        if self.is_slave_running():
            detail = errors[0] if errors else "Slave is still running after stop attempt"
            raise RuntimeError(detail)

    def refresh_local_state(self) -> TrayState:
        current = self.get_cached_state()
        state = TrayState(
            dashboard_running=self.is_dashboard_running(),
            slave_running=self.is_slave_running(),
            update_state=current.update_state,
        )
        with self._state_lock:
            self._state = state
        return state

    def update_cached_state(
        self,
        dashboard_running: Optional[bool] = None,
        slave_running: Optional[bool] = None,
        update_state: Optional[UpdateState] = None,
    ) -> TrayState:
        state = self.get_cached_state()
        if dashboard_running is not None:
            state.dashboard_running = dashboard_running
        if slave_running is not None:
            state.slave_running = slave_running
        if update_state is not None:
            state.update_state = update_state
        with self._state_lock:
            self._state = state
        return state

    def refresh_dashboard_state(self) -> TrayState:
        return self.update_cached_state(dashboard_running=self.is_dashboard_running())

    def refresh_slave_state(self) -> TrayState:
        return self.update_cached_state(slave_running=self.is_slave_running())

    def fetch_update_state(self) -> UpdateState:
        state = UpdateState(checked_at=time.time())
        try:
            config = self.load_config()
            state.local_version = str(config.get("version") or "0.0.0.0")
            os_type = get_os_type()
            manifest_url = resolve_manifest_url()
            response = requests.get(manifest_url, timeout=20)
            response.raise_for_status()
            manifest = response.json()
            stable = manifest.get("stable") or {}
            stable_os = stable.get(os_type) or {}
            latest_version = manifest.get("stable_version") or stable_os.get("version")
            if not latest_version:
                raise RuntimeError("stable version not present in manifest")
            state.latest_version = str(latest_version)
            state.up_to_date = parse_version(state.local_version) >= parse_version(state.latest_version)
            return state
        except Exception as exc:
            state.error = str(exc)
            state.up_to_date = None
            return state

    def snapshot_state(self) -> TrayState:
        state = TrayState(
            dashboard_running=self.is_dashboard_running(),
            slave_running=self.is_slave_running(),
            update_state=self.fetch_update_state(),
        )
        with self._state_lock:
            self._state = state
        return state

    def get_cached_state(self) -> TrayState:
        with self._state_lock:
            return TrayState(
                dashboard_running=self._state.dashboard_running,
                slave_running=self._state.slave_running,
                update_state=UpdateState(
                    checked_at=self._state.update_state.checked_at,
                    local_version=self._state.update_state.local_version,
                    latest_version=self._state.update_state.latest_version,
                    up_to_date=self._state.update_state.up_to_date,
                    error=self._state.update_state.error,
                ),
            )


class TrayApp(QtWidgets.QSystemTrayIcon):
    refresh_ready = QtCore.pyqtSignal()
    action_complete = QtCore.pyqtSignal(object)

    def __init__(self, icon, parent=None):
        super(TrayApp, self).__init__(icon, parent)
        self.setToolTip('BreakEven Client Service')
        self.paused = False
        self.service_process = None
        self.backend = BackendController()
        self.menu = QMenu(parent)

        self.dashboard_action = self.menu.addAction("🖥 Open Dashboard")
        self.dashboard_action.triggered.connect(self.open_dashboard)

        self.slave_action = self.menu.addAction("⏸ Pause Service")
        self.slave_action.triggered.connect(self.toggle_slave)

        self.update_action = self.menu.addAction("⬇ Check for Updates")
        self.update_action.triggered.connect(self.check_updates)

        self.quit_action = self.menu.addAction("❌ Quit")
        self.quit_action.triggered.connect(self.quit_app)

        self.setContextMenu(self.menu)
        self.activated.connect(self.icon_activated)
        self.refresh_ready.connect(self.refresh_menu_state)
        self.action_complete.connect(self.on_action_complete)

        self.refresh_timer = QtCore.QTimer()
        self.refresh_timer.setInterval(UPDATE_REFRESH_SECONDS * 1000)
        self.refresh_timer.timeout.connect(self.refresh_state_async)
        self.refresh_timer.start()

        self._refresh_in_flight = False
        self._action_in_flight = False
        self.refresh_state_async()

    def icon_activated(self, reason):
        if reason == QSystemTrayIcon.Trigger:
            self.refresh_menu_state()
            self.contextMenu().popup(QtGui.QCursor.pos())
            self.refresh_state_async()

    def refresh_menu_state(self):
        state = self.backend.get_cached_state()
        self.dashboard_action.setText(
            "❎ Close Dashboard" if state.dashboard_running else "🖥 Open Dashboard"
        )
        self.slave_action.setText(
            "🛑 Stop Slave" if state.slave_running else "▶ Start Slave"
        )

        if state.update_state.error:
            self.update_action.setText("⬇ Check for Updates (error)")
        elif state.update_state.up_to_date is True:
            self.update_action.setText(
                f"✅ Up to Date ({state.update_state.local_version})"
            )
        elif state.update_state.up_to_date is False:
            self.update_action.setText(
                f"⬇ Update Available ({state.update_state.latest_version})"
            )
        else:
            self.update_action.setText("⬇ Check for Updates")

    def refresh_state_async(self):
        if self._refresh_in_flight:
            return
        self._refresh_in_flight = True

        def worker():
            try:
                self.backend.snapshot_state()
                self.refresh_ready.emit()
            except Exception as exc:
                log_error(f"Status refresh failed: {exc}")
            finally:
                self._refresh_in_flight = False

        threading.Thread(target=worker, daemon=True).start()

    def run_action_async(self, action, error_prefix: str, post_action_refresh=None):
        if self._action_in_flight:
            log_info(f"Action ignored while another action is in flight: {error_prefix}")
            return
        self._action_in_flight = True

        def worker():
            error_message = None
            try:
                action()
            except Exception as exc:
                error_message = f"{error_prefix}: {exc}"
                log_error(error_message)
            finally:
                if post_action_refresh is not None:
                    try:
                        post_action_refresh()
                    except Exception as refresh_exc:
                        log_error(f"Post-action refresh failed: {refresh_exc}")
                self.action_complete.emit(error_message)

        threading.Thread(target=worker, daemon=True).start()

    @QtCore.pyqtSlot(object)
    def on_action_complete(self, error_message):
        self._action_in_flight = False
        self.refresh_menu_state()
        self.refresh_state_async()
        if error_message:
            self.showMessage(
                "BreakEven Tray",
                error_message,
                QtWidgets.QSystemTrayIcon.Warning,
                5000,
            )

    def run_dashboard_action(self, action, error_prefix: str, expected_dashboard_running: Optional[bool] = None):
        error_message = None
        try:
            log_info(f"Running dashboard action: {error_prefix}")
            action()
        except Exception as exc:
            error_message = f"{error_prefix}: {exc}"
            log_error(error_message)
        finally:
            try:
                if expected_dashboard_running is None:
                    self.backend.refresh_dashboard_state()
                else:
                    self.backend.update_cached_state(dashboard_running=expected_dashboard_running)
            except Exception as refresh_exc:
                log_error(f"Dashboard refresh failed: {refresh_exc}")
            self.refresh_menu_state()
            self.refresh_state_async()
            if error_message:
                self.showMessage(
                    "BreakEven Tray",
                    error_message,
                    QtWidgets.QSystemTrayIcon.Warning,
                    5000,
                )

    def open_dashboard(self):
        state = self.backend.get_cached_state()
        if state.dashboard_running:
            self.run_dashboard_action(
                self.backend.close_dashboard,
                "Dashboard toggle failed",
                expected_dashboard_running=False,
            )
            return
        self.run_dashboard_action(
            self.backend.open_dashboard,
            "Dashboard toggle failed",
            expected_dashboard_running=True,
        )

    def toggle_slave(self):
        state = self.backend.refresh_local_state()
        if state.slave_running:
            self.run_action_async(
                self.backend.stop_slave,
                "Failed to stop slave service",
                post_action_refresh=self.backend.refresh_slave_state,
            )
            return
        self.run_action_async(
            self.backend.start_slave,
            "Failed to start slave service",
            post_action_refresh=self.backend.refresh_slave_state,
        )

    def check_updates(self):
        self.refresh_state_async()
        state = self.backend.get_cached_state().update_state
        if state.error:
            self.showMessage(
                "BreakEven Tray",
                f"Update check failed: {state.error}",
                QtWidgets.QSystemTrayIcon.Warning,
                5000,
            )
            return

        if state.up_to_date is True:
            message = f"Client is up to date ({state.local_version})"
        elif state.up_to_date is False:
            message = f"Update available: {state.latest_version}"
        else:
            message = "Update status unavailable"

        self.showMessage(
            "BreakEven Tray",
            message,
            QtWidgets.QSystemTrayIcon.Information,
            5000,
        )

    def load_config(self):
        try:
            with open(resolve_client_config_path(), 'r', encoding='utf-8') as handle:
                return json.load(handle)
        except Exception as exc:
            log_error(f"Error reading config: {exc}")
            return None

    def quit_app(self):
        self.refresh_timer.stop()
        QApplication.quit()


def main():
    try:
        app = QApplication(sys.argv)
        app.setQuitOnLastWindowClosed(False)
        icon = QtGui.QIcon(ICON_PATH)
        tray_icon = TrayApp(icon)
        tray_icon.show()
        log_info("tray_app is ready")
        print("tray_app is ready", flush=True)
        print("ready", flush=True)
        sys.exit(app.exec_())
    except Exception as exc:
        log_error(f"tray_app failed: {exc}")
        log_error(traceback.format_exc())
        print(f"tray_app failed: {exc}", flush=True)
        sys.exit(1)


if __name__ == '__main__':
    main()
