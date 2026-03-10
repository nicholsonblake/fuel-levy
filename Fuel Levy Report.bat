@echo off
echo Generating Fuel Levy Report...
python "%~dp0fuel_levy.py" report
if errorlevel 1 (
    echo.
    echo ERROR: Report generation failed. Check your internet connection.
    pause
)
