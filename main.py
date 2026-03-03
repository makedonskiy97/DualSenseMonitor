import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
import ctypes.util
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, List, Optional


def _configure_qt_platform_env() -> None:
    if platform.system() != "Linux":
        return
    if os.environ.get("QT_QPA_PLATFORM"):
        return

    # Prefer Wayland when available to avoid xcb-specific runtime deps.
    xdg_session = os.environ.get("XDG_SESSION_TYPE", "").lower()
    if os.environ.get("WAYLAND_DISPLAY") or xdg_session == "wayland":
        os.environ["QT_QPA_PLATFORM"] = "wayland"
        return

    # Headless fallback (useful for remote/CI execution).
    if not os.environ.get("DISPLAY"):
        os.environ["QT_QPA_PLATFORM"] = "offscreen"


_configure_qt_platform_env()


def _configure_windows_dll_search_path() -> None:
    if platform.system() != "Windows":
        return
    if not getattr(sys, "frozen", False):
        return

    # PyInstaller onefile extracts native libs to sys._MEIPASS.
    # `hid` loads hidapi via dynamic loader, so add this location explicitly.
    meipass = getattr(sys, "_MEIPASS", None)
    if not meipass:
        return

    try:
        if hasattr(os, "add_dll_directory"):
            os.add_dll_directory(meipass)
    except Exception:
        pass

    # Fallback for loaders that rely on PATH lookup.
    current_path = os.environ.get("PATH", "")
    if meipass not in current_path.split(os.pathsep):
        os.environ["PATH"] = meipass + os.pathsep + current_path


_configure_windows_dll_search_path()


def _preload_windows_hidapi_dlls() -> None:
    if platform.system() != "Windows":
        return
    if not getattr(sys, "frozen", False):
        return

    meipass = getattr(sys, "_MEIPASS", None)
    if not meipass:
        return

    dll_candidates: List[str] = []
    for root, _, files in os.walk(meipass):
        for file_name in files:
            lower = file_name.lower()
            if lower.endswith(".dll") and (
                "hidapi" in lower or lower in {"hid.dll", "libhid.dll"}
            ):
                dll_candidates.append(os.path.join(root, file_name))

    for dll_path in dll_candidates:
        dll_dir = os.path.dirname(dll_path)
        try:
            if hasattr(os, "add_dll_directory"):
                os.add_dll_directory(dll_dir)
        except Exception:
            pass

        try:
            ctypes.CDLL(dll_path)
        except Exception:
            pass


_preload_windows_hidapi_dlls()

try:
    from PySide6.QtCore import QObject, Qt, Signal
    from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPen, QPixmap
    from PySide6.QtWidgets import (
        QApplication,
        QLabel,
        QMainWindow,
        QMenu,
        QSystemTrayIcon,
        QVBoxLayout,
        QWidget,
    )
except ModuleNotFoundError as exc:
    if exc.name == "PySide6":
        print(
            "PySide6 is not installed in this Python environment.\n"
            "Activate your virtual environment and install dependencies:\n"
            "  source .venv/bin/activate\n"
            "  pip install -r requirements.txt\n"
            "Then run:\n"
            "  .venv/bin/python main.py",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc
    raise

try:
    import hid
except Exception:
    hid = None
    # Retry once for frozen Windows builds after aggressive DLL preload.
    if platform.system() == "Windows" and getattr(sys, "frozen", False):
        try:
            import importlib

            hid = importlib.import_module("hid")
        except Exception:
            hid = None

try:
    import pydualsense as pydualsense_module
except Exception:
    pydualsense_module = None

try:
    import dualsense_controller as dualsense_controller_module
except Exception:
    dualsense_controller_module = None


DUALSENSE_VID = 0x054C
# 0x0CE6 = DualSense, 0x0DF2 = DualSense Edge (common alternative PID).
DUALSENSE_PIDS = {0x0CE6, 0x0DF2}
DUALSENSE_NAME_HINTS = ("dualsense", "wireless controller", "dualsense edge")


@dataclass
class ControllerState:
    connected: bool = False
    battery_percent: Optional[int] = None
    status: str = "Unknown"
    connection: str = "Not connected"
    device_path: Optional[bytes] = None
    error: Optional[str] = None
    updated_at: float = field(default_factory=time.time)


class DualSenseMonitor:
    """
    Polling monitor for DualSense battery + connection state.

    OS-specific behavior:
    - Windows: Uses hidapi feature/input reports for battery data.
    - Linux: Tries Python DualSense libs first, then optional dualsensectl fallback,
      then HID parsing fallback.
    """

    def __init__(self, poll_interval: int = 5) -> None:
        self.poll_interval = max(3, poll_interval)
        self.system = platform.system()
        self._callbacks: List[Callable[[ControllerState], None]] = []
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._last_state = ControllerState()
        self._last_connected = False
        self._last_battery_report: Optional[int] = None

        self._pydualsense_obj = None
        self._linux_lib_failed = False

    def add_callback(self, callback: Callable[[ControllerState], None]) -> None:
        self._callbacks.append(callback)

    def start(self) -> None:
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        self._log("App start")

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
        self._cleanup_linux_lib()

    def _emit(self, state: ControllerState) -> None:
        self._last_state = state
        for callback in self._callbacks:
            try:
                callback(state)
            except Exception as exc:
                self._log(f"Callback error: {exc}")

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            state = self.read_state()
            self._emit(state)
            self._log_events(state)
            self._stop_event.wait(self.poll_interval)

    def _log_events(self, state: ControllerState) -> None:
        if state.connected != self._last_connected:
            if state.connected:
                self._log(f"Controller connected ({state.connection})")
            else:
                self._log("Controller disconnected")
            self._last_connected = state.connected

        if state.connected and state.battery_percent is not None:
            if state.battery_percent != self._last_battery_report:
                self._log(
                    f"Battery update: {state.battery_percent}% | "
                    f"Status={state.status} | Connection={state.connection}"
                )
                self._last_battery_report = state.battery_percent

        if state.error:
            self._log(f"Error: {state.error}")

    def _log(self, message: str) -> None:
        now = datetime.now().strftime("%H:%M:%S")
        print(f"[{now}] {message}", flush=True)

    def read_state(self) -> ControllerState:
        if self.system == "Windows" and hid is None:
            return ControllerState(
                connected=False,
                status="Unknown",
                connection="Not connected",
                error=(
                    "hidapi backend is unavailable in this build, so Windows HID detection is disabled. "
                    "Rebuild EXE with PyInstaller flags: --hidden-import hid --collect-all hid."
                ),
            )

        if self.system == "Linux" and hid is None:
            return self._read_state_linux_without_hid()

        device_infos = self._detect_controllers()
        if not device_infos:
            return ControllerState(
                connected=False,
                status="Unknown",
                connection="Not connected",
                error=None,
            )

        if self.system == "Windows":
            best_unknown_state: Optional[ControllerState] = None
            for device_info in device_infos:
                connection = self._infer_connection_type(device_info)
                device_path = device_info.get("path")
                try:
                    battery_percent, status = self._read_battery_windows_hid(device_path)
                    current_state = ControllerState(
                        connected=True,
                        battery_percent=battery_percent,
                        status=status,
                        connection=connection,
                        device_path=device_path,
                        error=None,
                    )
                    if battery_percent is not None:
                        return current_state
                    if best_unknown_state is None:
                        best_unknown_state = current_state
                except PermissionError:
                    if best_unknown_state is None:
                        best_unknown_state = ControllerState(
                            connected=True,
                            battery_percent=None,
                            status="Unknown",
                            connection=connection,
                            device_path=device_path,
                            error="HID permission denied.",
                        )
                except Exception as exc:
                    if best_unknown_state is None:
                        best_unknown_state = ControllerState(
                            connected=True,
                            battery_percent=None,
                            status="Unknown",
                            connection=connection,
                            device_path=device_path,
                            error=f"Failed reading battery info: {exc}",
                        )

            if best_unknown_state is not None:
                return best_unknown_state
            return ControllerState(
                connected=True,
                battery_percent=None,
                status="Unknown",
                connection="Unknown",
                error="DualSense detected, but battery report is unavailable on current HID interface.",
            )

        device_info = device_infos[0]

        connection = self._infer_connection_type(device_info)
        device_path = device_info.get("path")

        try:
            if self.system == "Windows":
                battery_percent, status = self._read_battery_windows_hid(device_path)
            elif self.system == "Linux":
                battery_percent, status = self._read_battery_linux(device_path)
            else:
                battery_percent, status = self._read_battery_generic_hid(device_path)

            return ControllerState(
                connected=True,
                battery_percent=battery_percent,
                status=status,
                connection=connection,
                device_path=device_path,
                error=None,
            )
        except PermissionError:
            return ControllerState(
                connected=True,
                battery_percent=None,
                status="Unknown",
                connection=connection,
                device_path=device_path,
                error="HID permission denied. On Linux, check udev permissions for /dev/hidraw*.",
            )
        except Exception as exc:
            return ControllerState(
                connected=True,
                battery_percent=None,
                status="Unknown",
                connection=connection,
                device_path=device_path,
                error=f"Failed reading battery info: {exc}",
            )

    def _read_state_linux_without_hid(self) -> ControllerState:
        # Linux fallback when python `hid` cannot load hidapi shared libraries.
        # This path uses kernel power_supply info (works for many BT/USB gamepads)
        # and optional dualsensectl parsing.
        sysfs_state = self._read_linux_sysfs_battery_state()
        if sysfs_state is not None:
            return sysfs_state

        from_ctl = self._read_battery_linux_dualsensectl()
        if from_ctl is not None:
            battery_percent, status = from_ctl
            return ControllerState(
                connected=True,
                battery_percent=battery_percent,
                status=status,
                connection="Unknown",
                error="Using dualsensectl fallback because hidapi is unavailable.",
            )

        return ControllerState(
            connected=False,
            battery_percent=None,
            status="Unknown",
            connection="Not connected",
            error=(
                "hidapi is unavailable in this Python environment, so HID detection is disabled. "
                "Install system package `libhidapi-hidraw0` (or equivalent) for full support."
            ),
        )

    def _read_linux_sysfs_battery_state(self) -> Optional[ControllerState]:
        power_supply_root = "/sys/class/power_supply"
        if not os.path.isdir(power_supply_root):
            return None

        try:
            entries = [
                name
                for name in os.listdir(power_supply_root)
                if self._looks_like_dualsense_power_supply(name)
            ]
        except Exception:
            return None

        if not entries:
            return None

        # Pick the first candidate; usually there is only one connected controller.
        entry = sorted(entries)[0]
        base = os.path.join(power_supply_root, entry)

        present = self._read_text_file(os.path.join(base, "present"))
        if present is not None and present.strip() == "0":
            return None

        capacity_raw = self._read_text_file(os.path.join(base, "capacity"))
        status_raw = self._read_text_file(os.path.join(base, "status"))

        battery_percent: Optional[int] = None
        if capacity_raw is not None:
            try:
                battery_percent = max(0, min(100, int(capacity_raw.strip())))
            except ValueError:
                battery_percent = None

        status = "Unknown"
        if status_raw:
            mapped = status_raw.strip().lower()
            if mapped.startswith("charg"):
                status = "Charging"
            elif mapped.startswith("discharg"):
                status = "Discharging"
            elif mapped.startswith("full"):
                status = "Full"

        connection = self._infer_connection_from_sysfs_name(entry)
        return ControllerState(
            connected=True,
            battery_percent=battery_percent,
            status=status,
            connection=connection,
            error="Using Linux sysfs fallback because hidapi is unavailable.",
        )

    @staticmethod
    def _read_text_file(path: str) -> Optional[str]:
        try:
            with open(path, "r", encoding="utf-8") as file:
                return file.read().strip()
        except Exception:
            return None

    @staticmethod
    def _looks_like_dualsense_power_supply(name: str) -> bool:
        lower = name.lower()
        # Common names seen on Linux kernels for PlayStation controller batteries.
        patterns = (
            "ps-controller-battery",
            "sony_controller_battery",
            "dualsense",
            "playstation",
        )
        return any(pattern in lower for pattern in patterns)

    @staticmethod
    def _infer_connection_from_sysfs_name(name: str) -> str:
        # Names containing MAC-like suffix are typically Bluetooth devices.
        if re.search(r"[0-9a-f]{2}(:[0-9a-f]{2}){5}", name.lower()):
            return "Bluetooth"
        return "USB"

    def _detect_controller(self) -> Optional[dict]:
        controllers = self._detect_controllers()
        if controllers:
            return controllers[0]
        return None

    def _detect_controllers(self) -> List[dict]:
        if hid is None:
            return []

        devices: List[dict] = []

        try:
            devices.extend(hid.enumerate(DUALSENSE_VID, 0))
        except Exception:
            pass

        try:
            all_devices = hid.enumerate()
            for info in all_devices:
                if self._is_dualsense_device(info):
                    devices.append(info)
        except Exception:
            pass

        dedup_by_path = {}
        for info in devices:
            dedup_key = str(info.get("path", ""))
            dedup_by_path[dedup_key] = info

        candidates = [
            info for info in dedup_by_path.values() if self._is_dualsense_device(info)
        ]
        candidates.sort(key=self._dualsense_score, reverse=True)
        return candidates

    def _is_dualsense_device(self, info: dict) -> bool:
        pid = info.get("product_id")
        vid = info.get("vendor_id")
        path = str(info.get("path", "")).lower()
        product = str(info.get("product_string", "")).lower()
        manufacturer = str(info.get("manufacturer_string", "")).lower()

        if vid == DUALSENSE_VID and pid in DUALSENSE_PIDS:
            return True

        if "vid_054c" in path and any(
            f"pid_{known_pid:04x}" in path for known_pid in DUALSENSE_PIDS
        ):
            return True

        if any(name_hint in product for name_hint in DUALSENSE_NAME_HINTS):
            return vid == DUALSENSE_VID or "sony" in manufacturer

        return False

    def _dualsense_score(self, info: dict) -> int:
        score = 0
        pid = info.get("product_id")
        vid = info.get("vendor_id")
        path = str(info.get("path", "")).lower()
        product = str(info.get("product_string", "")).lower()

        if pid in DUALSENSE_PIDS:
            score += 100
        if vid == DUALSENSE_VID:
            score += 40
        if "vid_054c" in path and any(
            f"pid_{known_pid:04x}" in path for known_pid in DUALSENSE_PIDS
        ):
            score += 60
        if any(name_hint in product for name_hint in DUALSENSE_NAME_HINTS):
            score += 30

        interface_number = info.get("interface_number")
        if isinstance(interface_number, int):
            score += max(0, 5 - abs(interface_number))

        return score

    def _infer_connection_type(self, device_info: dict) -> str:
        path = str(device_info.get("path", "")).lower()

        # Linux hidapi commonly exposes bus_type: 0x03 USB, 0x05 Bluetooth
        bus_type = device_info.get("bus_type")
        if bus_type == 0x03:
            return "USB"
        if bus_type == 0x05:
            return "Bluetooth"

        # Heuristics for Windows/macOS path strings.
        if "bth" in path or "bluetooth" in path:
            return "Bluetooth"
        if "usb" in path or "hid#vid" in path:
            return "USB"

        return "Unknown"

    def _read_battery_windows_hid(self, device_path: bytes) -> tuple[Optional[int], str]:
        # Windows path: query HID feature reports first, then fallback to input report.
        battery_percent, status = self._read_battery_generic_hid(device_path)
        if battery_percent is not None:
            return battery_percent, status
        return None, "Unknown"

    def _read_battery_linux(self, device_path: bytes) -> tuple[Optional[int], str]:
        # Linux strategy:
        # 1) Try python libs (pydualsense/dualsense-controller) if installed.
        # 2) Fallback to external `dualsensectl battery` if available.
        # 3) Fallback to HID report parsing.

        if not self._linux_lib_failed:
            from_lib = self._read_battery_linux_library()
            if from_lib is not None:
                return from_lib

        from_ctl = self._read_battery_linux_dualsensectl()
        if from_ctl is not None:
            return from_ctl

        return self._read_battery_generic_hid(device_path)

    def _read_battery_linux_library(self) -> Optional[tuple[Optional[int], str]]:
        try:
            if pydualsense_module is not None:
                if self._pydualsense_obj is None:
                    # pydualsense backend (if available).
                    if hasattr(pydualsense_module, "pydualsense"):
                        self._pydualsense_obj = pydualsense_module.pydualsense()
                        self._pydualsense_obj.init()
                if self._pydualsense_obj is not None:
                    battery = getattr(self._pydualsense_obj, "battery", None)
                    if battery is not None:
                        if isinstance(battery, int):
                            return max(0, min(100, battery)), "Unknown"
                        if isinstance(battery, dict):
                            pct = battery.get("percent")
                            stat = battery.get("status", "Unknown")
                            if pct is not None:
                                return max(0, min(100, int(pct))), str(stat)

            if dualsense_controller_module is not None:
                # Best-effort adapter: API differs across releases.
                controller_cls = getattr(
                    dualsense_controller_module, "DualSenseController", None
                )
                if controller_cls is not None:
                    return None

            self._linux_lib_failed = True
            return None
        except Exception as exc:
            self._linux_lib_failed = True
            self._log(f"Linux library backend unavailable: {exc}")
            return None

    def _cleanup_linux_lib(self) -> None:
        if self._pydualsense_obj is not None:
            try:
                close_fn = getattr(self._pydualsense_obj, "close", None)
                if callable(close_fn):
                    close_fn()
            except Exception:
                pass

    def _read_battery_linux_dualsensectl(self) -> Optional[tuple[Optional[int], str]]:
        if shutil.which("dualsensectl") is None:
            return None

        try:
            completed = subprocess.run(
                ["dualsensectl", "battery"],
                check=False,
                capture_output=True,
                text=True,
                timeout=2,
            )
            output = (completed.stdout or "") + "\n" + (completed.stderr or "")
            output = output.strip()
            if not output:
                return None

            # Typical examples parsed leniently:
            # "Battery: 80% (Discharging)"
            # "80% charging"
            percent_match = re.search(r"(\d{1,3})\s*%", output)
            status = "Unknown"
            lower = output.lower()
            if "charg" in lower:
                status = "Charging"
            elif "full" in lower:
                status = "Full"
            elif "discharg" in lower or "drain" in lower:
                status = "Discharging"

            if percent_match:
                pct = int(percent_match.group(1))
                return max(0, min(100, pct)), status
            return None, status
        except Exception:
            return None

    def _read_battery_generic_hid(self, device_path: bytes) -> tuple[Optional[int], str]:
        if hid is None:
            return None, "Unknown"

        dev = hid.device()
        try:
            dev.open_path(device_path)
            dev.set_nonblocking(1)

            # USB/BT feature report candidates. Battery byte location can vary.
            for report_id, size in [(0x20, 64), (0x05, 64), (0x09, 64), (0x31, 78)]:
                try:
                    report = dev.get_feature_report(report_id, size)
                    parsed = self._parse_dualsense_battery_from_report(report)
                    if parsed[0] is not None:
                        return parsed
                except OSError:
                    continue
                except Exception:
                    continue

            # Non-blocking input report fallback.
            for _ in range(8):
                data = dev.read(78)
                if data:
                    parsed = self._parse_dualsense_battery_from_report(data)
                    if parsed[0] is not None:
                        return parsed
                time.sleep(0.03)

            return None, "Unknown"
        except OSError as exc:
            # Common Linux case when user lacks hidraw permissions.
            message = str(exc).lower()
            if "permission" in message or "denied" in message:
                raise PermissionError(str(exc)) from exc
            raise
        finally:
            try:
                dev.close()
            except Exception:
                pass

    def _parse_dualsense_battery_from_report(
        self, report: List[int] | bytes
    ) -> tuple[Optional[int], str]:
        if not report:
            return None, "Unknown"

        values = list(report)

        # DualSense commonly exposes battery in byte 53 of extended reports.
        # lower nibble: level (0-10), upper nibble: charge state flags.
        if len(values) > 53:
            packed = values[53]
            level = packed & 0x0F
            state_bits = (packed >> 4) & 0x0F
            if 0 <= level <= 10:
                percent = level * 10
                status = self._map_charge_state(state_bits, percent)
                return percent, status

        # Loose fallback: look for plausible percent-like byte and infer status unknown.
        for candidate in values:
            if 0 <= candidate <= 100:
                return candidate, "Unknown"

        return None, "Unknown"

    @staticmethod
    def _map_charge_state(state_bits: int, percent: int) -> str:
        # Best-effort mapping; values can vary by transport/report mode.
        if percent >= 100:
            return "Full"
        if state_bits in {0x1, 0x2, 0xA, 0xB}:
            return "Charging"
        if state_bits in {0x0, 0x3, 0x4, 0x5}:
            return "Discharging"
        return "Unknown"


class BatteryIconLabel(QLabel):
    def __init__(self) -> None:
        super().__init__()
        self.setFixedSize(160, 80)
        self.update_icon(None, "Unknown")

    def update_icon(self, percent: Optional[int], status: str) -> None:
        pixmap = QPixmap(self.size())
        pixmap.fill(Qt.transparent)

        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)

        x, y, width, height = 10, 15, 120, 50
        terminal_w = 10

        border_pen = QPen(QColor("#d0d0d0"), 3)
        painter.setPen(border_pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(x, y, width, height, 6, 6)
        painter.drawRect(x + width, y + 16, terminal_w, 18)

        if percent is None:
            fill_ratio = 0.0
            color = QColor("#808080")
        else:
            fill_ratio = max(0.0, min(1.0, percent / 100.0))
            if percent >= 60:
                color = QColor("#2ecc71")
            elif percent >= 30:
                color = QColor("#f39c12")
            else:
                color = QColor("#e74c3c")

        fill_w = int((width - 6) * fill_ratio)
        if fill_w > 0:
            painter.setBrush(color)
            painter.setPen(Qt.NoPen)
            painter.drawRoundedRect(x + 3, y + 3, fill_w, height - 6, 4, 4)

        if status == "Charging":
            painter.setPen(QPen(QColor("#ffffff"), 2))
            bolt = "⚡"
            painter.drawText(x + width // 2 - 10, y + height // 2 + 8, bolt)

        painter.end()
        self.setPixmap(pixmap)


class MainWindow(QMainWindow):
    state_received = Signal(object)

    def __init__(self, monitor: DualSenseMonitor) -> None:
        super().__init__()
        self.monitor = monitor
        self.setWindowTitle("DualSense Battery Monitor")
        self.setMinimumSize(420, 280)

        self._setup_ui()
        self._setup_tray()

        self.state_received.connect(self._apply_state)
        self.monitor.add_callback(self._on_monitor_update)

        self._apply_waiting_state()

    def _setup_ui(self) -> None:
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(14)

        self.icon_label = BatteryIconLabel()
        self.icon_label.setAlignment(Qt.AlignCenter)

        self.big_percent = QLabel("--%")
        self.big_percent.setAlignment(Qt.AlignCenter)
        self.big_percent.setStyleSheet("font-size: 52px; font-weight: 700;")

        self.connection_label = QLabel("Connection: Not connected")
        self.connection_label.setStyleSheet("font-size: 16px;")

        self.status_label = QLabel("Status: Unknown")
        self.status_label.setStyleSheet("font-size: 16px;")

        self.message_label = QLabel("Waiting for DualSense controller…")
        self.message_label.setStyleSheet("font-size: 14px; color: #bbbbbb;")

        layout.addWidget(self.icon_label, alignment=Qt.AlignCenter)
        layout.addWidget(self.big_percent)
        layout.addWidget(self.connection_label)
        layout.addWidget(self.status_label)
        layout.addWidget(self.message_label)

        self.setCentralWidget(central)

    def _setup_tray(self) -> None:
        self.tray_icon: Optional[QSystemTrayIcon] = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return

        self.tray_icon = QSystemTrayIcon(self)
        self.tray_icon.setIcon(self._make_tray_icon(None))

        menu = QMenu()
        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(QApplication.instance().quit)
        menu.addAction(quit_action)

        self.tray_icon.setContextMenu(menu)
        self.tray_icon.setToolTip("DualSense Battery Monitor")
        self.tray_icon.show()

    def _on_monitor_update(self, state: ControllerState) -> None:
        self.state_received.emit(state)

    def _apply_waiting_state(self) -> None:
        self.big_percent.setText("--%")
        self.icon_label.update_icon(None, "Unknown")
        self.connection_label.setText("Connection: Not connected")
        self.status_label.setText("Status: Unknown")
        self.message_label.setText("Waiting for DualSense controller…")
        if self.tray_icon:
            self.tray_icon.setIcon(self._make_tray_icon(None))

    def _apply_state(self, state: ControllerState) -> None:
        if not state.connected:
            self._apply_waiting_state()
            if state.error:
                self.message_label.setText(state.error)
            return

        if state.battery_percent is None:
            self.big_percent.setText("N/A")
            self.message_label.setText(
                state.error
                or "Controller connected, but battery info is currently unavailable."
            )
        else:
            self.big_percent.setText(f"{state.battery_percent}%")
            self.message_label.setText("Controller connected")

        self.icon_label.update_icon(state.battery_percent, state.status)
        self.connection_label.setText(f"Connection: {state.connection}")
        self.status_label.setText(f"Status: {state.status}")

        if self.tray_icon:
            self.tray_icon.setIcon(self._make_tray_icon(state.battery_percent))
            tip_pct = "N/A" if state.battery_percent is None else f"{state.battery_percent}%"
            self.tray_icon.setToolTip(
                f"DualSense Battery: {tip_pct} | {state.status} | {state.connection}"
            )

    def _make_tray_icon(self, percent: Optional[int]) -> QIcon:
        pixmap = QPixmap(24, 24)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)

        painter.setPen(QPen(QColor("#d0d0d0"), 2))
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(2, 6, 17, 12, 3, 3)
        painter.drawRect(19, 10, 3, 4)

        if percent is None:
            color = QColor("#808080")
            fill_ratio = 0
        else:
            if percent >= 60:
                color = QColor("#2ecc71")
            elif percent >= 30:
                color = QColor("#f39c12")
            else:
                color = QColor("#e74c3c")
            fill_ratio = max(0.0, min(1.0, percent / 100.0))

        fill_width = int(13 * fill_ratio)
        painter.setPen(Qt.NoPen)
        painter.setBrush(color)
        if fill_width > 0:
            painter.drawRoundedRect(4, 8, fill_width, 8, 2, 2)

        painter.end()
        return QIcon(pixmap)

    def closeEvent(self, event) -> None:
        self.monitor.stop()
        if self.tray_icon:
            self.tray_icon.hide()
        super().closeEvent(event)


def main() -> None:
    if platform.system() == "Linux":
        qpa_platform = os.environ.get("QT_QPA_PLATFORM", "")
        if qpa_platform in {"", "xcb"}:
            xcb_cursor = ctypes.util.find_library("xcb-cursor")
            if xcb_cursor is None:
                print(
                    "Qt xcb backend requires system package libxcb-cursor0 on Ubuntu/Debian.\n"
                    "Install it with:\n"
                    "  sudo apt update && sudo apt install -y libxcb-cursor0\n"
                    "Then run:\n"
                    "  .venv/bin/python main.py",
                    file=sys.stderr,
                )
                return

    app = QApplication([])

    monitor = DualSenseMonitor(poll_interval=5)
    window = MainWindow(monitor)
    window.show()

    monitor.start()

    app.aboutToQuit.connect(monitor.stop)
    app.exec()


if __name__ == "__main__":
    main()
