@echo off
REM ============================================================================
REM  AIRS - Developer Startup Automation
REM
REM  Startup order (enforced, does not proceed to the next stage until the
REM  current one is confirmed ready):
REM
REM      Redis -> Celery Ready -> FastAPI Ready -> Recovery -> Application Ready
REM
REM  Idempotent: an already-running Redis instance / Celery worker / FastAPI
REM  server is detected and reused, never restarted or duplicated. Safe to
REM  run this script multiple times.
REM
REM  Usage:
REM      start-project.bat
REM
REM  All tunables live in the CONFIGURATION block below - nothing else in
REM  this file needs to change to point at a different host/port/app path.
REM ============================================================================
 
setlocal EnableExtensions
 
REM ---------------------------------------------------------------------------
REM  CONFIGURATION (edit these, not the logic below)
REM ---------------------------------------------------------------------------
set "PROJECT_DIR=%~dp0"
set "VENV_ACTIVATE=%PROJECT_DIR%venv\Scripts\activate.bat"
 
set "REDIS_HOST=127.0.0.1"
set "REDIS_PORT=6379"
set "REDIS_CONTAINER=airs-redis"
@REM set "REDIS_SERVER_CMD=redis-server"
@REM set "REDIS_CLI_CMD=redis-cli"
 
set "CELERY_APP=app.core.celery_app"
set "CELERY_LOGLEVEL=info"
 
set "FASTAPI_APP=app.main:app"
set "FASTAPI_HOST=127.0.0.1"
set "FASTAPI_PORT=8002"
 
set "RECOVERY_SCRIPT=scripts\generate_missing_skill_embeddings.py"
 
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
echo [1/5] Redis
call :ensure_redis
if errorlevel 1 goto :fail
 
echo.
echo [2/5] Celery Worker
call :ensure_celery
if errorlevel 1 goto :fail
 
echo.
echo [3/5] FastAPI
call :ensure_fastapi
if errorlevel 1 goto :fail
 
echo.
echo [4/5] Missing Skill Embedding Recovery
call :run_recovery
 
echo.
echo [5/5] Application Ready
echo ------------------------------------------------------------
echo  Redis    : ready   (%REDIS_HOST%:%REDIS_PORT%)
echo  Celery   : ready
echo  FastAPI  : ready   (http://%FASTAPI_HOST%:%FASTAPI_PORT%/docs)
echo  Recovery : completed
echo ------------------------------------------------------------
echo  Application Ready.
echo ------------------------------------------------------------
del "%_HTTP_CODE_FILE%" >nul 2>&1
endlocal
exit /b 0
 
:fail
echo.
echo [FAILED] Startup aborted - see the message above.
del "%_HTTP_CODE_FILE%" >nul 2>&1
endlocal
exit /b 1
 
@REM REM =============================================================================
@REM REM  Stage: Redis
@REM REM =============================================================================
@REM :ensure_redis
@REM "%REDIS_CLI_CMD%" -h %REDIS_HOST% -p %REDIS_PORT% ping >nul 2>&1
@REM if not errorlevel 1 (
@REM     echo   [SKIP]  Redis is already running on %REDIS_HOST%:%REDIS_PORT% - reusing it.
@REM     goto :eof
@REM )
 
@REM echo   [START] Redis is not running - starting "%REDIS_SERVER_CMD%" ...
@REM start "AIRS - Redis" cmd /k %REDIS_SERVER_CMD% --port %REDIS_PORT%
 
@REM set /a _elapsed=0
@REM :wait_redis
@REM "%REDIS_CLI_CMD%" -h %REDIS_HOST% -p %REDIS_PORT% ping >nul 2>&1
@REM if not errorlevel 1 (
@REM     echo   [READY] Redis is up.
@REM     goto :eof
@REM )
@REM if %_elapsed% GEQ %READY_TIMEOUT_SECONDS% (
@REM     echo   [ERROR] Redis did not become ready within %READY_TIMEOUT_SECONDS% seconds.
@REM     exit /b 1
@REM )
@REM ping -n %POLL_INTERVAL_SECONDS% 127.0.0.1 >nul
@REM set /a _elapsed+=%POLL_INTERVAL_SECONDS%
@REM goto :wait_redis
 
REM =============================================================================
REM  Stage: Redis - Docker
REM =============================================================================
:ensure_redis

REM -----------------------------------------------------------------
REM Ensure Docker is running
REM -----------------------------------------------------------------
docker info >nul 2>&1
if errorlevel 1 (
    echo   [ERROR] Docker Desktop is not running.
    exit /b 1
)

REM -----------------------------------------------------------------
REM Does the container exist?
REM -----------------------------------------------------------------
docker inspect %REDIS_CONTAINER% >nul 2>&1
if errorlevel 1 (
    goto :create_container
)

REM -----------------------------------------------------------------
REM Verify required port mapping exists
REM -----------------------------------------------------------------
set "_PORT_OK="
for /f "delims=" %%P in ('docker port %REDIS_CONTAINER% 6379/tcp 2^>nul') do (
    set "_PORT_OK=1"
)

if not defined _PORT_OK (
    echo   [FIX] Existing Redis container has no host port mapping.
    echo   [FIX] Recreating container...

    docker rm -f %REDIS_CONTAINER% >nul 2>&1

    if errorlevel 1 (
        echo   [ERROR] Failed to remove Redis container.
        exit /b 1
    )

    goto :create_container
)

REM -----------------------------------------------------------------
REM Start container if stopped
REM -----------------------------------------------------------------
docker inspect -f "{{.State.Running}}" %REDIS_CONTAINER% | findstr /i "true" >nul

if errorlevel 1 (
    echo   [START] Starting Redis container...
    docker start %REDIS_CONTAINER% >nul

    if errorlevel 1 (
        echo   [ERROR] Failed to start Redis container.
        exit /b 1
    )
) else (
    echo   [SKIP] Redis container already running.
)

goto :wait_redis

:create_container

echo   [START] Creating Redis container...

docker run -d ^
    --name %REDIS_CONTAINER% ^
    -p %REDIS_PORT%:6379 ^
    --restart unless-stopped ^
    redis

if errorlevel 1 (
    echo   [ERROR] Failed to create Redis container.
    exit /b 1
)

:wait_redis

set /a _elapsed=0

:wait_loop

powershell -Command ^
"$r=Test-NetConnection -ComputerName %REDIS_HOST% -Port %REDIS_PORT% -WarningAction SilentlyContinue; if($r.TcpTestSucceeded){exit 0}else{exit 1}" >nul 2>&1

if not errorlevel 1 (
    echo   [READY] Redis is available at %REDIS_HOST%:%REDIS_PORT%.
    goto :eof
)

if %_elapsed% GEQ %READY_TIMEOUT_SECONDS% (
    echo   [ERROR] Redis did not become ready within %READY_TIMEOUT_SECONDS% seconds.
    exit /b 1
)

timeout /t %POLL_INTERVAL_SECONDS% /nobreak >nul
set /a _elapsed+=%POLL_INTERVAL_SECONDS%

goto :wait_loop
 
REM =============================================================================
REM  Stage: Celery Worker
REM =============================================================================
:ensure_celery
call "%VENV_ACTIVATE%"
celery -A %CELERY_APP% inspect ping -t 2 >nul 2>&1
if not errorlevel 1 (
    echo   [SKIP]  A Celery worker is already responding - reusing it.
    goto :eof
)
 
echo   [START] Starting Celery worker ...
start "AIRS - Celery Worker" cmd /k call "%VENV_ACTIVATE%" ^&^& celery -A %CELERY_APP% worker --loglevel=%CELERY_LOGLEVEL%
 
set /a _elapsed=0
:wait_celery
celery -A %CELERY_APP% inspect ping -t 2 >nul 2>&1
if not errorlevel 1 (
    echo   [READY] Celery worker is ready.
    goto :eof
)
if %_elapsed% GEQ %READY_TIMEOUT_SECONDS% (
    echo   [ERROR] Celery worker did not become ready within %READY_TIMEOUT_SECONDS% seconds.
    exit /b 1
)
ping -n %POLL_INTERVAL_SECONDS% 127.0.0.1 >nul
set /a _elapsed+=%POLL_INTERVAL_SECONDS%
goto :wait_celery
 
REM =============================================================================
REM  Stage: FastAPI
REM =============================================================================
:ensure_fastapi
call :check_fastapi
if "%_HTTP_CODE%"=="200" (
    echo   [SKIP]  FastAPI is already running at http://%FASTAPI_HOST%:%FASTAPI_PORT% - reusing it.
    goto :eof
)
 
echo   [START] Starting FastAPI (auto-reload on code changes) ...
start "AIRS - FastAPI" cmd /k call "%VENV_ACTIVATE%" ^&^& uvicorn %FASTAPI_APP% --host %FASTAPI_HOST% --port %FASTAPI_PORT% --reload
 
set /a _elapsed=0
:wait_fastapi
call :check_fastapi
if "%_HTTP_CODE%"=="200" (
    echo   [READY] FastAPI startup completed.
    goto :eof
)
if %_elapsed% GEQ %READY_TIMEOUT_SECONDS% (
    echo   [ERROR] FastAPI did not become ready within %READY_TIMEOUT_SECONDS% seconds.
    exit /b 1
)
ping -n %POLL_INTERVAL_SECONDS% 127.0.0.1 >nul
set /a _elapsed+=%POLL_INTERVAL_SECONDS%
goto :wait_fastapi
 
:check_fastapi
curl -s -o nul -w "%%{http_code}" "%_FASTAPI_CHECK_URL%" > "%_HTTP_CODE_FILE%" 2>nul
set "_HTTP_CODE="
set /p _HTTP_CODE=<"%_HTTP_CODE_FILE%"
goto :eof
 
REM =============================================================================
REM  Stage: Recovery (Missing Skill Embedding Recovery)
REM  Runs only after FastAPI is confirmed ready. Does not modify the recovery
REM  script's own logic - it already queues only active skills, skips inactive
REM  ones, and does nothing when no recovery is required.
REM =============================================================================
:run_recovery
call "%VENV_ACTIVATE%"
echo   [RUN]   Checking for Skill Ontology rows missing an embedding ...
python "%RECOVERY_SCRIPT%"
if errorlevel 1 (
    echo   [WARN]  Recovery script exited with an error - continuing startup regardless.
) else (
    echo   [DONE]  Recovery check completed.
)
goto :eof
 
 