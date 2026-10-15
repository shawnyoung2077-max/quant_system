@echo off

chcp 65001 >nul

cd /d "D:\26050\Documents\quant_system"

"C:\Users\26050\AppData\Local\Programs\Python\Python311\python.exe" -m polytrade.ensureproxy >> "D:\26050\Documents\quant_system\polytrade_logs\proxy_up.log" 2>&1
