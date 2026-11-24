import { useCallback, useEffect, useRef, useState } from 'react'
import type { OceanPoint, OceanSeries } from '../types'
import { ENV_COLORS, WIND_BASIS_LABELS } from '../types'
import { api } from '../api'

interface Props {
  mmsi: number
  hours: number
  onLocate?: (lat: number, lon: number, ts: number) => void
}

function fmtTime(ts: number) {
  const d = new Date(ts * 1000)
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

function num(v: number | null | undefined, digits = 2) {
  return v == null ? '—' : v.toFixed(digits)
}

/**
 * 一条环境变量的折线。
 *
 * 关键取舍：缺测必须断线，不能跨过去连起来假装数据连续。
 * 这是环境面板存在的意义之一 —— 让「哪里没数据」和「数据是多少」一样显眼。
 */
function EnvChart({
  points,
  pick,
  color,
  unit,
  label,
  digits = 2,
}: {
  points: OceanPoint[]
  pick: (p: OceanPoint) => number | null
  color: string
  unit: string
  label: string
  digits?: number
}) {
  const vals = points.map((p) => ({ ts: p.ts, v: pick(p), q: p.quality }))
  const nums = vals.map((d) => d.v).filter((v): v is number => v != null)
  if (!nums.length) {
    return (
      <div className="env-chart">
        <div className="env-chart-head">
          <span style={{ color }}>{label}</span>
          <em>无可用值</em>
        </div>
        <div className="env-chart-empty">该时段没有取到 {label}（可能全部落在海陆掩膜内）</div>
      </div>
    )
  }

  const w = 316
  const h = 56
  const pad = 4
  const lo = Math.min(...nums)
  const hi = Math.max(...nums)
  const span = hi - lo || Math.abs(hi) || 1
  const t0 = vals[0].ts
  const t1 = vals[vals.length - 1].ts
  const tspan = t1 - t0 || 1

  const xy = (d: { ts: number; v: number | null }) => ({
    x: pad + ((d.ts - t0) / tspan) * (w - pad * 2),
    y: h - pad - ((d.v as number) - lo) / span * (h - pad * 2),
  })

  // 按「连续有值」切段：中间有 null 就断开，不做插值
  const runs: Array<Array<{ ts: number; v: number | null; q: string | null }>> = []
  let cur: Array<{ ts: number; v: number | null; q: string | null }> = []
  for (const d of vals) {
    if (d.v == null) {
      if (cur.length) runs.push(cur)
      cur = []
    } else {
      cur.push(d)
    }
  }
  if (cur.length) runs.push(cur)

  const paths = runs.map((run) =>
    run
      .map((d, i) => {
        const { x, y } = xy(d)
        return `${i === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`
      })
      .join(' '),
  )

  const dots = runs.flat().map((d, i) => {
    const { x, y } = xy(d)
    return { x, y, ok: d.q === 'ok', i }
  })

  return (
    <div className="env-chart">
      <div className="env-chart-head">
        <span style={{ color }}>{label}</span>
        <em>
          {num(lo, digits)}–{num(hi, digits)} {unit}
        </em>
      </div>
      <svg viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" className="env-svg">
        <line x1={pad} y1={h - pad} x2={w - pad} y2={h - pad} stroke="rgba(126,163,201,0.18)" strokeWidth="1" />
        {paths.map((d, i) => (
          <path
            key={i}
            d={d}
            fill="none"
            stroke={color}
            strokeWidth="1.4"
            strokeLinejoin="round"
            vectorEffect="non-scaling-stroke"
          />
        ))}
        {dots.map((d) => (
          <circle
            key={d.i}
            cx={d.x}
            cy={d.y}
            r={d.ok ? 1.4 : 1.1}
            fill={d.ok ? color : '#0b1119'}
            stroke={color}
            strokeWidth={d.ok ? 0 : 1}
          />
        ))}
      </svg>
      <div className="env-chart-foot">
        <span>{fmtTime(t0)}</span>
        <span>{fmtTime(t1)}</span>
      </div>
    </div>
  )
}

export default function OceanPanel({ mmsi, hours, onLocate }: Props) {
  const [data, setData] = useState<OceanSeries | null>(null)
  const [loading, setLoading] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)
  const alive = useRef(true)

  useEffect(() => {
    alive.current = true
    return () => {
      alive.current = false
    }
  }, [])

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const r = await api.oceanSeries(mmsi, hours)
      if (alive.current) setData(r)
    } catch {
      if (alive.current) setData({ status: 'error', message: '环境接口不可用' })
    } finally {
      if (alive.current) setLoading(false)
    }
  }, [mmsi, hours])

  useEffect(() => {
    setMsg(null)
    load()
  }, [load])

  // 提交异步任务后轮询，最多等 120 秒。等不到就说没算完，不假装成功。
  const extract = async () => {
    setSubmitting(true)
    setMsg('已提交环境提取任务，正在访问 Copernicus（约 20–90 秒）…')
    try {
      const job = await api.oceanSubmit(mmsi, hours)
      const deadline = Date.now() + 120_000
      let status = job.status
      while (Date.now() < deadline) {
        await new Promise((r) => setTimeout(r, 2000))
        const j = await api.job(job.job_id)
        status = j.status
        if (status === 'done' || status === 'failed') break
      }
      if (!alive.current) return
      if (status === 'done') {
        setMsg(null)
        await load()
      } else if (status === 'failed') {
        setMsg('任务失败，可稍后重试。')
      } else {
        setMsg('任务仍在后台运行，稍后会自动出现在这里。')
      }
    } catch (e: any) {
      if (alive.current) setMsg(`提交失败：${e?.message ?? e}`)
    } finally {
      if (alive.current) setSubmitting(false)
    }
  }

  const summary = data?.summary
  const points = data?.series ?? []
  const n = summary?.points_matched ?? 0
  const sampled = summary?.points_sampled ?? 0

  return (
    <>
      <div className="section-title">
        环境背景 · {hours}h
        <span className="env-src">Copernicus · NOAA GFS · ERA5</span>
      </div>

      {loading && !data && <div className="empty">读取环境缓存…</div>}

      {data && (data.status === 'no_cache' || data.status === 'no_track' || data.status === 'unavailable') && (
        <div className="env-cta">
          <div className="env-cta-text">{data.message || data.reason || '暂无环境数据'}</div>
          {data.status === 'no_cache' && (
            <button className="env-btn" onClick={extract} disabled={submitting}>
              {submitting ? '提取中…' : '⟳ 提取这艘船的环境特征'}
            </button>
          )}
          {msg && <div className="env-cta-note">{msg}</div>}
        </div>
      )}

      {data && data.status === 'ok' && summary && (
        <>
          <div className="env-kpis">
            <div className="env-kpi">
              <span className="k">海表温度</span>
              <span className="v" style={{ color: ENV_COLORS.sst }}>
                {num(summary.sst_c?.median, 1)}
                <i>°C</i>
              </span>
              <span className="r">
                {num(summary.sst_c?.min, 1)}–{num(summary.sst_c?.max, 1)}
              </span>
            </div>
            <div className="env-kpi">
              <span className="k">表层流速</span>
              <span className="v" style={{ color: ENV_COLORS.current }}>
                {num(summary.current_speed_m_s?.median, 3)}
                <i>m/s</i>
              </span>
              <span className="r">中位</span>
            </div>
            <div className="env-kpi">
              <span className="k">有效波高</span>
              <span className="v" style={{ color: ENV_COLORS.wave }}>
                {num(summary.wave_height_m?.median, 2)}
                <i>m</i>
              </span>
              <span className="r">{summary.sea_state?.dominant_label ?? '—'}</span>
            </div>
            <div className="env-kpi wide">
              <span className="k">
                10 m 风速 · 风向
                {summary.wind_basis?.map((b) => (
                  <em className="env-src" key={b} style={{ marginLeft: 4 }}>
                    {WIND_BASIS_LABELS[b] ?? b}
                  </em>
                ))}
              </span>
              <div className="env-wind-row">
                <span className="v" style={{ color: ENV_COLORS.wind }}>
                  {num(summary.wind_speed_m_s?.median, 1)}
                  <i>m/s</i>
                </span>
                <span className="env-wind-dir">
                  {summary.wind_direction?.mean_text ?? '—'}
                  <em>{num(summary.wind_direction?.mean_deg, 0)}°</em>
                </span>
                <span className="env-wind-bf">
                  {summary.wind_beaufort?.dominant_force != null
                    ? `${summary.wind_beaufort.dominant_force} 级`
                    : '—'}
                  <em>{summary.wind_beaufort?.dominant_label ?? ''}</em>
                </span>
              </div>
              <span className="r">
                {num(summary.wind_speed_m_s?.min, 1)}–{num(summary.wind_speed_m_s?.max, 1)} m/s
                {summary.wind_direction?.mean_deg == null && summary.wind_speed_m_s ? ' · 风向变化过大，未给平均方向' : ''}
              </span>
            </div>
            <div className="env-kpi">
              <span className="k">
                叶绿素
                {summary.chl_basis === 'climatology' && <em className="env-clim">气候态</em>}
              </span>
              <span className="v" style={{ color: ENV_COLORS.chl }}>
                {num(summary.chl_mg_m3?.median, 3)}
                <i>mg/m³</i>
              </span>
              <span className="r">{summary.chl_ref_years ? `${summary.chl_ref_years}` : '—'}</span>
            </div>
          </div>

          <div className="env-charts">
            <EnvChart points={points} pick={(p) => p.sst_c} color={ENV_COLORS.sst} unit="°C" label="海表温度" digits={1} />
            <EnvChart
              points={points}
              pick={(p) => p.current_speed_m_s}
              color={ENV_COLORS.current}
              unit="m/s"
              label="表层流速"
              digits={3}
            />
            <EnvChart points={points} pick={(p) => p.wave_height_m} color={ENV_COLORS.wave} unit="m" label="有效波高" />
            <EnvChart points={points} pick={(p) => p.wind_speed_m_s} color={ENV_COLORS.wind} unit="m/s" label="10 m 风速" digits={1} />
          </div>

          <div className="env-meta">
            <span>
              匹配 {n}/{sampled} 采样点
            </span>
            <span>滞后 中位 {num(summary.lag_hours?.median, 1)}h / 最大 {num(summary.lag_hours?.max, 1)}h</span>
            <span>
              产品{' '}
              {Array.from(new Set(points.flatMap((p) => p.products)))
                .map(
                  (p) =>
                    ({ phy: '物理', wav: '海浪', bgcc: '叶绿素', gfs: '风·GFS', wind: '风·观测', era5: '风·ERA5' } as Record<
                      string,
                      string
                    >)[p] ?? p,
                )
                .join(' · ')}
            </span>
          </div>

          {onLocate && points.length > 0 && (
            <div className="env-jump">
              {points
                .filter((p) => p.lat != null && p.lon != null)
                .slice(0, 6)
                .map((p, i) => (
                  <button
                    key={i}
                    className="chip"
                    onClick={() => onLocate(p.lat as number, p.lon as number, p.ts)}
                    title={`定位 ${new Date(p.ts * 1000).toLocaleString()}`}
                  >
                    {fmtTime(p.ts)}
                  </button>
                ))}
            </div>
          )}

          <div className="env-caveat">
            {summary.chl_basis === 'climatology' && (
              <b>
                叶绿素是 {summary.chl_ref_years} 年同月气候态，不是当前浓度。
              </b>
            )}
            {summary.wind_basis?.includes('analysis_forecast') && (
              <b>风速来自 GFS 模式（分析场 + 3 小时内短时预报），不是现场实测。</b>
            )}
            {summary.wind_basis?.includes('reanalysis') && (
              <b>风速来自 ERA5 再分析（滞后约 5 天），不是现场实测。</b>
            )}
            环境值来自模式/卫星产品最近像元，不是现场实测；只能说明船「经历过」什么环境，不能说明它「偏好」什么环境。
            风向是「来向」：北风表示风从北边吹来。
          </div>

          <div style={{ padding: '0 12px 8px' }}>
            <button className="env-btn ghost" onClick={extract} disabled={submitting}>
              {submitting ? '刷新中…' : '⟳ 补算缺失时段'}
            </button>
          </div>
          {msg && <div className="env-cta-note" style={{ padding: '0 12px 10px' }}>{msg}</div>}
        </>
      )}

      {data && data.status === 'cached_but_unusable' && (
        <div className="env-cta">
          <div className="env-cta-text">有缓存记录，但该时段全部落在陆地掩膜内，没有可用数值。</div>
        </div>
      )}
    </>
  )
}
