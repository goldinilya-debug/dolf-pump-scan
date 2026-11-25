@echo off
REM BingX executor launcher: activates the venv and runs executor.py; restarts it after a crash.
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
if exist ".venv\Scripts\activate.bat" (
    call ".venv\Scripts\activate.bat"
) else if exist "..\.venv\Scripts\activate.bat" (
    call "..\.venv\Scripts\activate.bat"
) else (
    echo [start_bot] venv not found. Create it:  py -m venv .venv  then  .venv\Scripts\pip install -r requirements.txt
    exit /b 1
)
:loop
echo [start_bot] %date% %time% starting executor.py
python executor.py %*
set RC=%ERRORLEVEL%
REM 0 = stopped by hand, 2 = config error (fix .env) -> do not restart
if "%RC%"=="0" goto end
if "%RC%"=="2" (
    echo [start_bot] config error, see logs\executor.log. Not restarting.
    exit /b 2
)
echo [start_bot] executor exited with code %RC%, restarting in 30 s...
timeout /t 30 /nobreak >nul
goto loop
:end
exit /b 0
