"""FishEye 全局配置 —— 全部从环境变量 / .env 读取，缺失时用安全默认值。"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _env_int(key: str, default: int) -> int:
    try:
        return int(_env(key, str(default)))
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(_env(key, str(default)))
    except ValueError:
        return default


def _env_bool(key: str, default: bool) -> bool:
    return _env(key, "1" if default else "0").lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    # --- 数据源 ---
    api_key: str = field(default_factory=lambda: _env("AISSTREAM_API_KEY"))
    ws_url: str = "wss://stream.aisstream.io/v0/stream"
    bboxes: list = field(default_factory=lambda: _load_json("FISHEYE_BBOXES", [[[-90, -180], [90, 180]]]))

    # --- 存储 ---
    db_path: Path = field(default_factory=lambda: (ROOT / _env("FISHEYE_DB", "./data/fisheye.db")).resolve())
    archive_dir: Path = field(default_factory=lambda: (ROOT / _env("FISHEYE_ARCHIVE_DIR", "./data/archive")).resolve())
    archive_enabled: bool = field(default_factory=lambda: _env_bool("FISHEYE_ARCHIVE_ENABLED", True))
    archive_max_mb: int = field(default_factory=lambda: _env_int("FISHEYE_ARCHIVE_MAX_MB", 2048))
    min_save_interval: int = field(default_factory=lambda: _env_int("FISHEYE_MIN_SAVE_INTERVAL", 30))
    min_save_distance_m: float = field(default_factory=lambda: _env_float("FISHEYE_MIN_SAVE_DISTANCE_M", 100))
    retention_days: int = field(default_factory=lambda: _env_int("FISHEYE_RETENTION_DAYS", 30))

    # --- 采集器 ---
    flush_interval: float = field(default_factory=lambda: _env_float("FISHEYE_FLUSH_INTERVAL", 2.0))
    health_interval: float = field(default_factory=lambda: _env_float("FISHEYE_HEALTH_INTERVAL", 15.0))
    max_batch: int = field(default_factory=lambda: _env_int("FISHEYE_MAX_BATCH", 20000))
    log_level: str = field(default_factory=lambda: _env("FISHEYE_LOG_LEVEL", "INFO").upper())

    # --- 分析 ---
    analytics_interval: int = field(default_factory=lambda: _env_int("FISHEYE_ANALYTICS_INTERVAL", 120))
    gap_floor_sec: int = field(default_factory=lambda: _env_int("FISHEYE_GAP_FLOOR_SEC", 1800))
    gap_ceiling_sec: int = field(default_factory=lambda: _env_int("FISHEYE_GAP_CEILING_SEC", 6 * 3600))
    # 深度分析范围。默认跟随实际接收覆盖（全球），用 max_vessels 控制单轮成本。
    # 生产环境建议改成重点海域，例如：
    #   FISHEYE_ANALYTICS_BBOXES=[[[24,119],[33.5,128]],[[32,119],[41,127]]]
    analytics_bboxes: list = field(
        default_factory=lambda: _load_json("FISHEYE_ANALYTICS_BBOXES", [[[-90, -180], [90, 180]]])
    )
    analytics_window_hours: int = field(default_factory=lambda: _env_int("FISHEYE_ANALYTICS_WINDOW_HOURS", 6))
    analytics_max_vessels: int = field(default_factory=lambda: _env_int("FISHEYE_ANALYTICS_MAX_VESSELS", 1500))

    # --- 服务 ---
    host: str = field(default_factory=lambda: _env("FISHEYE_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: _env_int("FISHEYE_PORT", 8000))

    # --- 智能体（可选） ---
    llm_base_url: str = field(default_factory=lambda: _env("LLM_BASE_URL", "https://api.deepseek.com/v1"))
    llm_api_key: str = field(default_factory=lambda: _env("LLM_API_KEY"))
    llm_model: str = field(default_factory=lambda: _env("LLM_MODEL", "deepseek-chat"))

    @property
    def llm_enabled(self) -> bool:
        return bool(self.llm_api_key)

    def ensure_dirs(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.archive_dir.mkdir(parents=True, exist_ok=True)


def _load_json(key: str, default):
    raw = _env(key)
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default


settings = Settings()
