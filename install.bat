@echo off
rem Huetooth - install from source: installs the two Python packages and
rem adds "Huetooth" shortcuts to the desktop and the Start menu.
setlocal EnableExtensions
cd /d "%~dp0"
title Huetooth - install from source

echo.
echo   Huetooth - install from source
echo   ====================================
echo.
echo   Tip: the ready-made app needs no Python at all:
echo   https://github.com/jargyl/huetooth/releases/latest
echo.

rem --- 1. Python 3.10 or newer ---------------------------------------------------
set "PY="
py -3 -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1
if not errorlevel 1 set "PY=py -3"
if defined PY goto :have_python
python -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1
if not errorlevel 1 set "PY=python"
if not defined PY goto :no_python
:have_python
for /f "usebackq delims=" %%V in (`%PY% --version`) do echo   Found %%V
echo.

rem --- 2. The packages: bleak (Bluetooth) and Pillow (graphics) -------------------
echo   Installing the packages Huetooth needs...
%PY% -m pip install --disable-pip-version-check -q -r requirements.txt
if errorlevel 1 goto :pip_failed
%PY% -c "import bleak, PIL" >nul 2>&1
if errorlevel 1 goto :pip_failed
echo   Done.
echo.

rem --- 3. Shortcuts that start the app without a console window ------------------
for /f "usebackq delims=" %%P in (`%PY% -c "import sys, pathlib; print(pathlib.Path(sys.executable).with_name('pythonw.exe'))"`) do set "HT_PYTHONW=%%P"
if not exist "%HT_PYTHONW%" goto :no_pythonw
set "HT_DIR=%~dp0"
if not defined HT_LINK_FOLDERS set "HT_LINK_FOLDERS=Desktop Programs"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$w = New-Object -ComObject WScript.Shell; foreach ($f in $env:HT_LINK_FOLDERS -split ' ') { $dir = if ([IO.Path]::IsPathRooted($f)) { $f } else { [Environment]::GetFolderPath($f) }; $l = $w.CreateShortcut((Join-Path $dir 'Huetooth.lnk')); $l.TargetPath = $env:HT_PYTHONW; $l.Arguments = [char]34 + $env:HT_DIR + 'Huetooth.pyw' + [char]34; $l.WorkingDirectory = $env:HT_DIR; $l.IconLocation = $env:HT_DIR + 'assets\app.ico'; $l.Description = 'Control your Philips Hue Bluetooth lights'; $l.Save() }"
if errorlevel 1 goto :no_shortcuts
echo   Added "Huetooth" to your desktop and Start menu.
goto :ready

:no_pythonw
echo   Note: pythonw.exe was not found, so no shortcuts were made.
echo   Start the app by double-clicking "Huetooth.pyw" in this folder.
set "HT_PYTHONW="
goto :ready

:no_shortcuts
echo   Note: the shortcuts could not be created. Start the app by
echo   double-clicking "Huetooth.pyw" in this folder.

:ready
echo.
echo   All set. On the first start, Huetooth helps you find your lights.
echo.
set "HT_START="
set /p "HT_START=  Start Huetooth now? [Y/n] "
if /i "%HT_START%"=="n" goto :end
if /i "%HT_START%"=="no" goto :end
if defined HT_PYTHONW (
    start "" "%HT_PYTHONW%" "%~dp0Huetooth.pyw"
) else (
    start "" "%~dp0Huetooth.pyw"
)
goto :end

:no_python
echo   Python 3.10 or newer was not found on this PC.
echo.
echo   Easiest: download the ready-made app instead - no Python needed:
echo     https://github.com/jargyl/huetooth/releases/latest
echo.
echo   Or install Python from https://www.python.org/downloads/
echo   (tick "Add python.exe to PATH") and run install.bat again.
echo.
pause
exit /b 1

:pip_failed
echo.
echo   Installing the packages failed - see the messages above.
echo   Check your internet connection and run install.bat again.
echo.
pause
exit /b 1

:end
endlocal
exit /b 0
