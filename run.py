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
NAMES: dict[int, str] = {}   # pid -> 组件名，退出时要靠它说人话
PATHS: dict[int, Path] = {}  # pid -> 该组件的日志文件

# 退出码不是一个够用的信息：用户看到「code=2」不知道要改什么。
# 这里把已知的失败原因直接翻译成人话，再附上日志尾巴。
HINTS: dict[tuple[str, int | None], str] = {
    ("collector", 2): "采集器没起来：多半是 .env 里 AISSTREAM_API_KEY 没填，或 --region 写错（见上面日志最后一行）",
    ("api", 3): "API 没起来：端口被占用。换一个端口（FISHEYE_PORT=8001 python run.py）或先关掉占用它的进程",
    ("api", 1): "API 没起来：看上面日志最后的 Traceback",
}


def _tail(path: Path | None, n: int = 12) -> list[str]:
    """读「本次启动」之后的日志尾巴。

    日志是追加写的，所以要先跳到最后一个 ===== start 之后，否则用户会看到上一次
    运行的报错，越看越糊涂。
    """
    if path is None:
        return []
    try:
        raw = path.read_text("utf-8", errors="replace").splitlines()
    except OSError:
        return []
    start = 0
    for i, ln in enumerate(raw):
        if ln.startswith("===== start"):
            start = i + 1
    lines = [ln.rstrip() for ln in raw[start:] if ln.strip()]
    return lines[-n:]


def spawn(name: str, args: list[str]) -> subprocess.Popen:
    LOGS.mkdir(exist_ok=True)
    log_path = LOGS / f"{name}.log"
    fh = open(log_path, "ab", buffering=0)
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
    NAMES[p.pid] = name
    PATHS[p.pid] = log_path
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
    # 低于 3.10 会在 import 阶段抛 SyntaxError，报错位置毫无提示性，不如在这里说清楚
    if sys.version_info < (3, 10):
        print(f"需要 Python ≥ 3.10，当前是 {sys.version.split()[0]}")
        print("建议 3.11 或 3.13（CI 只在这两个版本上验证过）。")
        return 2
    if sys.version_info[:2] == (3, 10):
        print("提示：CI 只验证过 3.11 / 3.13，3.10 上如遇问题请先升级 Python。")

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
                    name = NAMES.get(p.pid, str(p.pid))
                    print(f"  ! {name} 已退出（code={p.returncode}）")
                    for ln in _tail(PATHS.get(p.pid)):
                        print(f"  │ {ln}")
                    hint = HINTS.get((name, p.returncode))
                    if hint:
                        print(f"  → {hint}")
                    else:
                        print(f"  → 上面是该进程日志的末尾，完整日志：logs/{name}.log")
                    PROCS.remove(p)
                    if PROCS:
                        print(f"  → 其余 {len(PROCS)} 个组件仍在运行")
            if not PROCS:
                print("所有子进程已退出。")
                return 1
            time.sleep(2)
    except KeyboardInterrupt:
        shutdown()
        return 0


if __name__ == "__main__":
    sys.exit(main())
