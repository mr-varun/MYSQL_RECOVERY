@echo off
rem mysql-rescue command line, using the bundled portable Python.
rem   start-cli.cmd recover  "D:\path\to\data"   [--db NAME] [--out DIR] [--servers DIR]
rem   start-cli.cmd force    "D:\path\to\data"   (the server won't start at all)
rem   start-cli.cmd diagnose "D:\path\to\data"
setlocal
set "PY=%~dp0runtime\python64\python.exe"
if not exist "%PY%" set "PY=py -3.12"
%PY% -I "%~dp0app\mysql_rescue.py" %*
endlocal
