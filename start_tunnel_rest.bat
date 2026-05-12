@echo off
REM ============================================================
REM  Expone el servidor REST (puerto 8001) via Cloudflare Tunnel
REM ============================================================
REM  Requisito previo: tener server_rest.py corriendo
REM  (ejecuta start_rest_server.bat primero en OTRO CMD).
REM ============================================================

cd /d "%~dp0"

where cloudflared >nul 2>&1
if errorlevel 1 (
    if not exist "cloudflared.exe" (
        echo ERROR: cloudflared no encontrado.
        pause
        exit /b 1
    )
    set CLOUDFLARED=cloudflared.exe
) else (
    set CLOUDFLARED=cloudflared
)

echo.
echo ============================================================
echo  La URL publica la veras abajo.
echo  Para AppCentral / Intelligence Studio usa:
echo    URL:    https://xxx.trycloudflare.com/trends/google
echo    Method: GET
echo    Header: X-API-Key = tu_api_key
echo ============================================================
echo.

%CLOUDFLARED% tunnel --url http://localhost:8001

pause
