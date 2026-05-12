@echo off
REM ============================================================
REM  Arranca el servidor REST API (localhost:8001)
REM ============================================================
REM  Para Intelligence Studio de AppCentral, Zapier, n8n, etc.
REM  Despues, abre otro CMD y ejecuta start_tunnel_rest.bat
REM  para exponerlo por internet.
REM ============================================================

cd /d "%~dp0"

echo.
echo [1/2] Activando entorno virtual...
call .venv\Scripts\activate.bat
if errorlevel 1 (
    echo ERROR: no se pudo activar el venv.
    pause
    exit /b 1
)

echo [2/2] Arrancando server_rest.py...
echo.
echo ============================================================
echo  Servidor:  http://localhost:8001
echo  Docs:      http://localhost:8001/docs
echo  Health:    http://localhost:8001/health
echo  Para PARAR: Ctrl+C en esta ventana
echo ============================================================
echo.

python server_rest.py

pause
