@echo off
REM Build KalikotAISW.exe — single-file Windows executable.
REM Run from anywhere; produces dist\KalikotAISW.exe in the kalikot-aisw folder.
setlocal
set HERE=%~dp0
set ROOT=%HERE%..
pushd "%ROOT%"

echo Ensuring build dependencies are installed...
python -m pip install --quiet pyinstaller pystray Pillow || goto :err

python -m PyInstaller ^
    --noconfirm ^
    --clean ^
    --onefile ^
    --windowed ^
    --name KalikotAISW ^
    --icon "app.ico" ^
    --add-data "app.ico;." ^
    --collect-submodules pystray ^
    --hidden-import pystray._win32 ^
    "gui\app.py" || goto :err

echo.
echo Build complete: %ROOT%\dist\KalikotAISW.exe
popd
exit /b 0

:err
echo Build failed.
popd
exit /b 1
