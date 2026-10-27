@echo off
rem One-shot progress of the autopcb run: progress bar, steps done, latest router output. Returns to the prompt.
"%~dp0.venv\Scripts\python.exe" "%~dp0tools\autopcb_logs.py" --snapshot
