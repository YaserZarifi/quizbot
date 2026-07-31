@echo off
REM Builds kankor-bot.exe. Run this from the project root after
REM   pip install -r requirements.txt
REM See README.md for the full first-time setup checklist.

pyinstaller --onefile --add-data "assets;assets" --name kankor-bot src\main.py

echo.
echo Done. The exe is in dist\kankor-bot.exe
echo Copy config.yaml next to it before running.
