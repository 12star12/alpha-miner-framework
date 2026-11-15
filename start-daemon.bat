@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist data mkdir data
echo ==========================================
echo  Alpha Miner - background daemon
echo  Log: data\auto.log
echo  Close this window freely; the daemon keeps running.
echo ==========================================
start "alpha-miner" /min cmd /c "python -u daemon.py >> data\auto.log 2>&1"
echo [OK] started (minimized window)
