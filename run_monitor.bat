@echo off
REM Run the Bull Put Spread monitor with .env loaded. Used by Task Scheduler.
REM Edit the path below if your project is elsewhere.
cd /d "C:\Users\twluf\BullPutSpreadAnalyzer\bull-put-spread-analyzer"
powershell -ExecutionPolicy Bypass -File "C:\Users\twluf\BullPutSpreadAnalyzer\bull-put-spread-analyzer\run_monitor.ps1"
