@echo off
cd /d "%~dp0"
python -X utf8 -m p1006 serve --install-queue
pause
