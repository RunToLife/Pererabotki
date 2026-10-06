@echo off
rem Автоматическая установка и запуск сервиса "Переработка" (Windows).
rem   install.bat                 установить, включить автозапуск при входе и запустить
rem   install.bat --no-autostart  только установить зависимости
rem   set PERER_PORT=9000 ^& install.bat   другой порт (по умолчанию 8080)
chcp 65001 >nul
setlocal EnableDelayedExpansion
cd /d "%~dp0"
set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
if not defined PERER_PORT set "PERER_PORT=8080"
if not defined PERER_HOST set "PERER_HOST=127.0.0.1"

echo.
echo ==^> Проверка Python
set "PY="
where py >nul 2>nul && (py -3 -c "import sys; sys.exit(0 if sys.version_info>=(3,8) else 1)" >nul 2>nul && set "PY=py -3")
if not defined PY (
  where python >nul 2>nul && (python -c "import sys; sys.exit(0 if sys.version_info>=(3,8) else 1)" >nul 2>nul && set "PY=python")
)
if not defined PY if exist "%ROOT%\runtime\python\python.exe" set "PY="%ROOT%\runtime\python\python.exe""
if not defined PY if exist "%ROOT%\vendor\python\python-3.12.10-win-amd64.zip" (
  echo Python 3.8+ не найден. Распаковываю встроенный Python 3.12 из vendor\python ^(без интернета^)...
  if exist "%ROOT%\runtime\_unpack" rmdir /s /q "%ROOT%\runtime\_unpack"
  powershell -NoProfile -ExecutionPolicy Bypass -Command "Expand-Archive -LiteralPath '%ROOT%\vendor\python\python-3.12.10-win-amd64.zip' -DestinationPath '%ROOT%\runtime\_unpack' -Force"
  if exist "%ROOT%\runtime\_unpack\tools\python.exe" (
    if exist "%ROOT%\runtime\python" rmdir /s /q "%ROOT%\runtime\python"
    move "%ROOT%\runtime\_unpack\tools" "%ROOT%\runtime\python" >nul
    rmdir /s /q "%ROOT%\runtime\_unpack"
  )
  if exist "%ROOT%\runtime\python\python.exe" set "PY="%ROOT%\runtime\python\python.exe""
)
if not defined PY (
  echo Python 3.8+ не найден и встроенный Python недоступен. Пробую установить через winget ^(нужен интернет^)...
  where winget >nul 2>nul
  if errorlevel 1 (
    echo ОШИБКА: winget недоступен. Установите Python 3.8+ с https://www.python.org/downloads/ ^(отметьте "Add python.exe to PATH"^) и запустите скрипт снова.
    exit /b 1
  )
  winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
  rem PATH в текущем окне не обновился - ищем python в стандартном месте установки
  for %%D in ("%LocalAppData%\Programs\Python\Python312\python.exe") do if exist %%D set "PY=%%~D"
  if not defined PY (
    echo Python установлен, но окно нужно перезапустить. Закройте это окно и запустите install.bat ещё раз.
    exit /b 1
  )
  set "PY="!PY!""
)
%PY% --version

echo.
echo ==^> Создание виртуального окружения
if not exist "venv\Scripts\python.exe" (
  %PY% -m venv venv
  if errorlevel 1 ( echo ОШИБКА: не удалось создать venv & exit /b 1 )
)
echo ==^> Установка зависимостей из vendor\wheels ^(без интернета^)
if not exist "%ROOT%\vendor\wheels" ( echo ОШИБКА: нет папки vendor\wheels с библиотеками. Скопируйте проект целиком. & exit /b 1 )
"venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q --no-index --find-links "%ROOT%\vendor\wheels" -r requirements.txt
if errorlevel 1 ( echo ОШИБКА: не удалось установить зависимости & exit /b 1 )
if not exist data mkdir data

if /i "%~1"=="--no-autostart" (
  echo.
  echo Установка завершена ^(автозапуск пропущен^). Запуск вручную: start.bat
  exit /b 0
)

echo.
echo ==^> Настройка автозапуска ^(планировщик заданий, при входе в Windows^)
schtasks /Delete /TN "Pererabotki" /F >nul 2>nul
schtasks /Create /TN "Pererabotki" /SC ONLOGON /RL LIMITED /F /TR "cmd /c set PERER_PORT=%PERER_PORT%& set PERER_HOST=%PERER_HOST%& \"%ROOT%\venv\Scripts\pythonw.exe\" \"%ROOT%\app\server.py\""
if errorlevel 1 (
  echo ПРЕДУПРЕЖДЕНИЕ: не удалось создать задачу автозапуска. Запускайте start.bat вручную.
) else (
  echo Автозапуск настроен: задача "Pererabotki"
)

echo.
echo ==^> Запуск сервиса
rem Если сервис уже запущен прошлой установкой - останавливаем, чтобы не занимать порт дважды
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe'\" | Where-Object { $_.CommandLine -like '*app\server.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }" >nul 2>nul
start "" /B "%ROOT%\venv\Scripts\pythonw.exe" "%ROOT%\app\server.py"

set "TRIES=0"
:wait
set /a TRIES+=1
powershell -NoProfile -Command "try { (Invoke-WebRequest -UseBasicParsing 'http://127.0.0.1:%PERER_PORT%/api/me' -TimeoutSec 1).StatusCode } catch { exit 1 }" >nul 2>nul
if not errorlevel 1 goto ok
if %TRIES% GEQ 20 (
  echo ОШИБКА: сервис не ответил за 10 секунд. Смотрите data\server.log
  exit /b 1
)
timeout /t 1 /nobreak >nul
goto wait

:ok
echo.
echo Готово! Сервис работает: http://127.0.0.1:%PERER_PORT%
start "" "http://127.0.0.1:%PERER_PORT%"
endlocal
