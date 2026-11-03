@echo off
rem Build DST Launcher thanh 1 file exe (chay tren Windows, can Python 3.11/3.12 64-bit).
rem Ket qua: dist\DSTLauncher.exe
cd /d "%~dp0"

if not exist .venv-desktop (
  py -3 -m venv .venv-desktop || python -m venv .venv-desktop || goto :error
)
call .venv-desktop\Scripts\activate.bat || goto :error
python -m pip install --upgrade pip || goto :error
pip install -r requirements-desktop.txt || goto :error

pyinstaller --noconfirm --clean --onefile --windowed --name DSTLauncher ^
  --collect-all lupa --hidden-import remote_link ^
  dst_desktop.py || goto :error

echo.
echo Xong: %~dp0dist\DSTLauncher.exe
pause
exit /b 0

:error
echo Build loi.
pause
exit /b 1
