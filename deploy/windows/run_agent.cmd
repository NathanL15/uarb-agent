@echo off
rem Supervisor: keeps `uarb-agent run` alive. Exit code 3 means .env changed and
rem the agent asked to be restarted; anything else is a crash, restarted after a pause.
setlocal
cd /d "%~dp0\..\.."
if not exist logs mkdir logs
:loop
echo [%date% %time%] starting uarb-agent >> logs\supervisor.log
uarb-agent run >> logs\agent.log 2>&1
set code=%errorlevel%
echo [%date% %time%] uarb-agent exited with %code% >> logs\supervisor.log
if "%code%"=="3" goto loop
timeout /t 15 /nobreak > nul
goto loop
