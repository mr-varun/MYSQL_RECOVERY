@echo off
rem mysql-rescue: double-click to open the recovery window.
rem Portable: runtime\python64 is the tool's own Python, so the PC needs no Python installed.
rem Server builds: the servers\ folder next to this file, plus the folders in settings.json ("server_dirs").
cd /d "%~dp0"

if exist "%~dp0runtime\python64\pythonw.exe" (
  start "" "%~dp0runtime\python64\pythonw.exe" -I "%~dp0app\gui.py"
  goto :eof
)

rem No bundled runtime: an installed Python 3 with tkinter.
where pyw >nul 2>nul && (
  pyw -3.12 -c "import tkinter" >nul 2>nul && ( start "" pyw -3.12 "%~dp0app\gui.py" & goto :eof )
  pyw -3 -c "import tkinter" >nul 2>nul && ( start "" pyw -3 "%~dp0app\gui.py" & goto :eof )
)
where pythonw >nul 2>nul && ( start "" pythonw "%~dp0app\gui.py" & goto :eof )

echo No Python found. Copy a Python 3.12 install into runtime\python64 (see README.md).
pause
