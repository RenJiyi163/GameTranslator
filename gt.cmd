@echo off
REM game-translator launcher. Keep this file ASCII-only: cmd.exe decodes .bat
REM as CP936 and UTF-8 Chinese comments turn into garbage.
REM
REM   gt.cmd run --profile mygame
REM   gt.cmd run --profile mygame --attach
REM   gt.cmd tm build --profile mygame
REM   gt.cmd doctor --profile mygame

setlocal
set PYEXE=C:\Python314\python.exe
if not exist "%PYEXE%" set PYEXE=python
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
"%PYEXE%" -m gt %*
endlocal
