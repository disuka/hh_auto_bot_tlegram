@echo off
title Telegram Bot (piston.py)
echo Запускаю бота локально...
echo.

cd /d "%~dp0"

if exist .venv\Scripts\activate (
    call .venv\Scripts\activate
    echo Виртуальное окружение активировано
) else (
    echo Предупреждение: виртуальное окружение не найдено, использую системный Python
)

python piston.py

if errorlevel 1 (
    echo.
    echo Ошибка при выполнении скрипта. Проверьте конфигурацию.
    pause
)