@echo off
REM Builds the Kankor Quiz Bot app. Run this from the project root after
REM   pip install -r requirements.txt
REM See README.md for the full first-time setup checklist.
REM
REM The result is a FOLDER, dist\kankor-bot\, not a single file. That folder is
REM the app: copy the whole thing to move it somewhere else. Inside it,
REM kankor-bot.exe is what you run.

pyinstaller --noconfirm kankor-bot.spec
if errorlevel 1 goto :failed

if not exist "dist\kankor-bot\config.yaml" copy config.yaml "dist\kankor-bot\config.yaml" >nul

echo.
echo ------------------------------------------------------------------
echo  Done. Run the app with:  dist\kankor-bot\kankor-bot.exe
echo.
echo  Settings live in dist\kankor-bot\config.yaml -- edit that copy,
echo  not the one in the project root.
echo ------------------------------------------------------------------
goto :eof

:failed
echo.
echo Build FAILED. Scroll up for the reason.
exit /b 1
