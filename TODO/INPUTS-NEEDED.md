# 需要你提供 / 决定的事项

> 按"卡住的程度"排序。每项都写了：为什么卡、我需要什么、拿到之后能打通什么。
> 更新时间：2026-10-27（v0.5）

---

## 1. 【最紧急，但你什么都不用做】让采集器连续跑 3–9 天

**现在的问题**：数据库时间跨度约 **2.5 小时**（2026-10-27 08:55 起）。所有需要"历史"的分析都因为这**一个**原因空转。

**具体影响**：
- `closed_trips = 0` → 行为指纹建不起来
- `compare_vessel_behavior` 只能返回 `insufficient_history`
- 航次特征全是 `truncated`，不能进基线
- 变化检测、异常检测、同类比较全部不可用

**你要做的**：别关机器、别关 `run.py`。

**需要多久**：
- 最少 **3 天**：单船能凑出 3 个完整航次
- **7 天**：指纹开始稳定，能做同船自身对比
- **30 天**：能建季节分层基线

**顺带注意**：`FISHEYE_RETENTION_DAYS` 是 30 天，够用。磁盘约 1 GB/月。

---

## 2. 【已解决，留档】Copernicus Marine 凭证

**结论**：**不需要你再提供。** 机器上已有可用登录态：
`C:\Users\34953\.copernicusmarine\.copernicusmarine-credentials`（2026-08-19 建立）
—— 这是 `I:\Diff` 那套流程留下的，直接复用成功。

**实测验证**：`copernicusmarine` 能正常选中数据集版本并完成 subset 下载，
爱琴海、佛罗里达两个冷瓦片全部取到有效 SST。

**已装的依赖**（`.venv` 里）：`numpy 2.5.4` / `xarray 2026.9.0` / `netCDF4 1.7.5` / `copernicusmarine 2.5.0` / `pandas 3.1.0`

---

## 3. 【已解决，留档】港口点位数据

**结论**：`world_port_index.zip` **能用**，已全部导入（3669 个港口，0 条丢弃）。

处理方式：
```powershell
unzip -o world_port_index.zip -d data/ports
.venv\Scripts\python.exe scripts\load_wpi.py
```

**注意**：zip 里是 shapefile（`.shp/.dbf/.shx`），但有 64 MB —— 因为 WPI 每个字段都开了 254 字符宽。
实际只需要 `.dbf` 里 15 个字段，`load_wpi.py` 直接按偏移切，不需要 pyshp/geopandas。

**已知局限**：2017 版 WPI，港口参考点而非泊位边界，小型渔码头覆盖不全 → 所以结论一律标「候选」。

---

## 4. 【已解决，留档】叶绿素：实时没有，改用气候态

**再确认一次**：`cmems_mod_glo_bgc-bio_anfc_0.25deg_P1D-m` 请求 `chl` 报
`VariableDoesNotExistInTheDataset`（只有 `nppv` / `o2`）。**不是我选错数据集，是产品本身没有。**

**已落地**：`bgc_clim` = `cmems_mod_glo_bgc_my_0.25deg_P1M-m` 的 **2019–2023 同月气候态**。
`chl_basis=climatology`、`lag_hours=null`、界面带「气候态」徽标，**不会冒充当前浓度**。

**还需要的（如果你要）**：把 `I://Diff` 那套 0.1° 历史数据跑完用于「历史作业—环境」配对分析。
`parts/physics` 已有 2320 个文件，`parts/bgc` 是 0，跑完要 3832 个任务。
**现在不做也不影响，按需取气候态只要十几秒。**

---

## 5. 【已解决】海浪 + 风都接了，风是三个源

**海浪**：`cmems_mod_glo_wav_anfc_0.083deg_PT3H-i` → 有效波高 / 波向 / 周期（3 小时）。
同一套 Copernicus 凭证，不用新注册。

**风**：最终接了三个源，不是只接 ERA5 —— 原因很实在：

| 产品 | 源 | 延迟 | 语义 |
|---|---|---|---|
| `wind_gfs` | NOAA GFS（NOMADS 子集，无需 key） | 约 5 小时 | 分析 + ≤3h 预报（**默认**） |
| `wind` | CMEMS 卫星散射计 L4 | 约 2 天 | 观测 |
| `wind_era5` | ERA5 再分析（`CDSAPI_KEY`，可选） | 约 5 天 | 再分析 |

**只用 ERA5 不行**：它滞后 5 天，而系统里的 AIS 数据只有最近几小时，
接了也一条风都取不到。GFS 是唯一能覆盖「今天」的，所以设为默认。

**已经验证**：CDS key 有效、许可已接受（实测最迟可用到 2026-10-22 04:00）；
GFS 实测延迟 < 6 小时，一天 10 KB、14 秒。

**不再需要你提供任何东西。**

---

## 6. 【已按你的决定暂缓】渔船行为标注数据

**你的决定**：先用速度阈值，做简单点。

**当前实现**：`SOG` 阈值 + 航迹形态规则，`rule_score` 是打分不是概率，`confidence` 恒 null，
`gear_type` 恒 `unknown`。**不做拖网/围网/延绳钓的区分。**

**什么时候需要标注**：想把 `rule_score` 升级成真正的 `confidence`、
或者做 V2 模型（LightGBM/HMM）、或者要在论文里报准确率/召回率时。

**可能来源**：Global Fishing Watch 开放数据、VMS 交叉验证、渔业观察员记录、人工标注几百段。

---

## 7. 【已按你的决定暂缓】国内 / 中国近海 AIS 数据源

**你的决定**：先不补充。

**现状留档**：AISStream 免费流覆盖重心在欧洲。当前点位最密的区域是
荷兰内河（52°N 5°E）、西雅图（49°N 123°W）、爱琴海（38°N 24°E）、迈阿密（26°N 80°W）。

**将来要补的话**：商业 AIS API（Spire / MarineTraffic / 国内厂商）、自建岸基站、或采购历史数据集。

---

## 8. 其他小决定

- **数据库什么时候迁 PostGIS**：按当前速率约 1 个月到千万级轨迹点。要不要现在迁？
  （现在 30 多万点、单机 SQLite 跑得动，暂时不是瓶颈）
- ~~前端要不要加「环境」面板~~ → **已做**：海温 / 流速 / 浪高三条曲线 + 指标卡，缺测断线不插值。
- ~~证据卡要不要做成界面卡片~~ → **已做**：侧栏「证据卡」区块，观测事实 / 统计推断 / 无法得知三分离。

---

## 附：怎么自己检查当前状态

```powershell
cd H:\ais_eyes

# 数据够不够做指纹
curl --noproxy '*' http://127.0.0.1:8000/api/analysis/readiness

# 环境引擎能不能用、缺什么
curl --noproxy '*' http://127.0.0.1:8000/api/analysis/ocean/capabilities

# 给某艘船提环境提取任务（异步）
curl --noproxy '*' -X POST "http://127.0.0.1:8000/api/analysis/ocean/tracks?mmsi=259186000&hours=6"

# 查任务进度 / 结果
curl --noproxy '*' "http://127.0.0.1:8000/api/analysis/jobs/<job_id>?with_result=true"

# 某艘船的时间线（含在港/锚泊判定）
curl --noproxy '*' "http://127.0.0.1:8000/api/vessels/211437290/timeline?hours=24"

# 港口表状态
.venv\Scripts\python.exe -c "from backend.app.services import ports; print(ports.port_count())"
```
