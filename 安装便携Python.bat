@echo off
setlocal EnableExtensions
chcp 65001 >nul
title SVCVC-API - Embedded Python Setup

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install_runtime.ps1"
set "EXITCODE=%ERRORLEVEL%"

if not "%EXITCODE%"=="0" pause
exit /b %EXITCODE%
