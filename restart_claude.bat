@echo off
REM ============================================================
REM  Reinicia Claude Desktop COMPLETAMENTE
REM ============================================================
REM  Usalo cuando modifiques server.py y necesites que Claude
REM  Desktop recargue el codigo nuevo del servidor MCP.
REM
REM  Cierra Claude Desktop + procesos Python del MCP,
REM  espera 2 segundos, y vuelve a abrir Claude Desktop.
REM ============================================================

echo.
echo [1/3] Cerrando Claude Desktop...
taskkill /F /IM Claude.exe 2>nul
taskkill /F /IM "Claude Helper.exe" 2>nul

echo [2/3] Matando procesos Python del servidor MCP...
REM Solo mata procesos python.exe cuyo commandline contenga "server.py"
REM (asi no matamos otros Python que puedas tener corriendo)
for /f "tokens=2 delims=," %%i in (
    'wmic process where "name='python.exe' and commandline like '%%server.py%%'" get processid /format:csv ^| findstr /r "[0-9]"'
) do (
    taskkill /F /PID %%i 2>nul
)

echo [3/3] Esperando 2 segundos y reabriendo Claude Desktop...
timeout /t 2 /nobreak >nul

REM Intenta abrir Claude desde las ubicaciones habituales.
REM Si tu instalacion esta en otra ruta, edita la linea de abajo.
start "" "claude://"
if errorlevel 1 (
    echo.
    echo No se pudo abrir Claude con el protocolo claude://
    echo Abrelo manualmente desde el menu Inicio.
)

echo.
echo Listo. Comprueba que Claude Desktop se abrio y prueba el servidor.
timeout /t 3 >nul
