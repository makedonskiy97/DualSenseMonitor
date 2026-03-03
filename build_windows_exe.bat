@echo off
setlocal

REM Build script for Windows: creates a local venv and packages the app as an EXE.

if not exist .venv\Scripts\python.exe (
    echo [INFO] Creating virtual environment...
    py -3 -m venv .venv
)

call .venv\Scripts\activate.bat
if errorlevel 1 (
    echo [ERROR] Failed to activate virtual environment.
    exit /b 1
)

echo [INFO] Upgrading pip...
python -m pip install --upgrade pip
if errorlevel 1 exit /b 1

echo [INFO] Installing runtime and build dependencies...
pip install -r requirements.txt -r requirements-build-windows.txt
if errorlevel 1 exit /b 1

echo [INFO] Building EXE with PyInstaller...
pyinstaller --noconfirm --clean --onefile --console --name DualSenseMonitor --hidden-import hid --collect-binaries hid main.py
if errorlevel 1 exit /b 1

echo.
echo [OK] Build completed.
echo [OK] EXE location: dist\DualSenseMonitor.exe

echo.
echo Press any key to exit...
pause >nul

endlocal
