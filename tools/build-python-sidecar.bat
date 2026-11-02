@echo off
setlocal EnableExtensions

set "SCRIPT_DIR=%~dp0"
set "SKIP_TESTS=0"
:parse_args
if "%~1"=="" goto args_done
if /I "%~1"=="--skip-tests" (
    set "SKIP_TESTS=1"
) else (
    echo [ERROR] Unknown argument: %~1
    echo Usage: %~nx0 [--skip-tests]
    exit /b 2
)
shift /1
goto parse_args
:args_done

for %%I in ("%SCRIPT_DIR%..") do set "ROOT_DIR=%%~fI"
set "CORE_DIR=%ROOT_DIR%\services\agent-core"
set "TAURI_DIR=%ROOT_DIR%\apps\agent-desktop\src-tauri"
set "BIN_DIR=%TAURI_DIR%\binaries"

echo.
echo ========================================
echo   agent - Build Python Sidecar
echo ========================================
echo.

where conda >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Cannot find Conda on PATH.
    exit /b 1
)

echo [1/8] Using Conda environment: agent...
call conda run --no-capture-output -n agent python --version
if errorlevel 1 (
    echo [ERROR] Cannot run the agent Conda environment.
    exit /b 1
)

if not exist "%CORE_DIR%\pyproject.toml" (
    echo [ERROR] Cannot find %CORE_DIR%\pyproject.toml
    exit /b 1
)
pushd "%CORE_DIR%"

if not exist "requirements-build.lock" (
    echo [ERROR] Cannot find %CORE_DIR%\requirements-build.lock
    popd
    exit /b 1
)

echo [2/8] Installing locked Core build dependencies...
call conda run --no-capture-output -n agent python -m pip install -r requirements-build.lock
if errorlevel 1 (
    echo [ERROR] Failed to install locked Python build dependencies.
    popd
    exit /b 1
)
call conda run --no-capture-output -n agent python -m pip install --no-build-isolation --no-deps -e .
if errorlevel 1 (
    echo [ERROR] Failed to install Python Core dependencies.
    popd
    exit /b 1
)

echo [3/8] Running Python tests...
if "%SKIP_TESTS%"=="1" (
    echo [SKIP] Python tests were explicitly skipped.
) else (
    call conda run --no-capture-output -n agent python -m pytest
    if errorlevel 1 (
        echo [ERROR] Python tests failed.
        popd
        exit /b 1
    )
)

echo.
echo [4/8] Building Python sidecar...
call conda run --no-capture-output -n agent python -m PyInstaller --noconfirm --clean agent-core.spec
if errorlevel 1 (
    echo [ERROR] PyInstaller build failed.
    popd
    exit /b 1
)
if not exist "dist\agent-core.exe" (
    echo [ERROR] Sidecar executable was not generated.
    popd
    exit /b 1
)

echo.
echo [5/8] Detecting Rust target...
where rustc >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Cannot find rustc.
    popd
    exit /b 1
)
for /f "tokens=2" %%A in ('rustc -Vv ^| findstr /b "host:"') do set "TARGET=%%A"
if not defined TARGET (
    echo [ERROR] Cannot detect Rust target triple.
    popd
    exit /b 1
)
echo Rust target: %TARGET%

echo.
echo [6/8] Verifying sidecar compatibility...
call powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%verify-python-sidecar.ps1" -RepositoryRoot "%ROOT_DIR%" -SidecarPath "%CORE_DIR%\dist\agent-core.exe"
if errorlevel 1 (
    echo [ERROR] Sidecar compatibility check failed.
    popd
    exit /b 1
)

echo.
echo [7/8] Copying sidecar to Tauri...
if not exist "%BIN_DIR%" mkdir "%BIN_DIR%"
copy /Y "dist\agent-core.exe" "%BIN_DIR%\agent-core-%TARGET%.exe" >nul
if errorlevel 1 (
    echo [ERROR] Failed to copy sidecar.
    popd
    exit /b 1
)
popd

echo.
echo [8/8] Build completed successfully.
echo Output: %BIN_DIR%\agent-core-%TARGET%.exe
echo.
endlocal
exit /b 0
