@echo off
title 邮件中心系统
cd /d "%~dp0"

echo 正在检查依赖...
python -c "import flask, cryptography" 2>nul
if errorlevel 1 (
  echo 首次运行，正在安装依赖（需联网）...
  pip install -r requirements.txt
)

echo.
echo  启动邮件中心系统...
echo  浏览器将自动打开 http://127.0.0.1:8000
echo  保持本窗口开启即为系统运行中，关闭本窗口即停止后台调度。
echo.

start "" "http://127.0.0.1:8000"
python mailcenter.py web

pause
