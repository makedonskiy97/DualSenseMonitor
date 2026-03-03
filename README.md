# DualSense Battery Monitor

Cross-platform Python GUI app (PySide6) for monitoring PS5 DualSense battery level and status.

## Features

- Detects DualSense connect/disconnect (USB/Bluetooth).
- Shows:
  - Battery percentage (0-100%).
  - Status: Charging / Discharging / Full.
  - Connection: USB / Bluetooth / Not connected.
- Periodic polling (default: every 5s).
- Console logs for app start, connection changes, battery updates, and errors.
- Linux fallback mode via `/sys/class/power_supply` and optional `dualsensectl` when `hidapi` is unavailable.

## Requirements

- Python 3.11+ recommended
- Windows 10/11 or Linux (Ubuntu/Debian-based)

Install Python dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

## Run

```bash
source .venv/bin/activate
python main.py
```

or explicitly:

```bash
.venv/bin/python main.py
```

## Build EXE on Windows

This project includes a ready-to-use build script for Windows.

### Option 1: one-click build script

On Windows (in `cmd`), run:

```bat
build_windows_exe.bat
```

The script will:

- create `.venv` if needed,
- install runtime + build dependencies,
- build `dist\\DualSenseMonitor.exe` with PyInstaller.

### Option 2: manual commands

```bat
py -3 -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt -r requirements-build-windows.txt
pyinstaller --noconfirm --clean --onefile --console --name DualSenseMonitor --hidden-import hid --collect-all hid --add-binary ".venv\\Lib\\site-packages\\pydualsense\\hidapi.dll;." main.py
```

Output file:

- `dist\\DualSenseMonitor.exe`

## Build Windows EXE from Linux (for GitHub Release)

Direct cross-compilation of PyInstaller Windows binaries on Linux is unreliable.
Recommended flow: trigger a Windows GitHub Actions build from Linux.

This repo includes:

- Workflow: `.github/workflows/release-windows-exe.yml`
- Helper script: `scripts/release_from_linux.sh`

### How to publish from Linux

1. Commit and push your code.
2. Create and push a release tag (for example `v1.0.0`):

```bash
bash scripts/release_from_linux.sh 1.0.0
```

or manually:

```bash
git tag -a v1.0.0 -m "Release v1.0.0"
git push origin v1.0.0
```

3. GitHub Actions runs on `windows-latest`, builds `DualSenseMonitor.exe`, and attaches:

- `dist/DualSenseMonitor.exe`
- `dist/DualSenseMonitor.exe.sha256`

to the corresponding GitHub Release.

## Linux system packages

Qt on Linux may require:

```bash
sudo apt update
sudo apt install -y libxcb-cursor0
```

For full HID mode (instead of fallback), install hidapi runtime:

```bash
sudo apt install -y libhidapi-hidraw0
```

## Linux permissions (optional, for HID access)

If battery info is unavailable due to permissions on `/dev/hidraw*`, add a udev rule:

```bash
sudo tee /etc/udev/rules.d/70-dualsense.rules >/dev/null <<'EOF'
KERNEL=="hidraw*", SUBSYSTEM=="hidraw", ATTRS{idVendor}=="054c", MODE="0666"
EOF
sudo udevadm control --reload-rules
sudo udevadm trigger
```

Then reconnect the controller.

## Notes

- If `hid` cannot load system `hidapi`, the app automatically uses Linux sysfs/`dualsensectl` fallback.
- If you see `PySide6 is not installed`, ensure you are running with the same venv where requirements were installed.
