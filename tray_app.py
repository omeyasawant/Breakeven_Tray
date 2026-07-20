#!/usr/bin/env python3

import sys
import os
import json
import faulthandler
import logging
import platform
import re
import socket
import stat
import subprocess
import tempfile
import threading
import time
import traceback
import signal
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin

import psutil
import requests


def _bootstrap_first_existing_dir(paths: List[str]) -> str:
    seen = set()
    for candidate in paths:
        normalized = os.path.normpath(candidate)
        if normalized in seen:
            continue
        seen.add(normalized)
        if os.path.isdir(normalized):
            return normalized
    return ""


def _bootstrap_existing_dirs(paths: List[str]) -> List[str]:
    seen = set()
    matches: List[str] = []
    for candidate in paths:
        normalized = os.path.normpath(candidate)
        if normalized in seen:
            continue
        seen.add(normalized)
        if os.path.isdir(normalized):
            matches.append(normalized)
    return matches


def _bootstrap_configure_linux_qt_runtime() -> None:
    if platform.system().lower() != "linux":
        return

    script_dir = os.path.dirname(os.path.abspath(__file__))
    bundle_dir = getattr(sys, "_MEIPASS", script_dir)
    runtime_dir = script_dir

    argv0 = sys.argv[0] if sys.argv else ""
    if argv0:
        candidate_dir = os.path.dirname(os.path.abspath(argv0))
        if os.path.isdir(candidate_dir):
            runtime_dir = candidate_dir
    elif getattr(sys, "frozen", False):
        candidate_dir = os.path.dirname(os.path.abspath(sys.executable))
        if os.path.isdir(candidate_dir):
            runtime_dir = candidate_dir

    runtime_roots = [bundle_dir, script_dir, runtime_dir, os.getcwd()]
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        runtime_roots.extend([exe_dir, os.path.join(exe_dir, "_internal")])
    runtime_roots.extend([os.path.join(root, "_internal") for root in list(runtime_roots)])

    qt_layouts: List[Tuple[str, str]] = []
    compat_library_roots: List[str] = []
    for root in runtime_roots:
        qt_layouts.extend([
            (
                os.path.join(root, "PyQt5", "Qt5", "plugins"),
                os.path.join(root, "PyQt5", "Qt5", "lib"),
            ),
            (
                os.path.join(root, "PyQt5", "Qt", "plugins"),
                os.path.join(root, "PyQt5", "Qt", "lib"),
            ),
            (
                os.path.join(root, "qt5_plugins"),
                os.path.join(root, "qt5_libs"),
            ),
            (
                os.path.join(root, "_internal", "PyQt5", "Qt5", "plugins"),
                os.path.join(root, "_internal", "PyQt5", "Qt5", "lib"),
            ),
            (
                os.path.join(root, "_internal", "PyQt5", "Qt", "plugins"),
                os.path.join(root, "_internal", "PyQt5", "Qt", "lib"),
            ),
        ])
        compat_library_roots.extend([
            os.path.join(root, "qt-host-libs"),
            os.path.join(root, "_internal", "qt-host-libs"),
        ])

    selected_plugin_root = ""
    selected_qt_library_root = ""
    for plugin_root, library_root in qt_layouts:
        if os.path.isdir(plugin_root) and os.path.isdir(library_root):
            selected_plugin_root = os.path.normpath(plugin_root)
            selected_qt_library_root = os.path.normpath(library_root)
            break

    if not selected_plugin_root:
        selected_plugin_root = _bootstrap_first_existing_dir(
            [plugin_root for plugin_root, _ in qt_layouts]
        )

    if not selected_qt_library_root and selected_plugin_root:
        sibling_library_root = os.path.normpath(
            os.path.join(os.path.dirname(selected_plugin_root), "lib")
        )
        if os.path.isdir(sibling_library_root):
            selected_qt_library_root = sibling_library_root

    allowed_library_roots: List[str] = []
    if selected_qt_library_root and os.path.isdir(selected_qt_library_root):
        allowed_library_roots.append(selected_qt_library_root)
    allowed_library_roots.extend(_bootstrap_existing_dirs(compat_library_roots))

    if allowed_library_roots:
        os.environ["LD_LIBRARY_PATH"] = os.pathsep.join(allowed_library_roots)
    else:
        os.environ.pop("LD_LIBRARY_PATH", None)

    if selected_plugin_root:
        os.environ["QT_PLUGIN_PATH"] = selected_plugin_root
        platform_plugin_dir = os.path.join(selected_plugin_root, "platforms")
        if os.path.isdir(platform_plugin_dir):
            os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"] = platform_plugin_dir

    display = str(os.environ.get("DISPLAY") or "").strip()
    wayland_display = str(os.environ.get("WAYLAND_DISPLAY") or "").strip()
    xdg_session_type = str(os.environ.get("XDG_SESSION_TYPE") or "").strip().lower()

    if display and xdg_session_type != "wayland":
        os.environ.setdefault("QT_QPA_PLATFORM", "xcb")
    elif wayland_display and xdg_session_type == "wayland":
        os.environ.setdefault("QT_QPA_PLATFORM", "wayland")

    os.environ.pop("QT_QPA_PLATFORMTHEME", None)
    os.environ.setdefault("QT_STYLE_OVERRIDE", "Fusion")
    os.environ.setdefault("QT_AUTO_SCREEN_SCALE_FACTOR", "1")
    os.environ.setdefault("QT_X11_NO_MITSHM", "1")
    if str(os.environ.get("BREAKEVEN_QT_DEBUG_PLUGINS") or "").strip() == "1":
        os.environ["QT_DEBUG_PLUGINS"] = "1"


_bootstrap_configure_linux_qt_runtime()


def _enable_early_crash_diagnostics() -> None:
    if str(os.environ.get("BREAKEVEN_DISABLE_FAULTHANDLER") or "").strip() == "1":
        return

    try:
        faulthandler.enable(all_threads=True)
    except Exception as exc:
        print(f"[CRASH] Unable to enable faulthandler: {exc}", file=sys.stderr, flush=True)
        return

    for signal_name in ("SIGUSR1", "SIGUSR2"):
        debug_signal = getattr(signal, signal_name, None)
        if debug_signal is None:
            continue
        try:
            faulthandler.register(debug_signal, all_threads=True, chain=False)
        except Exception:
            continue

    print("[CRASH] faulthandler enabled", file=sys.stderr, flush=True)


_enable_early_crash_diagnostics()

from PyQt5 import QtWidgets, QtGui, QtCore
from PyQt5.QtWidgets import QSystemTrayIcon, QMenu, QApplication

try:
    from packaging import version as pkg_version
except Exception:
    pkg_version = None

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BUNDLE_DIR = getattr(sys, "_MEIPASS", SCRIPT_DIR)


def resolve_launch_dir() -> str:
    argv0 = sys.argv[0] if sys.argv else ""
    if argv0:
        candidate = os.path.dirname(os.path.abspath(argv0))
        if os.path.isdir(candidate):
            return candidate

    if getattr(sys, "frozen", False):
        candidate = os.path.dirname(os.path.abspath(sys.executable))
        if os.path.isdir(candidate):
            return candidate

    return SCRIPT_DIR


RUNTIME_DIR = resolve_launch_dir()


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


def existing_dirs(paths: List[str]) -> List[str]:
    seen = set()
    matches: List[str] = []
    for candidate in paths:
        normalized = os.path.normpath(candidate)
        if normalized in seen:
            continue
        seen.add(normalized)
        if os.path.isdir(normalized):
            matches.append(normalized)
    return matches


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
    "breakevendashboard",
    "breakevendashboard.exe",
]

SLAVE_PROC_TOKENS = [
    "breakeven_slave",
    "breakeven-slave",
    "breakevenslaveservicehost",
    "breakevenslaveservicehost.exe",
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


def prepend_env_path(name: str, candidate_paths: List[str]) -> None:
    existing = [path for path in existing_dirs(candidate_paths) if path]
    if not existing:
        return

    current = [part for part in os.environ.get(name, "").split(os.pathsep) if part]
    merged: List[str] = []
    seen = set()
    for value in existing + current:
        normalized = os.path.normcase(os.path.normpath(value))
        if normalized in seen:
            continue
        seen.add(normalized)
        merged.append(value)
    os.environ[name] = os.pathsep.join(merged)


def set_env_path(name: str, leading_paths: List[str]) -> None:
    preferred = existing_dirs(leading_paths)
    current = [part for part in os.environ.get(name, "").split(os.pathsep) if part]
    merged: List[str] = []
    seen = set()

    for value in preferred + current:
        normalized = os.path.normcase(os.path.normpath(value))
        if normalized in seen:
            continue
        seen.add(normalized)
        merged.append(value)

    if merged:
        os.environ[name] = os.pathsep.join(merged)
    else:
        os.environ.pop(name, None)


def configure_linux_qt_runtime() -> None:
    if get_os_type() != "linux":
        return

    runtime_roots = [BUNDLE_DIR, SCRIPT_DIR, RUNTIME_DIR, os.getcwd()]

    meipass_dir = getattr(sys, "_MEIPASS", None)
    if meipass_dir:
        runtime_roots.insert(0, meipass_dir)

    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        runtime_roots.extend([exe_dir, os.path.join(exe_dir, "_internal")])

    # AppImage and PyInstaller onedir layouts may place Qt under an extra
    # _internal directory, so include both the launch root and that nested root.
    runtime_roots.extend([os.path.join(root, "_internal") for root in list(runtime_roots)])

    qt_layouts: List[Tuple[str, str]] = []
    compat_library_roots: List[str] = []
    for root in runtime_roots:
        qt_layouts.extend([
            (
                os.path.join(root, "PyQt5", "Qt5", "plugins"),
                os.path.join(root, "PyQt5", "Qt5", "lib"),
            ),
            (
                os.path.join(root, "PyQt5", "Qt", "plugins"),
                os.path.join(root, "PyQt5", "Qt", "lib"),
            ),
            (
                os.path.join(root, "qt5_plugins"),
                os.path.join(root, "qt5_libs"),
            ),
            (
                os.path.join(root, "_internal", "PyQt5", "Qt5", "plugins"),
                os.path.join(root, "_internal", "PyQt5", "Qt5", "lib"),
            ),
            (
                os.path.join(root, "_internal", "PyQt5", "Qt", "plugins"),
                os.path.join(root, "_internal", "PyQt5", "Qt", "lib"),
            ),
        ])
        compat_library_roots.extend([
            os.path.join(root, "qt-host-libs"),
            os.path.join(root, "_internal", "qt-host-libs"),
        ])

    selected_plugin_root = ""
    selected_qt_library_root = ""
    for plugin_root, library_root in qt_layouts:
        if os.path.isdir(plugin_root) and os.path.isdir(library_root):
            selected_plugin_root = os.path.normpath(plugin_root)
            selected_qt_library_root = os.path.normpath(library_root)
            break

    if not selected_plugin_root:
        selected_plugin_root = first_existing_path([plugin_root for plugin_root, _ in qt_layouts]) or ""

    if not selected_qt_library_root and selected_plugin_root:
        sibling_library_root = os.path.normpath(
            os.path.join(os.path.dirname(selected_plugin_root), "lib")
        )
        if os.path.isdir(sibling_library_root):
            selected_qt_library_root = sibling_library_root

    valid_compat_library_roots = existing_dirs(compat_library_roots)
    allowed_library_roots: List[str] = []
    if selected_qt_library_root and os.path.isdir(selected_qt_library_root):
        allowed_library_roots.append(selected_qt_library_root)
    allowed_library_roots.extend(valid_compat_library_roots)

    disallowed_runtime_roots = []
    for root in runtime_roots:
        disallowed_runtime_roots.extend([
            root,
            os.path.join(root, "_internal"),
        ])

    inherited_library_path = os.environ.get("LD_LIBRARY_PATH", "")

    if allowed_library_roots:
        os.environ["LD_LIBRARY_PATH"] = os.pathsep.join(allowed_library_roots)
    else:
        os.environ.pop("LD_LIBRARY_PATH", None)

    if selected_plugin_root:
        os.environ["QT_PLUGIN_PATH"] = selected_plugin_root

    platform_plugin_dir = ""
    if selected_plugin_root:
        candidate_platform_dir = os.path.join(selected_plugin_root, "platforms")
        if os.path.isdir(candidate_platform_dir):
            platform_plugin_dir = candidate_platform_dir
    if platform_plugin_dir:
        os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"] = platform_plugin_dir

    display = str(os.environ.get("DISPLAY") or "").strip()
    wayland_display = str(os.environ.get("WAYLAND_DISPLAY") or "").strip()
    xdg_session_type = str(os.environ.get("XDG_SESSION_TYPE") or "").strip().lower()

    if display and xdg_session_type != "wayland":
        os.environ.setdefault("QT_QPA_PLATFORM", "xcb")
    elif wayland_display and xdg_session_type == "wayland":
        os.environ.setdefault("QT_QPA_PLATFORM", "wayland")

    os.environ.pop("QT_QPA_PLATFORMTHEME", None)
    os.environ.setdefault("QT_STYLE_OVERRIDE", "Fusion")
    os.environ.setdefault("QT_AUTO_SCREEN_SCALE_FACTOR", "1")
    os.environ.setdefault("QT_X11_NO_MITSHM", "1")
    if str(os.environ.get("BREAKEVEN_QT_DEBUG_PLUGINS") or "").strip() == "1":
        os.environ["QT_DEBUG_PLUGINS"] = "1"

    log_info(f"[QT] QT_PLUGIN_PATH={os.environ.get('QT_PLUGIN_PATH', '')}")
    log_info(
        f"[QT] QT_QPA_PLATFORM_PLUGIN_PATH={os.environ.get('QT_QPA_PLATFORM_PLUGIN_PATH', '')}"
    )
    log_info(f"[QT] QT_QPA_PLATFORM={os.environ.get('QT_QPA_PLATFORM', '')}")
    log_info(
        f"[QT] QT_QPA_PLATFORMTHEME={os.environ.get('QT_QPA_PLATFORMTHEME', '')}"
    )
    log_info(f"[QT] Selected plugin root={selected_plugin_root}")
    log_info(f"[QT] Selected library root={selected_qt_library_root}")
    log_info(f"[QT] Inherited LD_LIBRARY_PATH={inherited_library_path}")
    log_info(f"[QT] LD_LIBRARY_PATH={os.environ.get('LD_LIBRARY_PATH', '')}")


def _local_x11_socket_available(display: str) -> bool:
    value = str(display or "").strip()
    if not value:
        return False

    if value.startswith("unix/"):
        value = value.split("/", 1)[1]

    if ":" not in value:
        return False

    host_part, display_part = value.rsplit(":", 1)
    if host_part and host_part not in {"localhost", "127.0.0.1", "::1"}:
        return False

    display_number = display_part.split(".", 1)[0]
    if not display_number.isdigit():
        return False

    socket_path = f"/tmp/.X11-unix/X{display_number}"
    if not stat.S_ISSOCK(os.stat(socket_path).st_mode):
        return False

    try:
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(0.2)
        probe.connect(socket_path)
        probe.close()
        return True
    except OSError:
        return False


def _local_wayland_socket_available(runtime_dir: str, wayland_display: str) -> bool:
    runtime_dir = str(runtime_dir or "").strip()
    wayland_display = str(wayland_display or "").strip()
    if not runtime_dir or not wayland_display:
        return False

    socket_path = os.path.join(runtime_dir, wayland_display)
    try:
        return stat.S_ISSOCK(os.stat(socket_path).st_mode)
    except OSError:
        return False


def _session_dbus_available(runtime_dir: str) -> bool:
    dbus_session_address = str(os.environ.get("DBUS_SESSION_BUS_ADDRESS") or "").strip()
    if dbus_session_address:
        return True

    runtime_dir = str(runtime_dir or "").strip()
    if not runtime_dir:
        return False

    bus_path = os.path.join(runtime_dir, "bus")
    try:
        return stat.S_ISSOCK(os.stat(bus_path).st_mode)
    except OSError:
        return False


def linux_graphical_session_available() -> bool:
    if get_os_type() != "linux":
        return True

    if str(os.environ.get("BREAKEVEN_TRAY_FORCE_GUI") or "").strip() == "1":
        return True

    if str(os.environ.get("BREAKEVEN_TRAY_HEADLESS") or "").strip() == "1":
        return False

    display = str(os.environ.get("DISPLAY") or "").strip()
    wayland_display = str(os.environ.get("WAYLAND_DISPLAY") or "").strip()
    xdg_session_type = str(os.environ.get("XDG_SESSION_TYPE") or "").strip().lower()
    xdg_runtime_dir = str(os.environ.get("XDG_RUNTIME_DIR") or "").strip()

    session_bus_available = _session_dbus_available(xdg_runtime_dir)
    if not session_bus_available:
        log_info("[HEADLESS] No session DBus is available for the tray process")

    if wayland_display and xdg_session_type == "wayland":
        wayland_ready = _local_wayland_socket_available(xdg_runtime_dir, wayland_display)
        if not wayland_ready:
            log_info(
                f"[HEADLESS] Wayland socket is unavailable: runtime_dir={xdg_runtime_dir} wayland_display={wayland_display}"
            )
        return wayland_ready and session_bus_available

    if display:
        x11_ready = _local_x11_socket_available(display)
        if not x11_ready:
            log_info(f"[HEADLESS] X11 display socket is unavailable: DISPLAY={display}")
        return x11_ready and session_bus_available

    if xdg_session_type in {"x11", "wayland"}:
        log_info(f"[HEADLESS] Session type {xdg_session_type} is present without a usable display socket")

    return False


def run_headless_service_loop(reason: str) -> int:
    stop_event = threading.Event()

    def _request_stop(signum, _frame):
        log_info(f"[HEADLESS] Signal received: {signum}")
        stop_event.set()

    for sig_name in ("SIGTERM", "SIGINT"):
        sig_value = getattr(signal, sig_name, None)
        if sig_value is None:
            continue
        try:
            signal.signal(sig_value, _request_stop)
        except Exception:
            continue

    log_info(f"[HEADLESS] {reason}")
    print("tray_app headless mode", flush=True)
    print("ready", flush=True)

    while not stop_event.wait(60):
        pass

    log_info("[HEADLESS] Tray headless loop stopped")
    return 0


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
        self._config_path = resolve_client_config_path()
        self._python_command: Optional[str] = None
        log_info(f"Tray script dir: {SCRIPT_DIR}")
        log_info(f"Tray bundle dir: {BUNDLE_DIR}")
        log_info(f"Tray runtime dir: {RUNTIME_DIR}")
        log_info(f"Tray launch argv[0]: {sys.argv[0] if sys.argv else ''}")
        log_info(f"Tray config path: {self._config_path}")
        log_info(f"Platform: {platform.platform()}")

    def load_config(self) -> dict:
        self._config_path = resolve_client_config_path()
        config = read_json(self._config_path)
        if not isinstance(config, dict):
            raise RuntimeError(f"Unable to read client config at {self._config_path}")
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

    def run_command(self, command: List[str], timeout: int = 5) -> Tuple[int, str, str]:
        log_info(f"Executing command (timeout={timeout}s): {' '.join(command[:2])}...")
        try:
            proc = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout,
                shell=False,
            )
            return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()
        except subprocess.TimeoutExpired:
            log_error(f"Command timed out after {timeout}s: {command[0]}")
            return 1, "", f"Command timed out after {timeout}s"

    def _process_contains_tokens(self, proc: psutil.Process, tokens: List[str]) -> bool:
        try:
            info = getattr(proc, "info", {}) or {}
            name = str(info.get("name") or "").lower()
            raw_cmdline = info.get("cmdline")
            if isinstance(raw_cmdline, list):
                cmdline = " ".join(str(part) for part in raw_cmdline).lower()
            else:
                cmdline = str(raw_cmdline or "").lower()
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
        matches: List[psutil.Process] = []
        seen_pids = set()
        for proc in psutil.process_iter(attrs=["name", "cmdline"]):
            if self._is_dashboard_process(proc, launch_target):
                if proc.pid in seen_pids:
                    continue
                seen_pids.add(proc.pid)
                matches.append(proc)
        return matches

    def _is_dashboard_process(self, proc: psutil.Process, launch_target: Optional[str]) -> bool:
        try:
            info = getattr(proc, "info", {}) or {}
            name = str(info.get("name") or "").lower()
            raw_cmdline = info.get("cmdline") or []
            cmdline = " ".join(str(part) for part in raw_cmdline).lower()
            cmdline_parts = [str(part) for part in raw_cmdline]

            compact_name = compact_process_text(name)
            compact_cmdline = compact_process_text(cmdline)
            compact_cmdline_parts = {
                compact_process_text(part)
                for part in cmdline_parts
                if part
            }
            compact_cmdline_basenames = {
                compact_process_text(os.path.basename(part))
                for part in cmdline_parts
                if part
            }

            # Exclude non-dashboard Breakeven processes that previously caused false positives.
            excluded_tokens = [
                "breakevenslave",
                "slaveservicehost",
                "breakevenupdater",
                "taskkillexe",
                "powershellexe",
                "cmdexe",
            ]
            if any(token in compact_name for token in excluded_tokens):
                return False
            if any(token in compact_cmdline for token in excluded_tokens):
                return False

            if launch_target and self._process_matches_path(proc, launch_target):
                return True

            target_tokens = {
                compact_process_text(token)
                for token in DASHBOARD_PROC_TOKENS
                if token
            }
            launch_target_compact = ""
            launch_target_stem_compact = ""
            if launch_target:
                target_name = os.path.basename(launch_target).lower()
                launch_target_compact = compact_process_text(target_name)
                stem, _ext = os.path.splitext(target_name)
                launch_target_stem_compact = compact_process_text(stem)
                if "dashboard" in launch_target_compact:
                    target_tokens.add(launch_target_compact)
                if "dashboard" in launch_target_stem_compact:
                    target_tokens.add(launch_target_stem_compact)

            target_tokens = {token for token in target_tokens if token}

            # Accept exact executable-name matches for known dashboard binaries.
            if compact_name in target_tokens:
                return True

            # Command line matches must be exact argument/basename matches, not substrings.
            if target_tokens & compact_cmdline_parts:
                return True
            if target_tokens & compact_cmdline_basenames:
                return True

            if launch_target_compact and "dashboard" in launch_target_compact:
                if launch_target_compact in compact_cmdline_basenames:
                    return True
            if launch_target_stem_compact and "dashboard" in launch_target_stem_compact:
                if launch_target_stem_compact in compact_cmdline_basenames:
                    return True

            if "breakevendashboard" in compact_cmdline:
                return True
            if "dashboardgui" in compact_cmdline and "breakeven" in compact_cmdline:
                return True

            return False
        except Exception:
            return False

    def find_slave_processes(self) -> List[psutil.Process]:
        matches: List[psutil.Process] = []
        log_info(f"[PROCESS_SCAN] Scanning for slave processes with tokens: {SLAVE_PROC_TOKENS}")
        try:
            for proc in psutil.process_iter(attrs=["name", "cmdline"]):
                if self._process_contains_tokens(proc, SLAVE_PROC_TOKENS):
                    matches.append(proc)
        except Exception as exc:
            log_error(f"[PROCESS_SCAN] Error iterating processes: {exc}")
        log_info(f"[PROCESS_SCAN] Process scan complete. Matches: {len(matches)}")
        return matches

    def _is_runtime_slave_process(self, proc: psutil.Process) -> bool:
        try:
            info = getattr(proc, "info", {}) or {}
            name = str(info.get("name") or "").lower()
            raw_cmdline = info.get("cmdline") or []
            cmdline = " ".join(str(part) for part in raw_cmdline).lower()

            compact_name = compact_process_text(name)
            compact_cmdline = compact_process_text(cmdline)

            # Ignore helper/system tools whose command line can mention slave image names.
            if compact_name in {"taskkillexe", "powershellexe", "pwshexe", "cmdexe"}:
                return False

            # Service host alone is not enough to consider slave fully started.
            if "servicehost" in compact_name:
                return False

            if "breakevenslave" in compact_name:
                return True

            if "breakevenslave" in compact_cmdline and "servicehost" not in compact_cmdline:
                return True

            if "slavepy" in compact_cmdline:
                return True

            return False
        except Exception:
            return False

    def find_runtime_slave_processes(self) -> List[psutil.Process]:
        matches: List[psutil.Process] = []
        try:
            for proc in psutil.process_iter(attrs=["name", "cmdline"]):
                if self._is_runtime_slave_process(proc):
                    matches.append(proc)
        except Exception as exc:
            log_error(f"[SLAVE_RUNTIME] Error iterating processes: {exc}")
        return matches

    def resolve_client_service_dir(self, config: dict) -> Optional[str]:
        install_path = config.get("installPath")
        service_install_path = config.get("serviceInstallPath")
        candidates: List[str] = []
        if service_install_path:
            candidates.extend([
                os.path.join(service_install_path, "client_service"),
                service_install_path,
            ])
        if install_path:
            candidates.extend([
                os.path.join(install_path, "client_service"),
                install_path,
            ])

        seen = set()
        for candidate in candidates:
            normalized = os.path.normpath(candidate)
            if normalized in seen:
                continue
            seen.add(normalized)
            if not os.path.isdir(normalized):
                continue
            slave_candidates = [
                os.path.join(normalized, "Breakeven_Slave.exe"),
                os.path.join(normalized, "Breakeven_Slave.py"),
                os.path.join(normalized, "Breakeven_Slave-x86_64.AppImage"),
            ]
            if any(os.path.exists(path) for path in slave_candidates):
                return normalized

        return os.path.normpath(candidates[0]) if candidates else None

    def resolve_python_interpreter(self) -> Optional[str]:
        if self._python_command:
            return self._python_command
        candidates = ["py", "python", "python3"] if get_os_type() == "windows" else ["python3", "python"]
        for candidate in candidates:
            try:
                subprocess.run(
                    [candidate, "--version"],
                    capture_output=True,
                    text=True,
                    timeout=8,
                    shell=False,
                    check=False,
                )
                self._python_command = candidate
                return candidate
            except Exception:
                continue
        return None

    def resolve_slave_launch_command(self, service_dir: Optional[str]) -> Optional[List[str]]:
        if not service_dir:
            return None

        exe_path = os.path.join(service_dir, "Breakeven_Slave.exe")
        if os.path.exists(exe_path):
            return [exe_path]

        py_path = os.path.join(service_dir, "Breakeven_Slave.py")
        if os.path.exists(py_path):
            python_cmd = self.resolve_python_interpreter()
            if python_cmd:
                return [python_cmd, py_path]

        appimage_path = os.path.join(service_dir, "Breakeven_Slave-x86_64.AppImage")
        if os.path.exists(appimage_path):
            return [appimage_path]

        return None

    def launch_slave_binary_fallback(self, config: dict) -> None:
        service_dir = self.resolve_client_service_dir(config)
        if not service_dir or not os.path.isdir(service_dir):
            raise RuntimeError("Client service directory not found. Check client_config.json paths.")

        launch_cmd = self.resolve_slave_launch_command(service_dir)
        if not launch_cmd:
            raise RuntimeError("No Breakeven_Slave launch target found in service directory.")

        subprocess.Popen(
            launch_cmd,
            cwd=service_dir,
            shell=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if get_os_type() == "windows" else 0,
        )

    def terminate_slave_processes_fallback(self) -> None:
        errors: List[str] = []
        processes = self.find_slave_processes()
        for proc in processes:
            try:
                proc.terminate()
            except Exception as exc:
                errors.append(str(exc))

        if processes:
            try:
                psutil.wait_procs(processes, timeout=3)
            except Exception:
                pass

        for proc in self.find_slave_processes():
            try:
                proc.kill()
            except Exception as exc:
                errors.append(str(exc))

        if get_os_type() == "windows":
            image_names = ["Breakeven_Slave.exe", "BreakEvenSlaveServiceHost.exe"]
            for image_name in image_names:
                try:
                    subprocess.run(
                        ["taskkill", "/IM", image_name, "/T", "/F"],
                        capture_output=True,
                        text=True,
                        timeout=10,
                        shell=False,
                    )
                except Exception as exc:
                    errors.append(str(exc))

        if self.is_slave_process_running():
            raise RuntimeError(errors[0] if errors else "Slave process is still running")

    def stop_slave_with_windows_elevation(self) -> bool:
        if get_os_type() != "windows":
            return False

        service_name = SLAVE_SERVICE_FALLBACK["windows"]
        script_name = f"breakeven_stop_slave_{int(time.time() * 1000)}.ps1"
        script_path = os.path.join(tempfile.gettempdir(), script_name)
        script_body = (
            "$ErrorActionPreference = 'SilentlyContinue'\n"
            f"Stop-Service -Name '{service_name}' -Force\n"
            "Start-Sleep -Milliseconds 800\n"
            "taskkill /IM Breakeven_Slave.exe /T /F | Out-Null\n"
            "taskkill /IM BreakEvenSlaveServiceHost.exe /T /F | Out-Null\n"
            f"$svc = Get-Service -Name '{service_name}' -ErrorAction SilentlyContinue\n"
            "if ($svc -and $svc.Status -ne 'Stopped') { exit 1 }\n"
            "exit 0\n"
        )

        try:
            with open(script_path, "w", encoding="utf-8") as handle:
                handle.write(script_body)

            escaped_path = script_path.replace("'", "''")
            elevate_cmd = (
                "$p = Start-Process -FilePath 'powershell.exe' -Verb RunAs -PassThru -Wait "
                f"-ArgumentList @('-NoLogo','-NoProfile','-ExecutionPolicy','Bypass','-File','{escaped_path}'); "
                "exit $p.ExitCode"
            )

            code, _stdout, stderr = self.run_command(
                ["powershell.exe", "-NoLogo", "-NoProfile", "-Command", elevate_cmd],
                timeout=60,
            )
            if code == 0:
                return True

            log_error(f"[STOP SLAVE] Elevated stop failed (code={code}): {stderr}")
            return False
        except Exception as exc:
            log_error(f"[STOP SLAVE] Elevated stop exception: {exc}")
            return False
        finally:
            try:
                if os.path.exists(script_path):
                    os.remove(script_path)
            except Exception:
                pass

    def start_slave_with_windows_elevation(self) -> bool:
        if get_os_type() != "windows":
            return False

        service_name = SLAVE_SERVICE_FALLBACK["windows"]
        script_name = f"breakeven_start_slave_{int(time.time() * 1000)}.ps1"
        script_path = os.path.join(tempfile.gettempdir(), script_name)
        script_body = (
            "$ErrorActionPreference = 'SilentlyContinue'\n"
            f"Start-Service -Name '{service_name}'\n"
            "Start-Sleep -Milliseconds 1200\n"
            "$procs = @(Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.ProcessName -match 'Breakeven_Slave|BreakEvenSlaveServiceHost' })\n"
            "if ($procs.Count -gt 0) { exit 0 }\n"
            "exit 1\n"
        )

        try:
            with open(script_path, "w", encoding="utf-8") as handle:
                handle.write(script_body)

            escaped_path = script_path.replace("'", "''")
            elevate_cmd = (
                "$p = Start-Process -FilePath 'powershell.exe' -Verb RunAs -PassThru -Wait "
                f"-ArgumentList @('-NoLogo','-NoProfile','-ExecutionPolicy','Bypass','-File','{escaped_path}'); "
                "exit $p.ExitCode"
            )

            code, _stdout, stderr = self.run_command(
                ["powershell.exe", "-NoLogo", "-NoProfile", "-Command", elevate_cmd],
                timeout=60,
            )
            if code == 0:
                return True

            log_error(f"[START SLAVE] Elevated start failed (code={code}): {stderr}")
            return False
        except Exception as exc:
            log_error(f"[START SLAVE] Elevated start exception: {exc}")
            return False
        finally:
            try:
                if os.path.exists(script_path):
                    os.remove(script_path)
            except Exception:
                pass

    def is_dashboard_running(self) -> bool:
        try:
            config = self.load_config()
            launch_target = self.resolve_dashboard_candidate(config.get("installPath", ""))
        except Exception:
            launch_target = None

        if self._dashboard_handle is not None:
            try:
                if self._dashboard_handle.poll() is None:
                    handle_pid = self._dashboard_handle.pid
                    handle_proc = psutil.Process(handle_pid)
                    handle_proc.info = {
                        "name": handle_proc.name(),
                        "cmdline": handle_proc.cmdline(),
                    }
                    if self._is_dashboard_process(handle_proc, launch_target):
                        return True

                    log_info(f"[DASHBOARD] Clearing stale dashboard handle pid={handle_pid}")
                    self._dashboard_handle = None
                else:
                    self._dashboard_handle = None
            except Exception:
                self._dashboard_handle = None

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
        runtime_processes = self.find_runtime_slave_processes()
        all_processes = self.find_slave_processes()
        log_info(
            f"[SLAVE_PROCESS] Found {len(runtime_processes)} runtime slave process(es) "
            f"({len(all_processes)} total related process(es))"
        )
        for proc in runtime_processes:
            try:
                info = getattr(proc, "info", {}) or {}
                name = info.get("name") or "unknown"
                cmdline = info.get("cmdline") or []
                cmdline_text = " ".join(str(part) for part in cmdline)[:100]
                log_info(f"[SLAVE_PROCESS]   - RUNTIME PID {proc.pid}: {name} | {cmdline_text}")
            except Exception:
                pass
        for proc in all_processes:
            try:
                info = getattr(proc, "info", {}) or {}
                name = info.get("name") or "unknown"
                cmdline = info.get("cmdline") or []
                cmdline_text = " ".join(str(part) for part in cmdline)[:100]
                log_info(f"[SLAVE_PROCESS]   - RELATED PID {proc.pid}: {name} | {cmdline_text}")
            except Exception:
                pass
        return bool(runtime_processes)

    def is_slave_running(self) -> bool:
        log_info("[SLAVE_STATUS] Checking slave status via process scan...")
        try:
            process_running = self.is_slave_process_running()
            log_info(f"[SLAVE_STATUS] Slave process detected: {process_running}")
            return process_running
        except Exception as exc:
            log_error(f"[SLAVE_STATUS] Error during slave check: {exc}")
            return False

    def start_slave(self) -> None:
        log_info("[START SLAVE] Attempting to start slave service...")
        config = self.load_config()
        commands = self.resolve_slave_control_commands(config)
        start_cmd = commands.get("start")
        start_errors: List[str] = []
        service_permission_denied = False

        if get_os_type() == "windows":
            log_info("[START SLAVE] Using elevated Windows service start...")
            if not self.start_slave_with_windows_elevation():
                start_errors.append("Failed to start slave service with elevation")

        elif start_cmd:
            try:
                log_info(f"[START SLAVE] Using service control: {start_cmd[0]}")
                code, _stdout, stderr = self.run_command(start_cmd)
                if code != 0:
                    log_error(f"[START SLAVE] Service start failed (code={code}): {stderr}")
                    lowered = (stderr or "").lower()
                    if get_os_type() == "windows" and (
                        "cannot open" in lowered
                        or "access is denied" in lowered
                        or "permission" in lowered
                    ):
                        service_permission_denied = True
                    start_errors.append(stderr or f"Failed to start slave service (code={code})")
                else:
                    log_info("[START SLAVE] Service start command succeeded")
            except Exception as exc:
                log_error(f"[START SLAVE] Service start exception: {exc}")
                start_errors.append(str(exc))

        if service_permission_denied and get_os_type() == "windows" and not self.is_slave_running():
            log_info("[START SLAVE] Retrying service start with elevated PowerShell prompt...")
            self.start_slave_with_windows_elevation()

        if not self.is_slave_running():
            try:
                log_info("[START SLAVE] Service did not start; using binary fallback...")
                self.launch_slave_binary_fallback(config)
                log_info("[START SLAVE] Binary fallback launched")
            except Exception as fallback_exc:
                log_error(f"[START SLAVE] Binary fallback failed: {fallback_exc}")
                start_errors.append(str(fallback_exc))

        for i in range(6):
            if self.is_slave_running():
                log_info("[START SLAVE] ✓ Slave is now RUNNING")
                return
            time.sleep(0.5)

        detail = start_errors[0] if start_errors else "Slave did not start"
        if service_permission_denied and get_os_type() == "windows":
            detail = "Administrator permission is required to start BreakEvenSlave service. Please run tray_app as Administrator and try again."
        log_error(f"[START SLAVE] ✗ Slave failed to start: {detail}")
        raise RuntimeError(detail)

    def stop_slave(self) -> None:
        log_info("[STOP SLAVE] Attempting to stop slave service...")
        config = self.load_config()
        commands = self.resolve_slave_control_commands(config)
        stop_cmd = commands.get("stop")
        errors: List[str] = []
        service_permission_denied = False

        if get_os_type() == "windows":
            log_info("[STOP SLAVE] Using elevated Windows service stop...")
            if not self.stop_slave_with_windows_elevation():
                errors.append("Failed to stop slave service with elevation")

        elif stop_cmd:
            try:
                log_info(f"[STOP SLAVE] Using service control: {stop_cmd[0]}")
                code, _stdout, stderr = self.run_command(stop_cmd)
                if code != 0:
                    log_error(f"[STOP SLAVE] Service stop failed (code={code}): {stderr}")
                    lowered = (stderr or "").lower()
                    if get_os_type() == "windows" and (
                        "cannot open" in lowered
                        or "access is denied" in lowered
                        or "permission" in lowered
                    ):
                        service_permission_denied = True
                    errors.append(stderr or f"Failed to stop slave service (code={code})")
                else:
                    log_info("[STOP SLAVE] Service stop command succeeded")
            except Exception as exc:
                log_error(f"[STOP SLAVE] Service stop exception: {exc}")
                errors.append(str(exc))

        for attempt in range(3):
            try:
                log_info(f"[STOP SLAVE] Force-terminating processes (attempt {attempt+1}/3)...")
                self.terminate_slave_processes_fallback()
            except Exception as exc:
                log_error(f"[STOP SLAVE] Termination failed: {exc}")
                errors.append(str(exc))

            time.sleep(1.0)
            if not self.is_slave_running():
                log_info("[STOP SLAVE] ✓ Slave is now STOPPED")
                return

        if service_permission_denied and get_os_type() == "windows":
            log_info("[STOP SLAVE] Retrying service stop with elevated PowerShell prompt...")
            self.stop_slave_with_windows_elevation()

            for _ in range(8):
                time.sleep(0.5)
                if not self.is_slave_running():
                    log_info("[STOP SLAVE] ✓ Slave stopped after elevated retry")
                    return

        detail = errors[0] if errors else "Slave is still running after stop attempt"
        if service_permission_denied and get_os_type() == "windows":
            detail = "Administrator permission is required to stop BreakEvenSlave service. Please run tray_app as Administrator and try again."
        log_error(f"[STOP SLAVE] ✗ Slave failed to stop: {detail}")
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
        log_info("[SNAPSHOT] Starting state snapshot...")
        
        # Check dashboard - relatively quick
        log_info("[SNAPSHOT] Checking dashboard status...")
        dashboard_running = False
        try:
            dashboard_running = self.is_dashboard_running()
        except Exception as exc:
            log_error(f"[SNAPSHOT] Dashboard check failed: {exc}")
        log_info(f"[SNAPSHOT] Dashboard: {dashboard_running}")
        
        # Check slave - should be very fast now  
        log_info("[SNAPSHOT] Checking slave status...")
        slave_running = False
        try:
            slave_running = self.is_slave_running()
        except Exception as exc:
            log_error(f"[SNAPSHOT] Slave check failed: {exc}")
        log_info(f"[SNAPSHOT] Slave: {slave_running}")
        
        # Update state is fetched separately in background - skip in this snapshot
        # to avoid network timeouts blocking the UI  
        log_info("[SNAPSHOT] Skipping update check in snapshot (fetched separately)")
        update_state = UpdateState(checked_at=time.time())
        
        state = TrayState(
            dashboard_running=dashboard_running,
            slave_running=slave_running,
            update_state=update_state,
        )
        with self._state_lock:
            self._state = state
        log_info(f"[SNAPSHOT] Complete: dashboard={dashboard_running}, slave={slave_running}")
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
        self._status_ready = False
        self.backend = BackendController()
        self.menu = QMenu(parent)

        self.dashboard_action = self.menu.addAction("🖥 Open Dashboard")
        self.dashboard_action.triggered.connect(self.open_dashboard)

        self.slave_action = self.menu.addAction("⏳ Detecting Slave Status...")
        self.slave_action.setEnabled(False)
        self.slave_action.triggered.connect(self.toggle_slave)

        self.update_action = self.menu.addAction("⬇ Check for Updates")
        self.update_action.triggered.connect(self.check_updates)

        self.quit_action = self.menu.addAction("❌ Quit")
        self.quit_action.triggered.connect(self.quit_app)

        self.menu.aboutToShow.connect(self.prepare_menu_state)
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
            self.prepare_menu_state()
            self.contextMenu().popup(QtGui.QCursor.pos())
            self.refresh_state_async()

    def prepare_menu_state(self):
        try:
            self.backend.refresh_dashboard_state()
        except Exception as exc:
            log_error(f"[MENU] Dashboard refresh before show failed: {exc}")
        self.refresh_menu_state()

    def refresh_menu_state(self):
        state = self.backend.get_cached_state()
        self._status_ready = True
        log_info(f"[MENU] Refreshing menu state: dashboard={state.dashboard_running}, slave={state.slave_running}")
        self.dashboard_action.setText(
            "❎ Close Dashboard" if state.dashboard_running else "🖥 Open Dashboard"
        )
        slave_text = "🛑 Stop Slave" if state.slave_running else "▶ Start Slave"
        self.slave_action.setText(slave_text)
        log_info(f"[MENU] Slave button text set to: {slave_text}")
        self.slave_action.setEnabled(True)

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
        log_info("[MENU] Starting async state refresh...")

        def worker():
            try:
                log_info("[WORKER] Running snapshot_state() with 8s timeout...")
                result = [None]
                
                def snapshot_wrapper():
                    try:
                        result[0] = self.backend.snapshot_state()
                    except Exception as exc:
                        log_error(f"[WORKER] snapshot_state exception: {exc}")
                
                snapshot_thread = threading.Thread(target=snapshot_wrapper, daemon=True)
                snapshot_thread.start()
                snapshot_thread.join(timeout=8)
                
                if result[0] is not None:
                    log_info("[WORKER] Snapshot complete, emitting signal...")
                else:
                    log_error("[WORKER] Snapshot timeout or failed")
                self.refresh_ready.emit()
            except Exception as exc:
                log_error(f"Status refresh failed: {exc}")
                self.refresh_ready.emit()
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
        state = self.backend.refresh_dashboard_state()
        log_info(f"[DASHBOARD] Button clicked. Current state: dashboard_running={state.dashboard_running}")
        if state.dashboard_running:
            log_info("[DASHBOARD] Closing dashboard...")
            self.run_dashboard_action(
                self.backend.close_dashboard,
                "Dashboard toggle failed",
                expected_dashboard_running=False,
            )
            return
        log_info("[DASHBOARD] Opening dashboard...")
        self.run_dashboard_action(
            self.backend.open_dashboard,
            "Dashboard toggle failed",
            expected_dashboard_running=True,
        )

    def toggle_slave(self):
        state = self.backend.refresh_local_state()
        log_info(f"[TOGGLE SLAVE] Button clicked. Current state: slave_running={state.slave_running}")
        if state.slave_running:
            log_info("[TOGGLE SLAVE] Initiating STOP operation...")
            self.run_action_async(
                self.backend.stop_slave,
                "Failed to stop slave service",
                post_action_refresh=self.backend.refresh_slave_state,
            )
            return
        log_info("[TOGGLE SLAVE] Initiating START operation...")
        self.run_action_async(
            self.backend.start_slave,
            "Failed to start slave service",
            post_action_refresh=self.backend.refresh_slave_state,
        )

    def check_updates(self):
        log_info("[UPDATES] Button clicked, checking for updates...")
        self.refresh_state_async()
        state = self.backend.get_cached_state().update_state
        if state.error:
            log_error(f"[UPDATES] Check failed: {state.error}")
            self.showMessage(
                "BreakEven Tray",
                f"Update check failed: {state.error}",
                QtWidgets.QSystemTrayIcon.Warning,
                5000,
            )
            return

        if state.up_to_date is True:
            message = f"Client is up to date ({state.local_version})"
            log_info(f"[UPDATES] {message}")
        elif state.up_to_date is False:
            message = f"Update available: {state.latest_version}"
            log_info(f"[UPDATES] {message}")
        else:
            message = "Update status unavailable"
            log_info(f"[UPDATES] {message}")

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
        log_info("[QUIT] Quit button clicked. Shutting down tray_app...")
        self.refresh_timer.stop()
        QApplication.quit()


def main():
    try:
        if get_os_type() == "linux":
            configure_linux_qt_runtime()
            if not linux_graphical_session_available():
                sys.exit(run_headless_service_loop("No graphical Linux session detected; skipping Qt tray UI"))

        log_info("[QT] Creating QApplication")
        app = QApplication(sys.argv)
        log_info("[QT] QApplication created")
        app.setQuitOnLastWindowClosed(False)
        log_info("[QT] Checking system tray availability")
        if not QSystemTrayIcon.isSystemTrayAvailable():
            sys.exit(run_headless_service_loop("System tray is unavailable in this Linux session; skipping Qt tray UI"))
        log_info("[QT] System tray is available")
        icon = QtGui.QIcon(ICON_PATH)
        log_info("[QT] Creating tray icon")
        tray_icon = TrayApp(icon)
        log_info("[QT] Showing tray icon")
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
