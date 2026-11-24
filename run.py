"""FishEye 一键启动：采集器 + 分析器 + 任务 worker + API 服务（含前端静态托管）。

    python run.py                # 全量启动
    python run.py --only api     # 只起 API
    python run.py --no-analytics # 不起分析器
    python run.py --no-worker    # 不起异步任务 worker（环境提取会一直排队）

Ctrl+C 会同时停止所有子进程。日志写入 logs/*.log。
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOGS = ROOT / "logs"
PY = sys.executable

PROCS: list[subprocess.Popen] = []


def spawn(name: str, args: list[str]) -> subprocess.Popen:
    LOGS.mkdir(exist_ok=True)
    fh = open(LOGS / f"{name}.log", "ab", buffering=0)
    fh.write(f"\n===== start {time.strftime('%Y-%m-%d %H:%M:%S')} {name} =====\n".encode())
    creation = 0
    if os.name == "nt":
        creation = subprocess.CREATE_NEW_PROCESS_GROUP  # 便于整组终止
    p = subprocess.Popen(
        [PY, "-u", *args],
        cwd=str(ROOT),
        stdout=fh,
        stderr=subprocess.STDOUT,
        creationflags=creation,
    )
    PROCS.append(p)
    print(f"  ▸ {name:<10} pid={p.pid}  -> logs/{name}.log")
    return p


def shutdown(*_a):
    print("\n正在停止 FishEye…")
    for p in PROCS:
        if p.poll() is None:
            try:
                if os.name == "nt":
                    p.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    p.terminate()
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
    deadline = time.time() + 8
    for p in PROCS:
        while p.poll() is None and time.time() < deadline:
            time.sleep(0.15)
        if p.poll() is None:
            p.kill()
    print("已停止。")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=["api", "collector", "analytics", "worker"], help="只启动单个组件")
    ap.add_argument("--no-analytics", action="store_true")
    ap.add_argument("--no-collector", action="store_true")
    ap.add_argument("--no-worker", action="store_true", help="不启动异步任务 worker")
    ap.add_argument("--region", help="采集区域（config/regions.json 的 key）")
    args = ap.parse_args()

    dist = ROOT / "frontend" / "dist"
    print("FishEye 渔眼 v0.1")
    print(f"  DB      : {ROOT / 'data' / 'fisheye.db'}")
    print(f"  前端    : {'已构建' if dist.exists() else '未构建（npm run build）'}")
    print("-" * 52)

    try:
        if args.only == "api":
            spawn("api", ["-m", "backend.app.main"])
        elif args.only == "collector":
            a = ["-m", "collector.main"] + (["--region", args.region] if args.region else [])
            spawn("collector", a)
        elif args.only == "analytics":
            spawn("analytics", ["-m", "analytics.worker"])
        elif args.only == "worker":
            spawn("worker", ["analytics/job_worker.py"])
        else:
            if not args.no_collector:
                a = ["-m", "collector.main"] + (["--region", args.region] if args.region else [])
                spawn("collector", a)
            if not args.no_analytics:
                spawn("analytics", ["-m", "analytics.worker"])
            if not args.no_worker:
                spawn("worker", ["analytics/job_worker.py"])
            spawn("api", ["-m", "backend.app.main"])
            host = os.environ.get("FISHEYE_HOST", "127.0.0.1")
            port = os.environ.get("FISHEYE_PORT", "8000")
            print("-" * 52)
            print(f"  打开浏览器: http://{host}:{port}")
            print("  Ctrl+C 停止全部\n")

        signal.signal(signal.SIGINT, shutdown)
        try:
            signal.signal(signal.SIGTERM, shutdown)
        except (ValueError, OSError):
            pass

        while True:
            for p in list(PROCS):
                if p.poll() is not None:
                    print(f"  ! 子进程 {p.pid} 已退出（code={p.returncode}），见 logs/")
                    PROCS.remove(p)
            if not PROCS:
                print("所有子进程已退出。")
                return 1
            time.sleep(2)
    except KeyboardInterrupt:
        shutdown()
        return 0


if __name__ == "__main__":
    sys.exit(main())
