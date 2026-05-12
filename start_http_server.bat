@echo off
REM ============================================================
REM  Arranca el servidor MCP en modo HTTP (localhost:8000/mcp)
REM ============================================================
REM  Despues, abre otro CMD y ejecuta start_tunnel.bat
REM  para exponerlo por internet via Cloudflare Tunnel.
REM
REM  Log: server.log (se recrea en cada arranque)
REM ============================================================

title Fashion Trends - Server
cd /d "%~dp0"

REM Forzar UTF-8 para que los emojis y caracteres acentuados se vean bien
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

REM Comprobar que no hay ya otro server corriendo en el puerto 8000.
REM Si lo hay, avisar y abortar (en lugar de fallar al abrir el log).
netstat -ano | findstr :8000 | findstr LISTENING >nul 2>&1
if %errorlevel%==0 (
    echo.
    echo ============================================================
    echo  ERROR: El puerto 8000 ya esta en uso.
    echo  Probablemente hay otro server corriendo.
    echo.
    echo  Cierra la otra ventana "Fashion Trends - Server" antes
    echo  de relanzar este script.
    echo.
    echo  Para forzar el cierre de TODOS los procesos Python:
    echo    taskkill /F /IM python.exe
    echo ============================================================
    echo.
    pause
    exit /b 1
)

REM Borrar log anterior si existe (recrear en cada arranque).
REM Si esta bloqueado, lo ignoramos y usamos otro nombre.
if exist "server.log" (
    del /q "server.log" 2>nul
    if exist "server.log" (
        echo AVISO: server.log esta bloqueado, usando server-new.log
        set "LOG_FILE=server-new.log"
    ) else (
        set "LOG_FILE=server.log"
    )
) else (
    set "LOG_FILE=server.log"
)

echo.
echo [1/2] Activando entorno virtual...
call .venv\Scripts\activate.bat
if errorlevel 1 (
    echo ERROR: no se pudo activar el venv.
    pause
    exit /b 1
)

echo [2/2] Arrancando server_http.py...
echo.
echo ============================================================
echo  Servidor: http://localhost:8000/mcp
echo  Health:   http://localhost:8000/health
echo  Log:      %LOG_FILE% (se actualiza en tiempo real)
echo  Para PARAR: Ctrl+C en esta ventana
echo ============================================================
echo.

REM Ejecuta el servidor mostrando la salida en pantalla Y guardandola en log.
REM El truco con ForEach-Object suprime el ruido "+ CategoryInfo" de PowerShell
REM que marca los mensajes de stderr como errores rojos cuando solo son logs.
powershell -NoProfile -Command "$OutputEncoding = [Console]::OutputEncoding = [System.Text.Encoding]::UTF8; python -u server_http.py 2>&1 | ForEach-Object { '{0}' -f $_ } | Tee-Object -FilePath '%LOG_FILE%'"

pause
