@echo off
cd /d "%~dp0.."
.venv\Scripts\hl-agent.exe web --port 8080
