@echo off
REM ============================================================================
REM  tickflow-stock-panel — 后端启动脚本 (Windows)
REM ----------------------------------------------------------------------------
REM  用途: 在本机启动 FastAPI 后端 (默认 http://localhost:3018)
REM
REM  重要: 回测/优化任务默认用「进程内 (in-process)」模式运行,
REM        以规避 Windows 下 uvicorn 父进程 + multiprocessing.spawn 子进程
REM        在 re-exec 时崩溃 (exitcode=1, 无报错) 的问题。
REM        如需恢复上游的 spawn 隔离模式, 删除下面这行 set 即可
REM        (或 set TICKFLOW_BACKTEST_MODE=spawn)。
REM ============================================================================

set TICKFLOW_BACKTEST_MODE=inprocess

cd /d %~dp0backend
if not exist ".venv\Scripts\python.exe" (
    echo [错误] 未找到 backend/.venv, 请先按 README 用 venv + pip 安装依赖。
    pause
    exit /b 1
)

echo [tickflow] 启动后端 (backtest mode=%TICKFLOW_BACKTEST_MODE%)  ->  http://localhost:3018
.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 3018
