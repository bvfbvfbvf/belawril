@echo off
REM Belawril - Build Windows Executable
REM Run this script on Windows with Python and PyInstaller installed.

echo Installing dependencies...
pip install pyinstaller

echo Building Belawril standalone executable...
pyinstaller --noconfirm --onedir --windowed --name "Belawril" ^
    --add-data "repeat_video.py;." ^
    --add-data "simulator.py;." ^
    --add-data "belawril_engine.py;." ^
    belawril_app.py

if %ERRORLEVEL% EQU 0 (
    echo.
    echo Build completed successfully!
    echo Output executable folder: dist\Belawril\Belawril.exe
) else (
    echo.
    echo Build failed with error code %ERRORLEVEL%.
)
pause
