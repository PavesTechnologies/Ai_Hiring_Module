@echo off
REM ============================================================================
REM AIRS - Developer Startup Automation
REM
REM Startup order:
REM     Redis Ready -> Celery Ready -> FastAPI Ready -> Application Ready
REM
REM Auto Reload:
REM     Celery  : ENABLED via watchmedo
REM     FastAPI : ENABLED via uvicorn --reload
REM
REM Infrastructure:
REM     Redis : Local Docker container
REM     RDS   : AWS RDS - NOT managed by this script
REM
REM Redis behavior:
REM     Existing + Running -> SKIP and reuse
REM     Existing + Stopped -> START and reuse
REM     Not Existing -> CREATE
REM ============================================================================

setlocal EnableExtensions

REM ============================================================================
REM CONFIGURATION
REM ============================================================================

set "PROJECT_DIR=%~dp0"
set "VENV_ACTIVATE=%PROJECT_DIR%venv\Scripts\activate.bat"

REM ----------------------------------------------------------------------------
REM Redis - Local Docker
REM ----------------------------------------------------------------------------

set "REDIS_CONTAINER=airs-redis"
set "REDIS_IMAGE=redis:7-alpine"
set "REDIS_PORT_HOST=6379"

REM ----------------------------------------------------------------------------
REM Celery
REM ----------------------------------------------------------------------------

set "CELERY_APP=app.core.celery_app"
set "CELERY_LOGLEVEL=info"
set "CELERY_WATCH_DIR=%PROJECT_DIR%app"

REM ----------------------------------------------------------------------------
REM FastAPI
REM ----------------------------------------------------------------------------

set "FASTAPI_APP=app.main:app"
set "FASTAPI_HOST=127.0.0.1"
set "FASTAPI_PORT=8002"

REM ----------------------------------------------------------------------------
REM Startup settings
REM ----------------------------------------------------------------------------

set "READY_TIMEOUT_SECONDS=60"
set "POLL_INTERVAL_SECONDS=2"

set "_FASTAPI_CHECK_URL=http://%FASTAPI_HOST%:%FASTAPI_PORT%/openapi.json"
set "_HTTP_CODE_FILE=%TEMP%\airs_startup_http_code.txt"

cd /d "%PROJECT_DIR%"

echo.
echo ============================================================
echo  AIRS Developer Startup
echo ============================================================
echo.

REM ============================================================================
REM [1/3] REDIS
REM ============================================================================

echo [1/3] Local Redis (Docker)

call :ensure_redis

if errorlevel 1 goto :fail


REM ============================================================================
REM [2/3] CELERY
REM ============================================================================

echo.
echo [2/3] Celery Worker

call :ensure_celery

if errorlevel 1 goto :fail


REM ============================================================================
REM [3/3] FASTAPI
REM ============================================================================

echo.
echo [3/3] FastAPI Application

call :ensure_fastapi

if errorlevel 1 goto :fail


REM ============================================================================
REM APPLICATION READY
REM ============================================================================

echo.
echo ============================================================
echo  AIRS Application Ready
echo ============================================================
echo  Redis   : READY (docker://%REDIS_CONTAINER%)
echo  Celery  : READY + AUTO-RELOAD
echo  FastAPI : READY + AUTO-RELOAD
echo  API     : http://%FASTAPI_HOST%:%FASTAPI_PORT%
echo  Docs    : http://%FASTAPI_HOST%:%FASTAPI_PORT%/docs
echo ============================================================
echo.

del "%_HTTP_CODE_FILE%" >nul 2>&1

endlocal
exit /b 0


REM ============================================================================
REM FAILURE HANDLER
REM ============================================================================

:fail

echo.
echo ============================================================
echo  [FAILED] AIRS Startup Aborted
echo ============================================================
echo.

del "%_HTTP_CODE_FILE%" >nul 2>&1

endlocal
exit /b 1


REM ============================================================================
REM LOCAL REDIS - DOCKER
REM
REM Behavior:
REM   Container exists + running -> SKIP and reuse
REM   Container exists + stopped  -> START and reuse
REM   Container does not exist    -> CREATE
REM ============================================================================

:ensure_redis

echo   [CHECK] Checking Docker daemon...

docker info >nul 2>&1

if errorlevel 1 (
    echo   [ERROR] Docker daemon is not running.
    echo   [ERROR] Start Docker Desktop and retry.
    exit /b 1
)

echo   [CHECK] Checking Redis container...


REM ----------------------------------------------------------------------------
REM Check whether the exact Redis container exists
REM ----------------------------------------------------------------------------

docker inspect "%REDIS_CONTAINER%" >nul 2>&1

if not errorlevel 1 (
    goto :redis_exists
)


REM ----------------------------------------------------------------------------
REM Container does not exist - create it
REM ----------------------------------------------------------------------------

echo   [CREATE] Redis container "%REDIS_CONTAINER%" does not exist.
echo   [CREATE] Creating from %REDIS_IMAGE%...

docker run -d ^
    --name "%REDIS_CONTAINER%" ^
    -p %REDIS_PORT_HOST%:6379 ^
    --restart unless-stopped ^
    %REDIS_IMAGE%

if errorlevel 1 (
    echo   [ERROR] Failed to create Redis container.
    exit /b 1
)

echo   [CREATED] Redis container created successfully.

goto :wait_redis


REM ----------------------------------------------------------------------------
REM Container exists
REM ----------------------------------------------------------------------------

:redis_exists

REM Check whether existing container is running

for /f "delims=" %%R in ('docker inspect -f "{{.State.Running}}" "%REDIS_CONTAINER%" 2^>nul') do (
    set "_REDIS_RUNNING=%%R"
)

if /i "%_REDIS_RUNNING%"=="true" (
    echo   [SKIP] Redis container "%REDIS_CONTAINER%" is already running.
    echo   [SKIP] Reusing existing Redis container.
    goto :wait_redis
)


REM ----------------------------------------------------------------------------
REM Existing container is stopped
REM ----------------------------------------------------------------------------

echo   [FOUND] Redis container "%REDIS_CONTAINER%" already exists.
echo   [START] Starting existing Redis container...

docker start "%REDIS_CONTAINER%" >nul

if errorlevel 1 (
    echo   [ERROR] Failed to start existing Redis container.
    exit /b 1
)

echo   [STARTED] Existing Redis container started.

goto :wait_redis


REM ----------------------------------------------------------------------------
REM Wait until Redis is ready
REM ----------------------------------------------------------------------------

:wait_redis

set /a _elapsed=0

:wait_redis_loop

docker exec "%REDIS_CONTAINER%" redis-cli ping 2>nul | findstr /i "PONG" >nul 2>&1

if not errorlevel 1 (
    echo   [READY] Redis is ready at 127.0.0.1:%REDIS_PORT_HOST%.
    goto :eof
)

if %_elapsed% GEQ %READY_TIMEOUT_SECONDS% (
    echo   [ERROR] Redis did not become ready within %READY_TIMEOUT_SECONDS% seconds.
    exit /b 1
)

timeout /t %POLL_INTERVAL_SECONDS% /nobreak >nul

set /a _elapsed+=%POLL_INTERVAL_SECONDS%

goto :wait_redis_loop

REM ============================================================================
REM CELERY WORKER
REM
REM Auto-reload is handled by watchmedo because your current Celery version
REM does not support the --autoreload option.
REM
REM Install once:
REM     pip install watchdog
REM ============================================================================

:ensure_celery

call "%VENV_ACTIVATE%"

echo   [CHECK] Checking existing Celery worker...

celery -A %CELERY_APP% inspect ping -t 2 >nul 2>&1

if not errorlevel 1 (
    echo   [SKIP] Celery worker is already responding.
    goto :eof
)


REM ----------------------------------------------------------------------------
REM Check watchmedo
REM ----------------------------------------------------------------------------

where watchmedo >nul 2>&1

if errorlevel 1 (
    echo   [ERROR] watchmedo is not installed.
    echo.
    echo   Install it using:
    echo.
    echo       pip install watchdog
    echo.
    exit /b 1
)


REM ----------------------------------------------------------------------------
REM Start Celery with auto-reload
REM ----------------------------------------------------------------------------

echo   [START] Starting Celery worker with AUTO-RELOAD...
echo   [WATCH] Watching: %CELERY_WATCH_DIR%

start "AIRS - Celery Worker" cmd /k ^
call "%VENV_ACTIVATE%" ^&^& ^
watchmedo auto-restart ^
--directory="%CELERY_WATCH_DIR%" ^
--pattern="*.py" ^
--recursive ^
-- ^
celery -A %CELERY_APP% worker ^
--loglevel=%CELERY_LOGLEVEL% ^
--pool=solo


REM ----------------------------------------------------------------------------
REM Wait for Celery
REM ----------------------------------------------------------------------------

set /a _elapsed=0

:wait_celery

celery -A %CELERY_APP% inspect ping -t 2 >nul 2>&1

if not errorlevel 1 (
    echo   [READY] Celery worker is ready.
    echo   [READY] Celery auto-reload is enabled.
    goto :eof
)

if %_elapsed% GEQ %READY_TIMEOUT_SECONDS% (
    echo   [ERROR] Celery worker did not become ready within %READY_TIMEOUT_SECONDS% seconds.
    exit /b 1
)

timeout /t %POLL_INTERVAL_SECONDS% /nobreak >nul

set /a _elapsed+=%POLL_INTERVAL_SECONDS%

goto :wait_celery


REM ============================================================================
REM FASTAPI APPLICATION
REM ============================================================================

:ensure_fastapi

call :check_fastapi

if "%_HTTP_CODE%"=="200" (
    echo   [SKIP] FastAPI is already running.
    goto :eof
)

echo   [START] Starting FastAPI with AUTO-RELOAD...

start "AIRS - FastAPI" cmd /k "call "%VENV_ACTIVATE%" && uvicorn %FASTAPI_APP% --host %FASTAPI_HOST% --port %FASTAPI_PORT% --reload"

set /a _elapsed=0

:wait_fastapi

call :check_fastapi

if "%_HTTP_CODE%"=="200" (
    echo   [READY] FastAPI application is ready.
    echo   [READY] FastAPI auto-reload is enabled.
    goto :eof
)

if %_elapsed% GEQ %READY_TIMEOUT_SECONDS% (
    echo   [ERROR] FastAPI did not become ready within %READY_TIMEOUT_SECONDS% seconds.
    exit /b 1
)

timeout /t %POLL_INTERVAL_SECONDS% /nobreak >nul

set /a _elapsed+=%POLL_INTERVAL_SECONDS%

goto :wait_fastapi


REM ============================================================================
REM FASTAPI HEALTH CHECK
REM ============================================================================

:check_fastapi

set "_HTTP_CODE="

curl -s -o nul -w "%%{http_code}" "%_FASTAPI_CHECK_URL%" > "%_HTTP_CODE_FILE%" 2>nul

if not exist "%_HTTP_CODE_FILE%" (
    set "_HTTP_CODE=000"
    goto :eof
)

set /p _HTTP_CODE=<"%_HTTP_CODE_FILE%"

if not defined _HTTP_CODE (
    set "_HTTP_CODE=000"
)

goto :eof