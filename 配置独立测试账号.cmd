@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" goto missing
".venv\Scripts\python.exe" -m research_workbench setup
pause
exit /b
:missing
echo Install the isolated .venv first. See RESEARCH_README.md.
pause
exit /b 1
