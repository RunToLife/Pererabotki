@echo off
rem Автоматическая установка и запуск сервиса "Переработка" (Windows).
rem   install.bat                 установить и запустить (автозапуск при входе в Windows НЕ настраивается)
rem   install.bat --no-start      только установить зависимости, не запускать (--no-autostart - то же самое)
rem   set PERER_PORT=9000 ^& install.bat   другой порт (по умолчанию 8080)
chcp 65001 >nul
rem Отложенное раскрытие (!) не включаем: путь к папке может содержать "!"
setlocal
cd /d "%~dp0"
set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
if not defined PERER_PORT set "PERER_PORT=8080"
if not defined PERER_HOST set "PERER_HOST=127.0.0.1"

echo.
echo ==^> Проверка Python
rem PY - полный путь к python.exe без кавычек; везде используется как "%PY%".
rem Кавычки внутри значения ломали скрипт, если в пути к папке есть скобки, например "Загрузки\Pererabotki-main (1)".
set "PY="
for /f "delims=" %%P in ('py -3 -c "import sys; assert sys.hexversion >= 0x3080000; print(sys.executable)" 2^>nul') do set "PY=%%P"
if not defined PY for /f "delims=" %%P in ('python -c "import sys; assert sys.hexversion >= 0x3080000; print(sys.executable)" 2^>nul') do set "PY=%%P"
if defined PY if not exist "%PY%" set "PY="
if not defined PY if exist "%ROOT%\runtime\python\python.exe" set "PY=%ROOT%\runtime\python\python.exe"
if not defined PY if exist "%ROOT%\vendor\python\python-3.12.10-win-amd64.zip" (
  echo Python 3.8+ не найден. Распаковываю встроенный Python 3.12 из vendor\python ^(без интернета^)...
  if exist "%ROOT%\runtime\_unpack" rmdir /s /q "%ROOT%\runtime\_unpack"
  powershell -NoProfile -ExecutionPolicy Bypass -Command "Expand-Archive -LiteralPath '%ROOT%\vendor\python\python-3.12.10-win-amd64.zip' -DestinationPath '%ROOT%\runtime\_unpack' -Force"
  if exist "%ROOT%\runtime\_unpack\tools\python.exe" (
    if exist "%ROOT%\runtime\python" rmdir /s /q "%ROOT%\runtime\python"
    move "%ROOT%\runtime\_unpack\tools" "%ROOT%\runtime\python" >nul
    rmdir /s /q "%ROOT%\runtime\_unpack"
  )
  if exist "%ROOT%\runtime\python\python.exe" set "PY=%ROOT%\runtime\python\python.exe"
)
if not defined PY (
  echo Python 3.8+ не найден и встроенный Python недоступен. Пробую установить через winget ^(нужен интернет^)...
  where winget >nul 2>nul
  if errorlevel 1 (
    echo ОШИБКА: winget недоступен. Установите Python 3.8+ с https://www.python.org/downloads/ ^(отметьте "Add python.exe to PATH"^) и запустите скрипт снова.
    goto :fail
  )
  winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
  rem PATH в текущем окне не обновился - ищем python в стандартном месте установки
  for %%D in ("%LocalAppData%\Programs\Python\Python312\python.exe") do if exist %%D set "PY=%%~D"
  if not defined PY (
    echo Python установлен, но окно нужно перезапустить. Закройте это окно и запустите install.bat ещё раз.
    goto :fail
  )
)
"%PY%" --version

echo.
echo ==^> Создание виртуального окружения
if not exist "venv\Scripts\python.exe" (
  "%PY%" -m venv venv
  if errorlevel 1 ( echo ОШИБКА: не удалось создать venv & goto :fail )
)
echo ==^> Установка зависимостей из vendor\wheels ^(без интернета^)
if not exist "%ROOT%\vendor\wheels" ( echo ОШИБКА: нет папки vendor\wheels с библиотеками. Скопируйте проект целиком. & goto :fail )
"venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q --no-index --find-links "%ROOT%\vendor\wheels" -r requirements.txt
if errorlevel 1 ( echo ОШИБКА: не удалось установить зависимости & goto :fail )
if not exist data mkdir data

set "NOSTART="
if /i "%~1"=="--no-start" set "NOSTART=1"
if /i "%~1"=="--no-autostart" set "NOSTART=1"

rem Автозапуск отключён: задачу в Планировщике больше не создаём.
rem Задачу "Pererabotki", оставшуюся от прошлых версий установщика, удаляем.
schtasks /Query /TN "Pererabotki" >nul 2>nul
if not errorlevel 1 (
  schtasks /Delete /TN "Pererabotki" /F >nul 2>nul
  echo Удалена задача автозапуска "Pererabotki" от прошлой установки.
)

if defined NOSTART (
  echo.
  echo Установка завершена ^(сервис не запущен^). Запуск вручную: start.bat
  exit /b 0
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
if %TRIES% GEQ 30 (
  echo ОШИБКА: сервис не ответил за 30 секунд.
  echo Чтобы увидеть ошибку запуска, выполните start.bat в этой же папке.
  goto :fail
)
timeout /t 1 /nobreak >nul
goto wait

:ok
echo.
rem localhost (а не 127.0.0.1) браузеры относят к зоне «Местная интрасеть» и сами передают учётку Windows
echo Готово. Сервис работает: http://localhost:%PERER_PORT%
echo Настройки входа по учётной записи Windows/AD: файл settings.env и README.md
start "" "http://localhost:%PERER_PORT%"
endlocal
exit /b 0

:fail
rem При двойном щелчке окно иначе закроется сразу и текст ошибки не будет виден
echo.
if exist "%ROOT%\data\server.log" (
  echo --- Последние строки data\server.log ---
  powershell -NoProfile -Command "Get-Content -Tail 20 -Encoding UTF8 -LiteralPath '%ROOT%\data\server.log'"
)
echo.
echo Установка не завершена. Пришлите текст из этого окна разработчику.
pause
exit /b 1
