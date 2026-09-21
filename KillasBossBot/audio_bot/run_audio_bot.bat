@echo off
rem Launch KILLA'S BOSS. Uses the local .venv if you created one (see README),
rem otherwise the `python` on PATH.
if exist "%~dp0.venv\Scripts\python.exe" (
    "%~dp0.venv\Scripts\python.exe" "%~dp0audio_bot.py"
) else (
    python "%~dp0audio_bot.py"
)
