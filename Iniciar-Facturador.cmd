@echo off
setlocal EnableExtensions DisableDelayedExpansion
pushd "%~dp0" || exit /b 1

set "DOCKER_CONTEXT="
set "DOCKER_HOST=npipe:////./pipe/docker_engine"
set "FACTURADOR_DOCKER=docker.exe"
where /q docker.exe
if errorlevel 1 set "FACTURADOR_DOCKER=%ProgramFiles%\Docker\Docker\resources\bin\docker.exe"

"%FACTURADOR_DOCKER%" compose version >nul 2>&1
if errorlevel 1 (
    echo Se requiere Docker Desktop con Docker Compose en este equipo.
    goto :error
)

"%FACTURADOR_DOCKER%" info >nul 2>&1
if not errorlevel 1 goto :arrancar

set "FACTURADOR_DESKTOP=%ProgramFiles%\Docker\Docker\Docker Desktop.exe"
if not exist "%FACTURADOR_DESKTOP%" (
    echo Abre Docker Desktop y vuelve a ejecutar este archivo.
    goto :error
)

echo Iniciando Docker Desktop...
start "" "%FACTURADOR_DESKTOP%"
if errorlevel 1 goto :error
for /l %%I in (1,1,60) do (
    "%FACTURADOR_DOCKER%" info >nul 2>&1
    if not errorlevel 1 goto :arrancar
    timeout /t 2 /nobreak >nul
)
echo Docker aun no responde. Revisa el estado en Docker Desktop.
goto :error

:arrancar
echo Preparando Facturador...
"%FACTURADOR_DOCKER%" compose -f compose.yaml up -d --build --wait
if errorlevel 1 goto :error
start "" "http://127.0.0.1:8765/"
popd
exit /b 0

:error
echo No se pudo iniciar Facturador.
pause
popd
exit /b 1
