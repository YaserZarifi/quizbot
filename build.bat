@echo off
REM Builds the Kankor Quiz Bot and installs it into app\.
REM Run from the project root after: pip install -r requirements.txt
REM
REM Two folders, and the difference matters:
REM
REM   dist\kankor-bot\   throwaway build output. PyInstaller DELETES this whole
REM                      folder at the start of every build.
REM   app\               the live app. Your database, Telegram login, logs and
REM                      downloaded question pictures live here.
REM
REM That is why the build never runs in app\: a rebuild would wipe your data
REM with it. This script copies the fresh program files over app\ and leaves
REM everything you own untouched.

REM Use the project's own virtual environment when there is one, so the build
REM works whether or not you remembered to activate it first.
set "PYI=pyinstaller"
if exist ".venv\Scripts\pyinstaller.exe" set "PYI=.venv\Scripts\pyinstaller.exe"

echo Building...
"%PYI%" --noconfirm kankor-bot.spec
if errorlevel 1 goto :failed

echo.
echo Installing into app\ ...

REM /XF and /XD protect the live data. No /PURGE, so nothing in app\ is deleted.
robocopy "dist\kankor-bot" "app" /E /NFL /NDL /NJH /NJS /NP ^
    /XF config.yaml *.db *.db-journal *.session *.session-journal ^
    /XD logs question_images output_images
if errorlevel 8 goto :failed

if not exist "app\config.yaml" copy config.yaml "app\config.yaml" >nul

echo.
echo ------------------------------------------------------------------
echo  Done. Run the app with:  app\kankor-bot.exe
echo.
echo  Settings: app\config.yaml
echo  Your data (database, login, logs) stays in app\ across rebuilds.
echo ------------------------------------------------------------------
goto :eof

:failed
echo.
echo Build FAILED. Scroll up for the reason.
exit /b 1
