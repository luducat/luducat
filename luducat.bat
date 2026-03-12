@echo off
setlocal EnableExtensions EnableDelayedExpansion

REM -------------------------------------------------------------------
REM Determine SCRIPT_DIR as the directory of this script (not the CWD)
REM Then normalize to an absolute path without trailing backslash
REM -------------------------------------------------------------------

REM SCRIPT_DIR: directory where this script resides
set "SCRIPT_DIR=%~dp0"
REM Remove trailing backslash if present (except for root like C:\)
if "%SCRIPT_DIR:~-1%"=="\" (
    set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"
)
REM Convert to absolute path (in case %~dp0 was relative)
for %%S in ("%SCRIPT_DIR%") do set "SCRIPT_DIR=%%~fS"
if "%SCRIPT_DIR%" == "" (
    echo "SCRIPT_DIR is empty. stop."
    goto :fatal_exit
)

REM Check for download tool. Windows has built-in curl,
REM but WINE and Windows older than 10 do not have that,
REM so the onboard CURL has to be used.
where curl >nul 2>&1
if %errorlevel% == 0 (
    set "CURL=curl.exe"
) else (
    set "CURL=%SCRIPT_DIR%\tools\wine\curl.exe"
)

REM Normalize SCRIPT_DIR_N by removing trailing backslash, then re-append later as needed
set "SCRIPT_DIR_N=%SCRIPT_DIR%"
for %%I in ("%SCRIPT_DIR_N%\.") do set "SCRIPT_DIR_N=%%~fI"

REM Ensure trailing backslash is present for reliable prefix comparisons
set "SCRIPT_DIR_N=%SCRIPT_DIR_N%\"


REM -------------------------------------------------------------------
REM Create or normalize TEMP_DIR. If a TEMP_DIR is provided, use it;
REM otherwise default to a subfolder inside SCRIPT_DIR_N (e.g., SCRIPT_DIR_Ntemp)
REM Then ensure TEMP_DIR_N is an absolute path inside SCRIPT_DIR_N
REM -------------------------------------------------------------------

REM If TEMP_DIR is not set, default to SCRIPT_DIR_N"temp"
if "%TEMP_DIR%"=="" (
    set "TEMP_DIR=%SCRIPT_DIR_N%temp"
) else (
    REM Normalize provided TEMP_DIR to absolute path
    for %%T in ("%TEMP_DIR%") do set "TEMP_DIR=%%~fT"
)

REM Remove trailing backslash from TEMP_DIR before we compare
if "%TEMP_DIR:~-1%"=="\" (
    set "TEMP_DIR_N=%TEMP_DIR:~0,-1%"
) else (
    set "TEMP_DIR_N=%TEMP_DIR%"
)

REM Ensure TEMP_DIR_N ends with a backslash for prefix comparison
set "TEMP_DIR_N=%TEMP_DIR_N%\"

REM -------------------------------------------------------------------
REM Safety check: TEMP_DIR must be inside SCRIPT_DIR_N
REM We compare by prefix. Build a safe prefix of SCRIPT_DIR_N
REM -------------------------------------------------------------------

REM Get the prefix of TEMP_DIR_N with the SCRIPT_DIR_N length
set "SCRIPT_PREFIX=%SCRIPT_DIR_N%"
set "CHECK_PREFIX=%TEMP_DIR_N:~0,%SCRIPT_DIR_N:~0,9999%"

REM If the TEMP_DIR_N does not start with SCRIPT_DIR_N, abort



REM Configuration
set "PYTHON_VERSION=3.12.8"
set "SCRIPT_DIR=%~dp0"
set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"
set "PYTHON_DIR=%SCRIPT_DIR%\.python"
set "PYPROJECT_FILE=%SCRIPT_DIR%\pyproject.toml"
set "REQUIREMENTS_FILE=%SCRIPT_DIR%\requirements.txt"


echo PYTHON_DIR   = "%PYTHON_DIR%"
echo.
echo SCRIPT_DIR   = "%SCRIPT_DIR%"
echo TEMP_DIR     = "%TEMP_DIR%"
echo CURL         = "%CURL%"
echo.


REM NOT WORKING YET
REM if /I not "%TEMP_DIR_N%"=="%CHECK_PREFIX%" (
REM    echo FATAL: TEMP_DIR is not inside SCRIPT_DIR. Aborting.
REM    goto :fatal_exit
REM )

REM Create temp directory
mkdir "%TEMP_DIR%" 2>nul

REM Check if pyproject.toml exists
if not exist "%PYPROJECT_FILE%" (
    echo ERROR: pyproject.toml not found: %PYPROJECT_FILE%
    echo Make sure you're running this from the luducat directory.
    goto :error_exit
)

REM Check for existing Python in .python directory
if exist "%PYTHON_DIR%\python.exe" (
    echo Using embedded Python from %PYTHON_DIR%
    goto :have_python
)

REM Need to download Python
echo Downloading Python %PYTHON_VERSION% embedded...

REM Determine architecture
set "ARCH=amd64"

set "PYTHON_URL=https://www.python.org/ftp/python/%PYTHON_VERSION%/python-%PYTHON_VERSION%-embed-%ARCH%.zip"
set "PYTHON_ZIP=%TEMP_DIR%\python-embed.zip"

REM Download Python using PowerShell
echo Downloading from %PYTHON_URL%...
rem powershell -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri '%PYTHON_URL%' -OutFile '%PYTHON_ZIP%'"
%CURL% -s --location --output "%PYTHON_ZIP%" "%PYTHON_URL%"

if errorlevel 1 (
    echo ERROR: Failed to download Python. Please check your internet connection.
    goto :error_exit
)

if not exist "%PYTHON_ZIP%" (
    echo ERROR: Python download failed - file not found.
    goto :error_exit
) else (
    dir %PYTHON_ZIP%
)

REM Create Python directory
mkdir "%PYTHON_DIR%" 2>nul

REM Extract Python using PowerShell
echo Extracting Python...
rem powershell -NoProfile -ExecutionPolicy Bypass -Command "Expand-Archive -Path '%PYTHON_ZIP%' -DestinationPath '%PYTHON_DIR%' -Force"
cd "%PYTHON_DIR%"
%SCRIPT_DIR%\tools\wine\7za.exe e -y "%PYTHON_ZIP%"
cd ..

if errorlevel 1 (
    echo ERROR: Failed to extract Python.
    goto :error_exit
)

if not exist "%PYTHON_DIR%\python.exe" (
    echo ERROR: Python extraction failed - python.exe not found.
    goto :error_exit
)

REM Enable pip in embedded Python by modifying python*._pth file
for %%f in ("%PYTHON_DIR%\python*._pth") do (
    echo import site>> "%%f"
)


set PATH=%PYTHON_DIR%\scripts;%PATH%

REM Download get-pip.py
echo Downloading pip for Python.
set "GET_PIP=%TEMP_DIR%\get-pip.py"
REM powershell -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri 'https://bootstrap.pypa.io/get-pip.py' -OutFile '%GET_PIP%'"
%CURL% -s --location --output "%GET_PIP%" "https://bootstrap.pypa.io/get-pip.py"
if errorlevel 1 (
    echo ERROR: Failed to download get-pip.py
    goto :error_exit
)


REM Install pip
echo Download complete, installing pip.
"%PYTHON_DIR%\python.exe" "%GET_PIP%" --no-warn-script-location
if errorlevel 1 (
    echo ERROR: Failed to install pip.
    goto :error_exit
)

rem activates libraries TODO: fix the version part in the file name
echo import site >> %PYTHON_DIR%\python312._pth

echo Importing setup tools...
.python\python.exe -m pip install --upgrade pip setuptools wheel


echo Python %PYTHON_VERSION% installed successfully.

:have_python
set "PYTHON_CMD=%PYTHON_DIR%\python.exe"
set "PIP_CMD=%PYTHON_DIR%\python.exe -m pip"
set PATH=%PYTHON_DIR%\scripts;%PYTHON_DIR%\Lib\site-packages\PySide6;%PATH%

REM Upgrade pip
echo Checking pip...
%PIP_CMD% install --upgrade pip --quiet 2>nul

REM Check if luducat is installed, install if not
%PYTHON_CMD% -c "import luducat" 2>nul
if errorlevel 1 (
    echo Installing luducat and dependencies...
    echo This may take a few minutes on first run...
    %PIP_CMD% install -e "%SCRIPT_DIR%"
    if errorlevel 1 (
        echo ERROR: Failed to install luducat.
        goto :error_exit
    )
    echo luducat installed successfully.
) else (
    echo luducat is already installed.
)

REM WINE Hack due to dll loading problems for QT 6
REM Wine does not properly implement AddDllDirectory, which PySide6 uses
REM to register its directory for DLL dependency resolution (icuuc.dll etc).
REM Copying DLLs and PATH additions do not fix this - Wine limitation.
rem cd %PYTHON_DIR%
rem COPY %PYTHON_DIR%\Lib\site-packages\PySide6\*.dll "%PYTHON_DIR%\"
rem cd ..

REM Run luducat
echo.
echo Checking for any module requirements that are missing...
pip3 install -r "%REQUIREMENTS_FILE%"
echo.
echo Starting luducat...
"%PYTHON_CMD%" -m luducat %*
set "EXIT_CODE=%ERRORLEVEL%"

echo PATH is %PATH%

REM Cleanup temp directory
call :safe_cleanup_temp

REM Pause if double-clicked (no args) or error occurred
if "%~1"=="" goto :maybe_pause
if not "%EXIT_CODE%"=="0" goto :maybe_pause
goto :end

:maybe_pause
REM Check if running interactively (double-clicked)
echo %CMDCMDLINE% | find /i "/c" >nul
if not errorlevel 1 (
    echo.
    pause
)
goto :end

:error_exit
call :safe_cleanup_temp
echo.
pause
exit /b 1

:fatal_exit
echo.
pause
exit /b 1

:end
exit /b %EXIT_CODE%

REM ========== Subroutines ==========

:safe_cleanup_temp
REM Safely remove temp directory with multiple safety checks
REM 1. Must exist
REM 2. Must contain the safety marker
REM 3. Must start with SCRIPT_DIR
REM 4. Must not be the SCRIPT_DIR itself

if not exist "%TEMP_DIR%" exit /b 0

REM Check marker is in path
echo "%TEMP_DIR%" | findstr /C:"%TEMP_MARKER%" >nul
if errorlevel 1 (
    echo WARNING: Skipping cleanup - temp dir missing safety marker
    exit /b 0
)

REM Check it starts with SCRIPT_DIR
echo "%TEMP_DIR%" | findstr /B /C:"%SCRIPT_DIR%" >nul
if errorlevel 1 (
    echo WARNING: Skipping cleanup - temp dir not in script directory
    exit /b 0
)

REM Check it's not the SCRIPT_DIR itself
if "%TEMP_DIR%"=="%SCRIPT_DIR%" (
    echo WARNING: Skipping cleanup - temp dir equals script directory
    exit /b 0
)

REM All checks passed, safe to delete
rmdir /s /q "%TEMP_DIR%" 2>nul
exit /b 0
