"""FishEye API 服务。

开发：uvicorn backend.app.main:app --reload --port 8000
生产：python -m backend.app.main   （若已 build 前端，会同时托管静态文件）
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, ORJSONResponse
from fastapi.staticfiles import StaticFiles

from core.config import ROOT, settings
from core.db import init_db

from .api import agent as agent_api
from .api import analysis as analysis_api
from .api import map as map_api
from .api import meta as meta_api
from .services.realtime_hub import vessel_stream

logging.basicConfig(
    level=getattr(logging, settings.log_level, logging.INFO),
    format="%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("api")

DIST = ROOT / "frontend" / "dist"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    from analytics.geofence_detector import seed_zones
    from core.db import connect

    conn = connect()
    try:
        n = seed_zones(conn)
        conn.commit()
    finally:
        conn.close()
    log.info("FishEye 启动完成 | DB=%s | 围栏 %s 个 | LLM=%s", settings.db_path, n, settings.llm_enabled)
    yield


app = FastAPI(
    title="FishEye API",
    version="0.1.0",
    lifespan=lifespan,
    default_response_class=ORJSONResponse,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(map_api.router)
app.include_router(analysis_api.router)
app.include_router(meta_api.router)
app.include_router(agent_api.router)


@app.websocket("/ws/vessels")
async def ws_vessels(ws: WebSocket):
    await vessel_stream(ws)


if DIST.exists():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

    @app.get("/")
    def index():
        return FileResponse(DIST / "index.html")

    @app.get("/{path:path}")
    def spa(path: str):
        f = DIST / path
        if f.exists() and f.is_file():
            return FileResponse(f)
        return FileResponse(DIST / "index.html")
else:
    # 干净克隆时前端还没构建。直接给一句人话，别让用户对着 404 猜。
    @app.get("/")
    def frontend_missing():
        return HTMLResponse(
            "<h2>前端尚未构建</h2>"
            "<p>在项目根目录执行：</p>"
            "<pre>cd frontend &amp;&amp; npm ci &amp;&amp; npm run build</pre>"
            "<p>然后重启服务即可。接口文档在 <a href='/docs'>/docs</a>，"
            "健康检查在 <a href='/api/health'>/api/health</a>。</p>",
            status_code=503,
        )


def _port_free(host: str, port: int) -> bool:
    """先自己试着 bind 一次。uvicorn 抢不到端口时只打一行 Errno 10048 就退出 3，
    用户看到的是「启动完成」紧接着「进程没了」，很容易误判成代码坏了。"""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def main() -> None:
    import uvicorn

    if not _port_free(settings.host, settings.port):
        log.error(
            "端口 %s:%s 已被占用，无法启动。\n"
            "  · 换端口：FISHEYE_PORT=8001 python run.py\n"
            "  · 或找占用者：Windows 用 netstat -ano | findstr :%s 再 taskkill /PID <pid> /F；\n"
            "               macOS/Linux 用 lsof -i :%s\n"
            "  · 也可能已经有一个 FishEye 在跑，先看浏览器能不能打开 http://%s:%s",
            settings.host, settings.port, settings.port, settings.port, settings.host, settings.port,
        )
        raise SystemExit(3)

    uvicorn.run(
        "backend.app.main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
