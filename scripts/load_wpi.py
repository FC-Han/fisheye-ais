#!/usr/bin/env python3
"""把 World Port Index（NGA WPI，shapefile 的 .dbf 部分）导入 ports 表。

用法：
    .venv/Scripts/python.exe scripts/load_wpi.py [路径]

默认读 data/ports/WPI.dbf（由 world_port_index.zip 解压得到）。

只做一件事：把 3669 个港口点位的经纬度、名称、国家、港口规模/类型写进 SQLite。
不依赖 pyshp / geopandas —— WPI 的 .dbf 是定长记录，直接按字段偏移切片即可。
"""
from __future__ import annotations

import io
import struct
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.db import connect, init_db  # noqa: E402

# WPI 的编码是单字母/双字母代码，这里翻译成中文，原始码保留在括号里方便回溯
HARBOR_SIZE = {
    "V": "很小",
    "S": "小",
    "M": "中",
    "L": "大",
}
HARBOR_TYPE = {
    "CN": "海岸天然",
    "CB": "海岸防波堤",
    "CT": "海岸潮闸",
    "LC": "湖泊/运河",
    "RN": "河流天然",
    "RB": "河流港池",
    "RT": "河流潮闸",
    "OR": "开敞锚地",
    "TR": "避风锚地",
}

# 我们实际需要的字段，其余 200+ 个字段（引航、加油、起重能力等）不导入
WANT = (
    "INDEX_NO",
    "REGION_NO",
    "PORT_NAME",
    "COUNTRY",
    "LATITUDE",
    "LONGITUDE",
    "LAT_DEG",
    "LAT_MIN",
    "LAT_HEMI",
    "LONG_DEG",
    "LONG_MIN",
    "LONG_HEMI",
    "HARBORSIZE",
    "HARBORTYPE",
    "SHELTER",
)


def _open_src(src) -> io.BufferedReader | io.BytesIO:
    """统一打开方式：磁盘路径直接开，内存字节包成 BytesIO。"""
    if isinstance(src, (bytes, bytearray)):
        return io.BytesIO(bytes(src))
    return Path(src).open("rb")


def parse_dbf(src) -> list[dict]:
    """解析 dBase III/IV 文件，返回记录 dict 列表。

    src 可以是 Path，也可以是 dbf 的原始字节（zip 内文件用）。
    """
    with _open_src(src) as f:
        header = f.read(32)
        if len(header) < 32:
            raise ValueError(f"{src} 不是有效的 dbf")
        nrec = struct.unpack("<I", header[4:8])[0]
        hlen = struct.unpack("<H", header[8:10])[0]
        rlen = struct.unpack("<H", header[10:12])[0]

        fields: list[tuple[str, int]] = []
        while True:
            blob = f.read(32)
            if not blob or blob[0:1] == b"\x0d":
                break
            name = blob[0:11].split(b"\x00")[0].decode("latin1").strip()
            length = blob[16]
            fields.append((name, length))

        # 记录区从第 1 字节开始（第 0 字节是删除标记），逐字段累加偏移
        offset = 1
        layout: dict[str, tuple[int, int]] = {}
        for name, length in fields:
            layout[name] = (offset, length)
            offset += length
        if offset != rlen:
            raise ValueError(f"字段总长 {offset} 与记录长 {rlen} 不一致，文件可能损坏")

        f.seek(hlen)
        out: list[dict] = []
        for _ in range(nrec):
            rec = f.read(rlen)
            if len(rec) < rlen:
                break
            if rec[0:1] == b"*":  # 已删除记录
                continue
            row: dict[str, str] = {}
            for name in WANT:
                if name not in layout:
                    continue
                off, length = layout[name]
                row[name] = rec[off : off + length].decode("latin1").strip()
            out.append(row)
        return out


def _fnum(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def resolve_coords(row: dict) -> tuple[float, float] | None:
    """优先用 LATITUDE/LONGITUDE（已带符号的十进制度）。

    缺失时退回 度+分+半球 组合换算，再不行就丢掉这条记录。
    """
    lat = _fnum(row.get("LATITUDE"))
    lon = _fnum(row.get("LONGITUDE"))
    if lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180:
        return lat, lon

    lat_d = _fnum(row.get("LAT_DEG"))
    lat_m = _fnum(row.get("LAT_MIN")) or 0.0
    lon_d = _fnum(row.get("LONG_DEG"))
    lon_m = _fnum(row.get("LONG_MIN")) or 0.0
    if lat_d is None or lon_d is None:
        return None
    lat = lat_d + lat_m / 60.0
    lon = lon_d + lon_m / 60.0
    if row.get("LAT_HEMI") == "S":
        lat = -abs(lat)
    if row.get("LONG_HEMI") == "W":
        lon = -abs(lon)
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return lat, lon


ZIP_FALLBACK = ROOT / "world_port_index.zip"


def default_source() -> Path | None:
    """找可用的 WPI 数据：优先已解压的 dbf，其次仓库根目录自带的 zip。

    克隆后不需要手动 unzip —— zip 里就是同一个 dbf。
    """
    dbf = ROOT / "data" / "ports" / "WPI.dbf"
    if dbf.exists():
        return dbf
    if ZIP_FALLBACK.exists():
        return ZIP_FALLBACK
    return None


def read_records(src: Path) -> list[dict]:
    """从 .dbf 或含 dbf 的 .zip 读出记录。"""
    if Path(src).suffix.lower() == ".zip":
        with zipfile.ZipFile(src) as z:
            name = next((n for n in z.namelist() if n.lower().endswith(".dbf")), None)
            if name is None:
                raise ValueError(f"{src} 里没有 .dbf 文件")
            return parse_dbf(z.read(name))
    return parse_dbf(src)


def import_ports(src: Path | str | None = None) -> tuple[int, int]:
    """把 WPI 港口写进 ports 表，返回 (成功条数, 跳过条数)。

    可被 ports.py 首次查询时自动调用，因此这里不打印、不抛业务异常，
    只用返回值表达结果。
    """
    src = Path(src) if src is not None else default_source()
    if src is None:
        raise FileNotFoundError(
            "找不到港口数据：既没有 data/ports/WPI.dbf，也没有 world_port_index.zip"
        )
    rows = read_records(src)
    init_db()
    now = int(time.time())
    good = skipped = 0
    with connect() as conn:
        conn.execute("DELETE FROM ports")
        batch = []
        for row in rows:
            coords = resolve_coords(row)
            port_id = _fnum(row.get("INDEX_NO"))
            name = (row.get("PORT_NAME") or "").strip()
            if coords is None or port_id is None or not name:
                skipped += 1
                continue
            lat, lon = coords
            size_code = (row.get("HARBORSIZE") or "").strip().upper()
            type_code = (row.get("HARBORTYPE") or "").strip().upper()
            batch.append(
                (
                    int(port_id),
                    name,
                    (row.get("COUNTRY") or "").strip() or None,
                    round(lat, 6),
                    round(lon, 6),
                    int(_fnum(row.get("REGION_NO")) or 0),
                    HARBOR_SIZE.get(size_code, size_code or None),
                    HARBOR_TYPE.get(type_code, type_code or None),
                    size_code or None,
                    type_code or None,
                    (row.get("SHELTER") or "").strip() or None,
                    "WPI",
                    now,
                )
            )
            good += 1

        conn.executemany(
            """INSERT OR REPLACE INTO ports
               (port_id, name, country, lat, lon, region_no, harbor_size, harbor_type,
                harbor_size_code, harbor_type_code, shelter, source, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            batch,
        )
        conn.commit()
    return good, skipped


def main() -> int:
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else default_source()
    if src is None or not Path(src).exists():
        print(f"[x] 找不到港口数据：{src}")
        print("    world_port_index.zip 应放在项目根目录（本仓库已自带）")
        return 1

    print(f"[i] 读取 {src}")
    good, skipped = import_ports(src)
    print(f"[✓] 导入 {good} 个港口，跳过 {skipped} 条（坐标或名称缺失）")

    with connect(readonly=True) as conn:
        for label, sql in (
            ("总数", "SELECT COUNT(*) FROM ports"),
            ("有国家", "SELECT COUNT(*) FROM ports WHERE country IS NOT NULL"),
            ("有规模", "SELECT COUNT(*) FROM ports WHERE harbor_size IS NOT NULL"),
        ):
            print(f"    {label}: {conn.execute(sql).fetchone()[0]}")
        print("    样例（北大西洋附近）:")
        for r in conn.execute(
            "SELECT name, country, lat, lon, harbor_size, harbor_type FROM ports "
            "WHERE lat BETWEEN 50 AND 55 AND lon BETWEEN -6 AND 3 ORDER BY harbor_size DESC LIMIT 5"
        ):
            print(f"      {r['name']:<22} {r['country']} {r['lat']:.3f},{r['lon']:.3f} "
                  f"{r['harbor_size']} {r['harbor_type']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
