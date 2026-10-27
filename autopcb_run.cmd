@echo off
rem Build + route the V2 ESC board end to end, with the live progress dashboard in this window.
rem   autopcb_run [workspace\esc_v2] [--passes 100] [--skip-build]
"%~dp0.venv\Scripts\python.exe" -u "%~dp0tools\autopcb.py" %*
