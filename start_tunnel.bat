@echo off
REM ============================================================
REM  Expone el servidor MCP a internet via Cloudflare Tunnel
REM ============================================================
REM  Requisito previo: descargar cloudflared.exe
REM    https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/
REM  Ponlo en esta carpeta o en el PATH del sistema.
REM
REM  Requisito previo 2: tener server_http.py corriendo
REM  (ejecuta start_http_server.bat primero en OTRO CMD).
REM
REM  Log: tunnel.log (se recrea en cada arranque)
REM ============================================================

title Fashion Trends - Tunnel
cd /d "%~dp0"

chcp 65001 >nul

echo.
echo Comprobando cloudflared...

REM ESTRATEGIA DE BUSQUEDA (en orden):
REM 1. cloudflared.exe en la carpeta actual (mas fiable)
REM 2. C:\Program Files (x86)\cloudflared\cloudflared.exe (instalador oficial)
REM 3. C:\Program Files\cloudflared\cloudflared.exe
REM 4. Como ultimo recurso, "where" en el PATH
REM Usamos la RUTA ABSOLUTA siempre que sea posible para que PowerShell
REM no tenga problemas para localizarlo.
set "CLOUDFLARED_PATH="

if exist "%~dp0cloudflared.exe" (
    set "CLOUDFLARED_PATH=%~dp0cloudflared.exe"
    goto found
)

if exist "C:\Program Files (x86)\cloudflared\cloudflared.exe" (
    set "CLOUDFLARED_PATH=C:\Program Files (x86)\cloudflared\cloudflared.exe"
    goto found
)

if exist "C:\Program Files\cloudflared\cloudflared.exe" (
    set "CLOUDFLARED_PATH=C:\Program Files\cloudflared\cloudflared.exe"
    goto found
)

REM Como fallback: intentar resolver con `where` y capturar la ruta exacta.
for /f "delims=" %%i in ('where cloudflared 2^>nul') do (
    if not defined CLOUDFLARED_PATH (
        set "CLOUDFLARED_PATH=%%i"
    )
)

:found

if "%CLOUDFLARED_PATH%"=="" (
    echo.
    echo ============================================================
    echo  ERROR: no se encuentra cloudflared.exe.
    echo.
    echo  OPCION 1 ^(recomendada^): descarga el .exe y ponlo en esta
    echo  misma carpeta:
    echo    %~dp0
    echo.
    echo  Descarga: https://github.com/cloudflare/cloudflared/releases/latest
    echo  Archivo:  cloudflared-windows-amd64.exe
    echo  Renombra a: cloudflared.exe
    echo.
    echo  OPCION 2: instalar via winget:
    echo    winget install --id Cloudflare.cloudflared
    echo ============================================================
    echo.
    pause
    exit /b 1
)

echo Usando: %CLOUDFLARED_PATH%

REM Borrar log anterior si existe
if exist "tunnel.log" del /q "tunnel.log" 2>nul

echo.
echo ============================================================
echo  Cloudflare te dara una URL publica temporal tipo:
echo    https://xxx-yyy-zzz.trycloudflare.com
echo.
echo  Tu endpoint MCP publico sera:
echo    https://xxx-yyy-zzz.trycloudflare.com/mcp
echo.
echo  Log: tunnel.log (se actualiza en tiempo real)
echo  Para PARAR el tunnel: Ctrl+C
echo ============================================================
echo.

REM Pasar la ruta absoluta a PowerShell entre comillas para que
REM espacios en "Program Files" no rompan nada.
powershell -NoProfile -Command "$OutputEncoding = [Console]::OutputEncoding = [System.Text.Encoding]::UTF8; & \"%CLOUDFLARED_PATH%\" tunnel --url http://localhost:8000 2>&1 | ForEach-Object { '{0}' -f $_ } | Tee-Object -FilePath 'tunnel.log'"

pause
