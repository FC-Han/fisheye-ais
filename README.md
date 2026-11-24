# FishEye 渔眼 · v0.5

全球 AIS 实时态势 + 渔船行为分析的渔业情报平台。
地图只是前端，真正的资产是持续积累的 **AIS 时空数据库** 和 **渔船行为事件库**。

> 核心立场：只展示**实际收到**的 AIS 信号；捕捞状态是**规则推断**；AIS 中断**不等于**关闭 AIS；不做违法认定。

---

## 1. 快速开始

### 1.1 需要准备什么

| 配置项 | 必需 | 用途 | 获取方式 |
|---|---|---|---|
| `AISSTREAM_API_KEY` | **必需** | AIS 实时船位流，没有它就没有数据 | [aisstream.io](https://aisstream.io) 注册即得，免费 |
| Copernicus Marine 账号 | 可选 | 海温 / 海流 / 浪高 / 叶绿素 | [data.marine.copernicus.eu](https://data.marine.copernicus.eu) 免费注册 |
| `CDSAPI_KEY` | 可选 | ERA5 历史风场回填 | [cds.climate.copernicus.eu](https://cds.climate.copernicus.eu) 注册后复制 API key，**并到数据集页点「接受许可」**，否则请求 403 |
| `LLM_API_KEY` | 可选 | 智能体自然语言问答 | 任意 OpenAI 兼容接口（DeepSeek / 通义 / 本地 vLLM）。不填则退回内置规则解析 |

**三个可选项缺了都不会崩**：能力接口会如实返回降级原因，其它分析照常跑。
默认风源是 NOAA GFS（不需要任何 key），所以不配 CDS 也能拿到当天风场。

### 1.2 安装

```bash
git clone https://github.com/FC-Han/fisheye-ais.git
cd fisheye-ais

python -m venv .venv
# Windows
.venv/Scripts/python -m pip install -r requirements.txt
# macOS / Linux
# .venv/bin/python -m pip install -r requirements.txt
```

前端必须构建一次（`dist` 不入库，避免构建产物与源码不同步）：

```bash
cd frontend
npm ci
npm run build
cd ..
```

### 1.3 配置

```bash
cp .env.example .env      # Windows: copy .env.example .env
# 编辑 .env，至少填 AISSTREAM_API_KEY
```

环境引擎（可选）用命令行登录一次即可，凭证存在用户目录、不进版本库：

```bash
.venv/Scripts/python -m copernicusmarine login
```

> 仓库里所有密钥都通过环境变量读取，`.env` / `api.txt` 已在 `.gitignore` 中，
> 不会被提交。请不要把 key 写进任何被跟踪的文件。

### 1.4 启动

```bash
python run.py
# 浏览器打开 http://127.0.0.1:8000
```

首次启动会自动建库（`data/fisheye.db`）、建表、并从仓库自带的
`world_port_index.zip` 自动导入 3669 个港口 —— 不需要额外初始化步骤。

只起一部分：

```bash
python run.py --only api
python run.py --only collector --region east_china
python run.py --no-analytics
```

前端开发模式（热更新，改样式时用）：

```bash
cd frontend && npm run dev     # http://localhost:5173，自动代理 /api 与 /ws
```

### 1.5 确认装好了

```bash
curl http://127.0.0.1:8000/api/health                    # {"ok":true,...}
curl http://127.0.0.1:8000/api/analysis/readiness        # 各引擎可用性 + 缺失原因
python -m unittest discover -s tests -v                  # 83 个单测
```

`readiness` 会明确告诉你哪一块没配好、为什么。它报 `available:false` 是设计行为，
不是安装失败。

---

## 2. 架构

```
AISStream WebSocket (全球 bbox)
        │
   collector/          Python asyncio 长连接：订阅 / 解码 / 去重 / 降采样 / 断线重连
        │  queue.Queue（批量，可丢不可卡）
   collector/writer.py 写入线程：实时状态全量更新 + 轨迹降采样 + JSONL 原始归档
        │
   data/fisheye.db      SQLite WAL + R*Tree 空间索引
        │                （database/init_postgis.sql 为等价 PostGIS 版，供迁移）
        ├── analytics/   捕捞规则引擎 / 中断检测 / 围栏事件 / 保留清理（常驻）
        │
   backend/app/         FastAPI：REST + WebSocket 增量推送 + 智能体工具层
        │
   frontend/            React 18 + TypeScript + Vite + MapLibre GL
```

**为什么是 SQLite 而不是 PostGIS**：这台机器没有 Docker/PostgreSQL，v0.1 用 SQLite
（WAL + R\*Tree）做到零运维可用，表结构与 PostGIS 版一一对应，迁移只换 DAO。
数据量上千万点后请切 PostGIS/TimescaleDB。

---

## 3. 关键工程决策

### 3.1 实时状态全量、轨迹降采样
每条位置消息都更新 `vessel_latest_state`（地图实时性靠它），但只有满足
**同船 ≥30s 或移动 ≥100m** 才写一条 `ais_positions`。
实测全球订阅约 80 条/秒（≈690 万条/天），若全量落库一天 1.4 GB+；
降采样后轨迹体积降到 1/5~1/10，轨迹形状几乎无损。参数见 `.env`。

### 3.2 捕捞识别是可解释规则，不是"AI 判案"
`analytics/fishing_detector.py`
- 只分析 `fishing` / `candidate` / `unknown` 身份，**明确排除货船油轮**
- 片段条件：0.4–6 kn 连续低速、片段 ≥30 分钟、点数 ≥6、点间隔 ≤30 分钟
- 置信度由 5 个因子加权：时长、速度贴合度、速度稳定性、转向率、航迹绕行度
- 输出 `evidence` JSON（每个因子分值、均速、路径长度、直线度…），前端可展开
- **不区分渔具**：没有渔具档案就标 `gear=unknown`，只说"低速作业候选"

### 3.3 AIS 中断必须排除三种假中断
`analytics/gap_detector.py`
1. 我方链路断了 → `stream_health.connected / last_msg_ts`，状态降级为 `stream_unclear`
2. 该海域无覆盖 → 用周边 1.5° 内 15 分钟内的船舶数作覆盖代理，`coverage_quality=poor` 时降级为 `coverage_unclear`
3. 该船本来就发得稀 → 用其自身历史上报间隔中位数 × 3 作为动态期望值

只有三者都排除，才升级为 `candidate`。UI 与智能体都会显示这三项证据。

### 3.4 智能体不碰 SQL
`backend/app/agent/` 是 **工具层 + 规划层**：
- **10 个只读工具**（实时船位 / 轨迹 / 档案 / 运动状态 / 捕捞事件 / 热点 / 中断 / 围栏 / 检索 / 链路），
  全部有结果上限、时间上限、`caveat` 字段
- 配了 `LLM_API_KEY` → OpenAI 兼容接口 + Function Calling（本项目默认 DeepSeek）
- 没配 → 内置中文意图解析，**同一批工具、同样的真实数据**，不是假回答
- 铁律写进 system prompt：区分【观测】/【推断】/【缺失】，不得违法认定
- 追问有上下文：`chat(history)` 会带最近 8 轮，所以"这艘船最近在干什么"能接住上一句提到的 MMSI

### 3.5 热点 = 可解释的聚集度，不是渲染特效
`backend/app/services/hotspots.py` 把海域切成网格（默认 1°，可调），每一格用三路证据加权：
```
score = 捕捞时长(小时) × (0.6 + 0.4×平均置信度)   ← fishing_events
      + 0.5 × 渔船低速点密度(≈船·小时)             ← ais_positions
      + 0.35 × 此刻低速渔船数                      ← vessel_latest_state
      + 0.15 × 该格中断候选数                      ← ais_gap_events
```
分值归一到 0–100，输出 `basis`（event / low_speed / mixed）说明这一格"为什么热"。
海域名用 `core/seas.py` 的粗粒度分区表反查（东海北部、北海西南部…），只用于展示命名。

### 3.6 单船运动画像 = 速度分段 + 几何指标
`backend/app/services/behavior.py` 把轨迹翻译成"这艘船最近在干什么"：
- **状态分段**：<0.5 kn 锚泊 / 0.5–4.5 kn 低速（作业或漂流）/ 4.5–12 kn 巡航 / >12 kn 高速；
  短于 5 分钟的碎片并入相邻段，同状态被 ≤10 分钟缺报隔开也会合并，避免时间轴抖动
- **几何指标**：航迹里程、位移、直线度（位移/里程，低值=在固定渔区往复）、活动半径
- **趋势**：最近 3 小时 vs 前 3 小时均速，给"明显加速/略有减速"这类判定
- 与事件对齐：哪些分段落在疑似捕捞片段内、哪里中断过
- 输出一句可复核的中文叙述，前端同时画状态条 + 逐小时速度曲线

**为什么不做行为分类模型**：免费 AIS 流单船 1 条/3–12 分钟，做分类器只会过拟合。
先给可解释的规则画像，等有船舶登记 + 渔具档案 + 更密数据再谈模型。

---

## 4. 目录

```
core/            config / db / parser / geo —— 三个进程共用
collector/       AISStream 客户端 + 写入线程
analytics/       fishing_detector / gap_detector / geofence_detector / worker
backend/app/     main.py + api/{map,meta,agent}.py + services/ + agent/
frontend/        React + MapLibre（src/components/*）
config/          regions.json（预设海域）/ zones.json（围栏，示意边界）
database/        init_postgis.sql（迁移用）
tests/           16 个单测（解析 / 规则引擎 / 中断判定 / 几何）
data/            fisheye.db + archive/*.jsonl（git 忽略）
```

## 5. API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 健康检查 |
| GET | `/api/stats/summary` | 态势汇总 |
| GET | `/api/stream/status` | 采集链路健康 + 速率历史 + 库大小 |
| GET | `/api/vessels?bbox=&identity=&limit=` | 视野内船舶（R\*Tree 查询） |
| GET | `/api/vessels/{mmsi}` | 单船档案 + 24h 统计 |
| GET | `/api/vessels/{mmsi}/track?hours=` | 轨迹 + 捕捞片段 + 中断 |
| GET | `/api/vessels/{mmsi}/behavior?hours=` | 运动画像：状态分段 / 里程 / 趋势 / 事件对齐 |
| GET | `/api/vessels/{mmsi}/timeline?hours=&compact=` | **行为时间线**：连续片段序列 + 缺口 + 可靠性 |
| GET | `/api/vessels/{mmsi}/trips?hours=` | **航次重建**：切分依据 + 16 维航次特征 |
| GET | `/api/vessels/{mmsi}/fingerprint?days=` | **行为指纹**：稳健基线（不足时返回 insufficient_history） |
| GET | `/api/vessels/{mmsi}/compare?days=&hours=` | **行为差异**：当前航次 vs 自身基线 + 稳健 Z |
| GET | `/api/vessels/{mmsi}/quality?hours=` | **观测质量**：覆盖率 / 缺口 / 上游链路 / 分级建议 |
| GET | `/api/vessels/{mmsi}/evidence?kind=gap\|fishing&ts=` | **证据卡**：观测事实 + 替代解释 + 无法解释项；不带 ts 时返回该窗口的卡片列表 |
| GET | `/api/analysis/readiness` | **能力自检**：当前数据够不够做指纹/环境分析 |
| GET | `/api/analysis/ocean/capabilities` | **环境引擎自检**：依赖 / 凭证 / 产品 / **每个产品的 basis（实时/气候态）** / 明确没做什么 |
| POST | `/api/analysis/ocean/tracks?mmsi=&hours=&products=` | **提交环境提取任务**（异步，返回 job_id）；products 默认 `physics,waves,wind_gfs,bgc_clim`，可加 `wind`（卫星观测风）/ `wind_era5`（历史风） |
| GET | `/api/analysis/ocean/series?mmsi=&hours=` | **只读环境序列**（前端面板用，绝不联网；无缓存返回 `no_cache`） |
| GET | `/api/analysis/jobs?limit=&status=` | 异步任务列表 + 队列深度 |
| GET | `/api/analysis/jobs/{job_id}?with_result=` | 单任务状态 / 结果全文 |
| GET | `/api/hotspots?hours=&cell=&limit=&bbox=` | 渔船作业热点（网格聚集度排序） |
| GET | `/api/map/density?bbox=&cell=` | 网格聚合（低缩放用） |
| GET | `/api/events?kinds=&hours=` | 三类事件归一化列表 |
| GET | `/api/zones` · `/api/regions` | 围栏 / 预设海域 |
| POST | `/api/agent/chat` | 智能体对话 |
| WS | `/ws/vessels?bbox=&interval=` | 视野增量推送（每 2s，仅推 ts 增长的船） |

## 6. 已知限制（重要）

1. **AISStream 免费流地理分布极不均匀，中国近海几乎没数据。** 实测：
   - 全球订阅 1 小时：欧洲 3984 艘、东海 9 艘、黄海 24 艘、南海北部 75 艘；
   - 单独订阅东海 bbox（24–33.5°N, 119–128°E）**75 秒只收到 4 条消息**。
   
   这是上游接收端/抽样策略造成的，不是本系统过滤掉的。要做中国渔业态势，
   必须换国内 AIS 源（船讯网/亿海蓝等）或商业套餐——这是产品级前置条件，不是代码问题。
2. **全球订阅吞吐受上游限制**。实测 30–85 条/秒（远低于"全球 AIS"的理论量级），
   说明免费 Key 拿到的是抽样流；单船约 1 条/3–12 分钟，所以 30 分钟以上的连续作业片段
   需要积累 40 分钟以上数据才会出现（第一版启动后约 30 分钟开始出事件，属正常）。
3. **船舶身份是弱分类**。仅靠 AIS 类型位 30 判"渔船"，缺失时用船名模式判"疑似"。
   生产环境需接船舶登记/许可名单（`vessels.identity_source` 已预留字段）。
4. **围栏是示意边界**。`config/zones.json` 里的多边形是占位数据（`source=demo`），
   必须替换为农业农村部公告 / MPA 官方 shapefile。
5. **数据授权未确认**。AISStream 允许实时访问 ≠ 允许长期存储、再分发、商业化。
6. **底图三选一，全部免 Key**（右上角切换，选择记忆在 localStorage）：
   | 模式 | 来源 | 说明 |
   |---|---|---|
   | 深海 | 自托管 Natural Earth 50m + 10m | 零外部依赖，全球视图最快；zoom≥5 自动换 10m 海岸线 |
   | 卫星 | Esri World Imagery 栅格 | 免 Key，放大看岸线/港口最真实 |
   | 海图 | Esri Ocean Base + Reference | 免 Key，含水深与海域注记，渔业场景最贴 |

   已经踩过的坑：CARTO dark 现在返回 "API KEY REQUIRED" 水印；OSM 官方瓦片对本机直接拒绝（403）；
   OpenFreeMap 样式可加载但切换后自定义图层易丢。**要让外部矢量样式可靠，必须自建 style 对象而不是 setStyle(url)。**
   若后续要换 MapTiler / Mapbox / CARTO，只需在 `frontend/src/basemaps.ts` 的 `styleFor()` 加一个分支。
7. 轨迹为降采样存储；事件表保留 30 天；原始 JSONL 归档默认上限 2 GB 滚动。

## 7. v0.2 新增能力（本次）

- **单船追踪**：船舶面板「◎ 追踪」把地图锁在目标船上，船位每 2–3 秒更新一次就平滑跟随；
  手动拖图或按 Esc 自动退出，左下角有追踪浮窗。
- **渔船热点**：热力图层 + 环形标注 + 排行榜面板（得分、事件数、低速船数、中断线索、代表船舶），
  点击卡片或地图圆环飞向该海域；左栏常驻 TOP3。
- **单船运动画像**：状态条、逐小时均速曲线、可复核的中文叙述、状态分段列表、轨迹 CSV 导出。
- **AI 助手升级**：接 DeepSeek（`.env` 的 `LLM_API_KEY`），支持热点/运动状态/按船名检索三类新问题，
  能带上下文追问；答案强制"结论 → 逐条依据 → 数据局限"，并可一键在地图上定位结果。
- **底图三选一**（见 6.6）、界面重排（热点/事件/船舶/AI 四个标签页）、速率迷你曲线。

## 8. v0.3 分析层（Behavior Engine / Vessel DNA / Reliability）

这一层的设计原则是**算法算、模型解释**：所有判断都由确定性代码完成并留下证据，
大模型只负责组织语言和与用户交互，绝不碰原始探索。

| 模块 | 作用 | 代码 |
|---|---|---|
| `timeline.py` | 轨迹 → 时间连续的行为片段（两层标签） | 缺口显式成段，不插值；`confidence` 恒为 null |
| `trips.py` | 按停泊/断档切分航次 + 16 维特征 | 截断航次标 `truncated`，不进基线 |
| `fingerprint.py` | 稳健基线（中位数/MAD/P10/P90）+ 变化检测 | MAD=0 时 Z 分数返回 null，不硬凑 |
| `reliability.py` | 观测质量 + 中断归因 | 用「最长连续断连」而不是在线比例判断链路责任 |
| `evidence.py` | 事件证据卡 | 观测事实/替代解释/无法解释三分离 |

三个必须知道的约定：

1. **`confidence` 恒为 `null`**。规则得分叫 `evidence.rule_score`，它未经标注数据校准，不是概率。
2. **`gear_type` 恒为 `unknown`**。没有渔具登记库时，不假装能区分拖网/围网/延绳钓。
   另外注意：**拖网本身就是直线拖曳**，所以"直线度低=在作业"这个直觉对拖网是错的，
   代码里直线度只在「长距离 + 长时间」时才作为排除条件。
3. **中断归因只给倾向，不给结论**。`vessel_side_likely` 不等于"故意关闭 AIS"。

能力边界与待办见 [`TODO/README.md`](TODO/README.md) 和 [`TODO/INPUTS-NEEDED.md`](TODO/INPUTS-NEEDED.md)。

## 9. v0.3 增量（港口判定 / 环境引擎 / 异步任务）

| 模块 | 作用 | 关键点 |
|---|---|---|
| `scripts/load_wpi.py` | 导入 NGA World Port Index | 直接解析 `.dbf`，不依赖 pyshp；3669 个港口全导入 |
| `services/ports.py` | 最近港口查询 + 在港/锚地分类 | 网格分桶，单次查询 ~0.04 ms；**在港半径按 L/M/S/V 规模分档**（12/8/4/3 km） |
| `services/ocean.py` | 轨迹点环境特征（SST/流速/海高） | 两层缓存；30 分钟采样；**逐点记 `source_time` 与 `lag_hours`** |
| `services/jobs.py` | 异步任务队列（SQLite） | 同参数去重、抢占用 rowcount 判断、卡死任务可回收 |
| `analytics/job_worker.py` | 常驻 worker | 由 `run.py` 拉起，日志 `logs/worker.log` |
| `run.py` | 增加第 4 个子进程 | `--no-worker` 可跳过 |

**环境数据的两个坑（已写进代码注释）**：

1. 带三维物理量的近实时数据集是 `cmems_mod_glo_phy_anfc_0.083deg_PT1H-m`，
   **不是** `_P1D-m`——后者只有海冰、混合层、海底温盐，没有 `thetao`/`uo`/`vo`。
2. 深度要用**区间** `0–1 m`，别用精确值 `0.494025`，否则浮点精度会被判成越界。

**环境数据的三条硬约束**：

- 每个点都记 `source_time` / `lag_hours`，超 36 小时判 `out_of_range`，绝不拿过期值当此刻值
- 全球海洋模式把内河、潟湖、部分港湾判为陆地，这类点返回 `land_mask`（这是正确行为，不是 bug）
- **只能说明「船经历过什么环境」，不能说明「船偏好什么环境」**——后者需要构造对照样本

## 10. 下一步

- P0：采集器连续跑 3–9 天（唯一等着就行的项）
- P1：~~10 m 风~~ → 已接三源（GFS / CMEMS L4 观测 / ERA5），见第 12 节
- P2：标注数据 → 概率校准 → V2 模型（按当前决定暂缓，保持速度阈值规则）
- P3：PostGIS 迁移（约 1 个月后到千万级轨迹点）
- 预警：站内 → 邮件/企业微信推送

## 11. v0.4 增量（海浪 / 叶绿素气候态 / 环境面板 / 证据卡界面）

| 模块 | 作用 | 关键点 |
|---|---|---|
| `services/ocean.py` | 三个环境产品统一调度 | `physics`（逐时）+ `waves`（3 小时）+ `bgc_clim`（历史气候态） |
| `ocean_features` | 列级合并写入 | 同一 `(mmsi, ts)` 被多个产品写，**整行 REPLACE 会把先写的清成 NULL** |
| `components/OceanPanel.tsx` | 海温 / 流速 / 浪高曲线 | 缺测**断线不插值**；叶绿素带「气候态」徽标 |
| `components/EvidenceCards.tsx` | 证据卡界面 | 观测事实 / 统计推断 / 无法得知 三分离，复核状态可见 |
| `evidence.gap_cards` | 事件表为空时回退时间线 | 否则「时间线画着缺口、证据卡说没有」自相矛盾 |

**三个决定性事实（都实测过，不是推测）**：

1. **风浪不在物理产品里**，必须换到 `cmems_mod_glo_wav_anfc_0.083deg_PT3H-i`
   （`VHM0`/`VMDR`/`VTM02`，3 小时，2D 场**不能传 depth**）。同一套 Copernicus 凭证即可，无需新账号。
2. **BGC 近实时产品真的没有 chl**：请求直接报 `VariableDoesNotExistInTheDataset`。
   本系统给的是 `cmems_mod_glo_bgc_my_0.25deg_P1M-m` 的 **2019–2023 同月气候态**，
   `chl_basis=climatology`、`lag_hours=null`，**不冒充当前浓度**。
3. **BGC 产品第一层是 0.5057 m**，深度下限写 0.0 会被服务端告警越界，必须用 `0.4–1.2 m`。

实测（爱琴海，MMSI 259186000，24h）：
SST 22.1–22.5 °C（滞后 0.1–1.5 h）、有效波高 0.27–0.56 m（微浪~小浪）、
叶绿素 0.085–0.095 mg/m³（2019–2023 十月气候态）。
汉堡港内船舶全部返回 `land_mask` —— 全球模式把易北河判成陆地，**这是正确行为**。

## 12. v0.5 增量（三源风场 + 蒲福风级）

| 产品 key | 数据源 | 延迟 | basis | 说明 |
|---|---|---|---|---|
| `wind_gfs` | NOAA GFS 0.25°（NOMADS GRIB 子集） | **约 5 小时** | `analysis_forecast` | **默认**，唯一能覆盖「今天」 |
| `wind` | CMEMS `cmems_obs-wind_glo_phy_nrt_l4_0.125deg_PT1H` | 约 2 天 | `observation_l4` | 卫星散射计融合**观测** |
| `wind_era5` | ERA5 `reanalysis-era5-single-levels`（CDS） | 约 5 天 | `reanalysis` | 历史回填；单次排队约 3 分钟 |

### 为什么是三个源，而不是「接一个 ERA5 就完事」

这是本轮最实在的发现：**单个源没有一个能覆盖「今天」。**

- ERA5 实测最迟只到 **2026-10-22 04:00**（查询时 10-27，滞后约 5 天）
- CMEMS L4 观测实测只到 **2026-10-25 23:00**（滞后约 2 天）
- 而系统里的 AIS 数据只有最近几小时

只接前两个，界面上永远是「无可用值」——功能写了等于没写。所以加了 GFS。
三个源按时间轴互补：GFS 管今天、L4 管前天、ERA5 管更早。

### 关键实现坑

1. **变量名与列名不是一回事**：风数据集只给 `eastward_wind` / `northward_wind`（GFS 是 `10u`/`10v`），
   风速风向必须自己算。所以 `PRODUCTS` 里把「请求变量」`request_variables` 和「写入列」
   `variables` 分开，混用会去请求一个不存在的变量。
2. **cfgrib 的 `shortName` 不是变量名**：`filter_by_keys={'shortName':'10u'}` 能筛出数据，
   但返回的变量名叫 `u10`。拿 `shortName` 去索引必然 `KeyError`。
3. **时间轴类型必须收敛**：CMEMS 给 `datetime64`、ERA5 的维叫 `valid_time`、
   自拼的 GFS 瓦片一开始存成 int64 纳秒 —— 三种表示相减直接 `UFuncTypeError`。
   现在统一在 `_match_tile` 里转成秒级 int64 再算。
4. **三个风产品共享同一批 `wind_*` 列**，于是「这列有值」不能反推「这个产品跑过」。
   原先的反推逻辑会凭空给一行标上 `wind, era5`（实际只跑了 gfs），
   更糟的是 `_load_cached` 会因此误判命中而**一个字都不抓**。
   现在 `_has_exclusive_columns()` 只对独占列的产品做反推，共享列的一律只认 `products` 字段。
5. **ECMWF open data 没有区域裁剪**：一次要下全球场（一天约 80 MB），
   而 GFS 的区域子集一天只要 **10 KB**，差三个数量级。所以选了 GFS。

### 派生字段

- `beaufort_of()`：蒲福风级表查表（0–12 级），纯查表不是推断
- `wind_dir_text()`：八方位文字
- **风向是「来向」**：352° 表示风从北边吹来。`wind_from_uv()` 里取的是分量反方向，
  写成 `atan2(u, v)` 会把风向整体错 180°。
- 概要里的平均风向**不能取众数**：350° 和 10° 差了 20° 却会算成两个方向。
  现在先对 u/v 分量求平均再反算角度，且分量互相抵消（风向转了一圈以上）时返回 `null`
  而不是硬给一个平均值。

### 实测（爱琴海 259186000，24 小时）

| 变量 | 数值 | 来源 |
|---|---|---|
| 10 m 风速 | 5.6–12.0 m/s，中位 7.5 | GFS，滞后 0.1–2.3 h |
| 蒲福风级 | 4 级和风为主（5/7 点），2 点达 6 级强风 | 查表 |
| 风向 | 平均 352.8°，北风 | 分量平均 |
| 有效波高 | 0.27–0.56 m（微浪~小浪） | CMEMS WAV |
| 海表温度 | 22.1–22.5 °C | CMEMS PHY |
| 叶绿素 | 0.085–0.095 mg/m³（2019–2023 十月气候态） | CMEMS BGC MY |

智能体回答验证：

> 「以 4 级和风为主，风速 5.6–12.0 m/s，中位 7.5 m/s，风向偏北…
> 数据局限：风为 GFS 分析/短时预报在最近网格的取值，非船上实测，滞后 0.1–2.3 小时。」

### 依赖

`cdsapi`（ERA5）、`cfgrib` + `eccodes`（GFS 的 GRIB2）已加入 `requirements.txt`。
CDS 的 key 通过 `CDSAPI_KEY` 环境变量读取（写进 `.env` 即可）。本地若沿用旧的 `api.txt`，
代码会回退读它的第 3 行；该文件已在 `.gitignore` 中，**不会入库**。
