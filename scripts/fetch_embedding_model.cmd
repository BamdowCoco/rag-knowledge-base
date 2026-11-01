@echo off
rem Wrapper for fetch_embedding_model.ps1 (ASCII only: cmd parses .cmd with the console code page).
rem Usage:
rem   scripts\fetch_embedding_model.cmd
rem   scripts\fetch_embedding_model.cmd -Dest D:\models\bge-small-zh-v1.5
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0fetch_embedding_model.ps1" %*
exit /b %ERRORLEVEL%
