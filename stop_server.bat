@echo off
REM ============================================================
REM  Para TODOS los procesos del servidor MCP fashion-trends
REM ============================================================
REM  Util si algo se quedo colgado o si quieres cerrar el servidor
REM  antes de editar codigo sin reiniciar Claude Desktop entero.
REM ============================================================

echo.
echo Buscando procesos Python del servidor fashion-trends...

set KILLED=0
for /f "tokens=2 delims=," %%i in (
    'wmic process where "name='python.exe' and commandline like '%%server.py%%'" get processid /format:csv ^| findstr /r "[0-9]"'
) do (
    echo Matando PID %%i
    taskkill /F /PID %%i 2>nul
    set /a KILLED+=1
)

if %KILLED%==0 (
    echo No hay procesos del servidor corriendo.
) else (
    echo Terminados %KILLED% proceso(s).
)

echo.
pause
