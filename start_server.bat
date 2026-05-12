@echo off
REM ============================================================
REM  Fashion Trends MCP Server - INICIO con MCP Inspector
REM ============================================================
REM  Uso: doble clic en este archivo, o ejecutar desde CMD:
REM      start_server.bat
REM
REM  Lanza el servidor bajo el MCP Inspector para pruebas.
REM  Para usarlo con Claude Desktop NO necesitas este script:
REM  Claude Desktop arranca el servidor solo al abrirse.
REM ============================================================

cd /d "%~dp0"

echo.
echo [1/3] Activando entorno virtual...
call .venv\Scripts\activate.bat
if errorlevel 1 (
    echo ERROR: no se pudo activar el venv. Ejecuta primero:
    echo        python -m venv .venv
    echo        .venv\Scripts\activate
    echo        pip install -r requirements.txt
    pause
    exit /b 1
)

echo [2/3] Verificando dependencias...
python -c "import mcp, pytrends, praw, dotenv" 2>nul
if errorlevel 1 (
    echo Instalando dependencias que faltan...
    pip install -r requirements.txt
)

echo [3/3] Lanzando MCP Inspector...
echo.
echo ============================================================
echo  Abrira una pestana del navegador en http://localhost:5173
echo  Para PARAR el servidor: pulsa Ctrl+C en esta ventana
echo ============================================================
echo.

npx @modelcontextprotocol/inspector python server.py

pause
