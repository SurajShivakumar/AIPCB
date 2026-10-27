@echo off
rem Live progress of the autopcb board run (progress bar, finished steps, router output).
rem   autopcb_logs            watch the current / last run (stays open; Ctrl+C to close)
rem   autopcb_logs --once     close when the current run ends
rem   autopcb_logs --log      print the full log
"%~dp0.venv\Scripts\python.exe" "%~dp0tools\autopcb_logs.py" %*
