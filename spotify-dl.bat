@echo off
setlocal
set "PYTHON_EXE=C:\Users\mehul\AppData\Local\Programs\Python\Python312\python.exe"
if exist "%PYTHON_EXE%" (
    "%PYTHON_EXE%" "%~dp0spotify_dl.py" %*
) else (
    python "%~dp0spotify_dl.py" %*
)
endlocal
