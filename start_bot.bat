@echo off
cd /d "c:\Users\wjlee\OneDrive - MUS\MUS-WJLEE\MUS Advisory\auto_trading_live"
set KIS_MODE=live
start /min "" "C:\Users\wjlee\AppData\Local\Python\pythoncore-3.14-64\python.exe" telegram_bot.py
