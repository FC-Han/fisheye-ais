# 排错手册

每一条都是**真的跑出来的**：症状一栏是实际打印的原文（或原文的缩短版），不是推测。
如果你遇到的情况不在这里，跳到最后一节，那里列了需要提供什么信息。

先做一次自检，很多时候答案已经在里面：

```bash
curl http://127.0.0.1:8000/api/health            # 服务活着吗
curl http://127.0.0.1:8000/api/stream/status     # 还在收数据吗
curl http://127.0.0.1:8000/api/analysis/readiness # 各分析能力为什么不可用
```

---

## 1. 装依赖

### 1.1 `pip install -r requirements.txt` 跑了十几分钟还没结束

**不是卡死。** 这套依赖要下 100 MB 以上（numpy / pandas / netCDF4 / h5py / eccodes /
copernicusmarine 及其 boto3 依赖）。实测在校园网 + 走代理的情况下，10 分钟才下到 103 MB，
全程约 170 KB/s。

处理：等着；或者换国内镜像重来一次更快：

```bash
python -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

判断「真的在下载」还是「真的死了」：看 pip 的临时目录体积是否在涨

```bash
# Windows
dir %TEMP%\pip-unpack-*
# macOS / Linux
du -sh /tmp/pip-unpack-*
```

### 1.2 一启动就 `SyntaxError`，或者 `TypeError: unsupported operand type(s) for |`

Python 版本太老。项目用到 3.10 起的语法。用 3.11 或 3.13（CI 只验证这两个版本）。

```bash
python -V
```

现在 `run.py` 会在入口处先检查版本并直接说明，不再让你对着 Traceback 猜。

---

## 2. 启动

### 2.1 `collector 已退出（code=2）`，日志里是「缺少 AISSTREAM_API_KEY」

`.env` 里没填 key，或者文件根本没被读到。

```bash
# 1) 文件到底在不在、名字对不对
# Windows
dir /a .env*
# macOS / Linux
ls -la .env
```

**最常见的坑**：用记事本另存为时文件名变成了 `.env.txt`（Windows 默认隐藏扩展名），
看起来是 `.env`，实际读不到。用 `copy .env.example .env` 生成，别手动新建。

### 2.2 日志显示「已连接 AISStream」，紧接着「连接异常：no close frame received or sent」，一直重试

**key 无效**（写错、少几位、带了空格或引号）。AISStream 对无效 key 的处理是：
WebSocket 握手成功 → 立刻关闭连接 → 不给任何错误说明。所以日志看起来像网络抖动，
其实是认证失败。

实测现象（一个故意写错的 40 位 key）：

```
INFO  [collector] 已连接 AISStream，订阅区域：全球
WARNING [collector] 连接异常：no close frame received or sent，1s 后重试
```

现在连续两次「连上不到 20 秒、一条消息都没收到」会直接给出结论：

```
ERROR [collector] 连续两次「刚连上就被服务器断开，且一条消息都没收到」。最常见的原因是
AISSTREAM_API_KEY 无效：到 aisstream.io 重新复制完整的 key（40 位十六进制，注意不要带空格
或引号）写进 .env 后重启。若 key 确认无误，再检查订阅区域 BoundingBoxes 是否为空、经纬度
有没有写反（先纬度后经度）。
```

### 2.3 `api 已退出（code=3）`

端口被占用。旧版只会打一行 `ERROR: [Errno 10048] error while attempting to bind on
address ('127.0.0.1', 8000)` 然后退出，看起来像代码崩了；现在会直接告诉你怎么办：

```
ERROR [api] 端口 127.0.0.1:8000 已被占用，无法启动。
  · 换端口：FISHEYE_PORT=8001 python run.py
  · 或找占用者：Windows 用 netstat -ano | findstr :8000 再 taskkill /PID <pid> /F
  · 也可能已经有一个 FishEye 在跑
```

注意：如果屏幕上出现 **两份** `▸ api pid=...`，说明你启动了两次，第二次会抢不到端口。

### 2.4 浏览器打开是「前端尚未构建」（HTTP 503）

`dist` 不在版本库里，必须构建一次：

```bash
cd frontend && npm ci && npm run build && cd ..
```

需要 Node ≥ 18。实测 `npm ci` 装 107 个包约 13 秒，`vite build` 约 4 秒，产物 7.5 MB。

---

## 3. 服务能打开，但没有数据

### 3.1 先看 `/api/stream/status`

这是判断「数据在不在流」的唯一权威，字段含义：

| 字段 | 含义 |
|---|---|
| `connected` | 采集器当前是否连着 AISStream |
| `collector_alive` | 采集器进程最近一次心跳是否在合理时间内 |
| `seconds_since_message` | 距上一条 AIS 消息多少秒；超过几分钟就不正常 |
| `rate_1m` | 最近一分钟消息速率（条/秒） |
| `msgs_total` | 本次启动以来收到的总条数 |
| `db_size_mb` | 数据库大小 |

### 3.2 真实案例：页面能打开，地图上一条船都没有

我自己的机器就踩过：API 和 worker 都活着，前端照常显示，但 `/api/stream/status` 返回

```json
{"connected": false, "collector_alive": false, "seconds_since_message": 1721235 }
```

`1721235` 秒 ≈ **20 天** —— 采集器早在 20 天前就退了，页面显示的是 20 天前的旧数据。
`vessels_live_30m` 是 `0`。

**结论：前端能打开 ≠ 数据在更新。** 判断标准永远是 `seconds_since_message`。

恢复：

```bash
python run.py            # 重新起全栈
# 或者只补采集器
python run.py --only collector --region east_china
```

### 3.3 `connected: true` 但 `rate_1m` 长期是 `0`

有连接、没数据，按顺序查：

1. **订阅区域里真的没有船**：`FISHEYE_BBOXES` 写的是远洋空白区，或者小到不覆盖任何航道。
2. **经纬度顺序写反**：AISStream 的 bbox 是 `[[纬度下限, 经度下限], [纬度上限, 经度上限]]`，
   先纬度后经度。写成先经度后纬度不会报错，只会静默地收不到东西。
3. **动过 `FilterMessageTypes`**：默认订阅里包含 `PositionReport`，如果你改
   `collector/main.py` 时把它删了，只剩静态报文，地图自然不会动。

改完 `.env` 重启 `run.py` 即可，不需要清库。

---

## 4. 环境数据（海温 / 浪高 / 风 / 叶绿素）

### 4.1 `/api/analysis/ocean/capabilities` 报 `available: false`

看 `reason` 字段，只有两种可能：

| `reason` 原文 | 处理 |
|---|---|
| `未找到 Copernicus Marine 登录凭证（~/.copernicusmarine/.copernicusmarine-credentials）` | 命令行登录一次：`.venv/Scripts/python -m copernicusmarine login`（macOS/Linux 用 `.venv/bin/python`）。**不是设环境变量** |
| `缺少依赖: numpy, xarray, ...` | 没装环境引擎的依赖，重新 `pip install -r requirements.txt` |

这是**如实降级**，不是故障：其它分析（行为、航次、港口）照常用。

### 4.2 `credentials_found: true`，但提取任务失败并返回 403

CDS（ERA5）那一套和 Copernicus Marine 是两套独立凭证。CDS 的 key 光填进 `.env`
不够，必须去数据集页面点「接受许可」，否则每次请求都是 403。

只在用 `wind_era5` 这个产品时才需要 CDS；默认风源是 NOAA GFS，**不需要任何 key**。

### 4.3 叶绿素为什么是几天/几年前的数

BGC 近实时产品不提供叶绿素，所以只能用 `cmems_mod_glo_bgc_my_0.25deg_P1M-m`
构造 2019–2023 同月气候态。返回里 `chl_basis: "climatology"`、`lag_hours: null`
就是在说这件事。它回答的是「这个季节通常是多少」，不是「今天是多少」。

### 4.4 风速为什么是几小时前

默认风源 `wind_gfs` 是 NOAA GFS 的分析场 + 3 小时内短时预报，滞后约 5 小时，
`wind_basis` 会写明。要真实观测就换 `wind`（卫星散射计 L4，滞后约 2 天），
要历史回填用 `wind_era5`（滞后约 5 天）。

---

## 5. 分析功能显示不可用

`/api/analysis/readiness` 的 `capabilities` 里 `available: false` 是**数据不足**，不是坏了：

| 能力 | 条件 |
|---|---|
| `trip_reconstruction` | 需要至少一次连续 2 小时静止作为航次边界 |
| `vessel_dna` | 需要单船 ≥ 3 个已闭合航次，按近海渔船 1–3 天/航次算，约需 3–9 天连续覆盖 |

刚跑起来的库一定是 `vessel_dna: available: false`，当前 0 个航次。等数据攒够它会自己变 true，
不需要改配置。

---

## 6. 还搞不定

带这三样东西去提问，能省掉一半来回：

```bash
curl -s http://127.0.0.1:8000/api/stream/status
curl -s http://127.0.0.1:8000/api/analysis/readiness
tail -30 logs/collector.log
```

`logs/` 下按组件分文件（`collector.log` / `api.log` / `analytics.log` / `worker.log`），
`run.py` 在组件异常退出时也会把该文件末尾几行直接打在控制台上。
