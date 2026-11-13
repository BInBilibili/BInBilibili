@echo off
rem 启动 FPS 悬浮窗（无控制台窗口）
cd /d "%~dp0"
where pythonw >nul 2>nul
if errorlevel 1 (
    start "" python "%~dp0fps_overlay.pyw"
) else (
    start "" pythonw "%~dp0fps_overlay.pyw"
)
