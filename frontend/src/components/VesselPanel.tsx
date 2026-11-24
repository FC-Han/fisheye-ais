import BehaviorTimelinePanel from './BehaviorTimeline'
import OceanPanel from './OceanPanel'
import EvidenceCards from './EvidenceCards'
import type {
  Behavior,
  BehaviorTimeline,
  FingerprintResult,
  FishEvent,
  ObservationQuality,
  TrackPoint,
  TrackResponse,
  Vessel,
} from '../types'
import { IDENTITY_LABELS, STATE_COLORS } from '../types'

interface Props {
  vessel: Vessel | null
  track: TrackResponse | null
  behavior: Behavior | null
  behaviorLoading: boolean
  timeline: BehaviorTimeline | null
  timelineLoading: boolean
  quality: ObservationQuality | null
  fingerprint: FingerprintResult | null
  onLocate: (lat: number, lon: number, ts: number) => void
  events: FishEvent[]
  hours: number
  onHours: (h: number) => void
  playing: boolean
  onPlay: () => void
  idx: number
  onIdx: (i: number) => void
  onPickEvent: (e: FishEvent) => void
  onClose: () => void
  loading: boolean
  follow: boolean
  onToggleFollow: () => void
  onCenter: () => void
}

function fmt(ts?: number | null) {
  if (!ts) return '—'
  const d = new Date(ts * 1000)
  return `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} ${String(
    d.getHours(),
  ).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}
function fmtTime(ts?: number | null) {
  if (!ts) return '—'
  const d = new Date(ts * 1000)
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

function Spark({ series }: { series: Array<{ hour_ts: number; avg_sog: number | null }> }) {
  const pts = series.filter((s) => s.avg_sog != null)
  if (pts.length < 2)
    return <div className="spark-empty">该窗口报文过少，暂不足以绘制逐小时曲线</div>
  const w = 100
  const h = 26
  const max = Math.max(4, ...pts.map((p) => p.avg_sog as number))
  const step = w / (series.length - 1)
  const coords = series.map((s, i) => ({
    x: i * step,
    y: h - ((s.avg_sog ?? 0) / max) * (h - 3) - 1,
    v: s.avg_sog,
  }))
  const line = coords.map((c, i) => `${i === 0 ? 'M' : 'L'}${c.x.toFixed(2)},${c.y.toFixed(2)}`).join(' ')
  const area = `${line} L${w},${h} L0,${h} Z`
  return (
    <svg className="spark" viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none">
      <defs>
        <linearGradient id="sparkFill" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="rgba(52,211,153,0.35)" />
          <stop offset="100%" stopColor="rgba(52,211,153,0)" />
        </linearGradient>
      </defs>
      <path d={area} fill="url(#sparkFill)" />
      <path d={line} fill="none" stroke="#34d399" strokeWidth="1.2" vectorEffect="non-scaling-stroke" />
      {coords.map((c, i) =>
        c.v != null ? <circle key={i} cx={c.x} cy={c.y} r="0.9" fill="#8ef0c6" /> : null,
      )}
    </svg>
  )
}

function StateBar({ b }: { b: Behavior }) {
  const segs = (b.segments ?? []).filter((s) => s.duration_min > 0)
  if (!segs.length) return null
  const total = segs.reduce((s, x) => s + x.duration_min, 0) || 1
  return (
    <div className="state-bar" title="按时间顺序的运动状态分段">
      {segs.map((s, i) => (
        <i
          key={i}
          style={{
            width: `${(s.duration_min / total) * 100}%`,
            background: STATE_COLORS[s.state] || '#3b4a5e',
            opacity: s.fishing ? 1 : 0.62,
          }}
          title={`${s.label} · ${fmtTime(s.start_ts)}–${fmtTime(s.end_ts)} · ${Math.round(s.duration_min)} 分钟`}
        />
      ))}
    </div>
  )
}

function exportCsv(vessel: Vessel, track: TrackResponse | null) {
  if (!track?.track.length) return
  const head = ['ts', 'datetime', 'lat', 'lon', 'sog_kn', 'cog_deg']
  const rows = track.track.map((p) => [
    p.ts,
    new Date(p.ts * 1000).toISOString(),
    p.lat,
    p.lon,
    p.sog ?? '',
    p.cog ?? '',
  ])
  const csv = [head, ...rows].map((r) => r.join(',')).join('\n')
  const blob = new Blob(['\ufeff' + csv], { type: 'text/csv;charset=utf-8' })
  const a = document.createElement('a')
  a.href = URL.createObjectURL(blob)
  a.download = `fisheye_${vessel.mmsi}_${track.hours}h.csv`
  a.click()
  URL.revokeObjectURL(a.href)
}

export default function VesselPanel(p: Props) {
  const { vessel, track, behavior, behaviorLoading } = p
  if (!vessel) {
    return (
      <div className="empty" style={{ paddingTop: 70 }}>
        在地图上点选任意船舶查看档案与轨迹
        <br />
        也可以搜索船名 / MMSI，或从热点列表进入
      </div>
    )
  }

  const pts: TrackPoint[] = track?.track ?? []
  const cur = pts[Math.min(p.idx, Math.max(0, pts.length - 1))]
  const stat = behavior?.stats
  const stateColor = STATE_COLORS[behavior?.current?.state ?? 'unknown'] || '#5d7899'

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      <div className="vessel-head">
        <div className="vessel-title">
          <span className="vn">{vessel.ship_name || '未知船名'}</span>
          <span className={`badge ${vessel.identity}`}>{IDENTITY_LABELS[vessel.identity] || vessel.identity}</span>
          <button className="icon-btn" style={{ marginLeft: 'auto' }} onClick={p.onClose} title="关闭">
            ✕
          </button>
        </div>
        <div className="vessel-mmsi">
          MMSI {vessel.mmsi}
          {vessel.imo ? ` · IMO ${vessel.imo}` : ''}
          {vessel.call_sign ? ` · ${vessel.call_sign}` : ''}
        </div>
        <div className="vessel-actions">
          <button className={`act ${p.follow ? 'on' : ''}`} onClick={p.onToggleFollow}>
            {p.follow ? '◉ 追踪中' : '◎ 追踪'}
          </button>
          <button className="act" onClick={p.onCenter}>
            ⌖ 居中
          </button>
          <button
            className="act"
            onClick={() => navigator.clipboard?.writeText(String(vessel.mmsi))}
            title="复制 MMSI"
          >
            ⧉ MMSI
          </button>
          <button className="act" onClick={() => exportCsv(vessel, track)} disabled={!track?.track.length} title="导出轨迹 CSV">
            ⤓ 轨迹
          </button>
        </div>
      </div>

      <div className="kv-grid">
        <div className="kv">
          <div className="kv-k">航速 SOG</div>
          <div className="kv-v" style={{ color: (vessel.sog ?? 0) > 6 ? '#dce6f2' : '#34d399' }}>
            {vessel.sog ?? '—'} <span style={{ fontSize: 9, color: 'var(--dim)' }}>kn</span>
          </div>
        </div>
        <div className="kv">
          <div className="kv-k">运动状态</div>
          <div className="kv-v" style={{ color: stateColor, fontSize: 11.5 }}>
            {behaviorLoading ? '分析中…' : behavior?.current?.label.split('（')[0] || '—'}
          </div>
        </div>
        <div className="kv">
          <div className="kv-k">航迹里程</div>
          <div className="kv-v" style={{ fontSize: 12 }}>
            {stat ? `${stat.distance_nm} nm` : '—'}
          </div>
        </div>
        <div className="kv">
          <div className="kv-k">低速活动</div>
          <div className="kv-v" style={{ fontSize: 12 }}>{stat ? `${stat.slow_h} h` : '—'}</div>
        </div>
        <div className="kv">
          <div className="kv-k">疑似捕捞</div>
          <div className="kv-v" style={{ fontSize: 12, color: (behavior?.fishing_events?.length ?? 0) ? '#34d399' : undefined }}>
            {behavior?.fishing_events?.length ?? 0} 段
          </div>
        </div>
        <div className="kv">
          <div className="kv-k">信号中断</div>
          <div className="kv-v" style={{ fontSize: 12, color: (behavior?.gap_events?.length ?? 0) ? '#f0645f' : undefined }}>
            {behavior?.gap_events?.length ?? 0} 次
          </div>
        </div>
      </div>

      <div className="filter-row">
        <span style={{ fontSize: 10.5, color: 'var(--dim)', alignSelf: 'center', marginRight: 4 }}>轨迹窗口</span>
        {[6, 12, 24, 72].map((h) => (
          <button key={h} className={`chip ${p.hours === h ? 'active' : ''}`} onClick={() => p.onHours(h)}>
            {h}h
          </button>
        ))}
      </div>

      <div className="panel-scroll">
        {p.loading && <div className="empty">加载轨迹…</div>}

        {behavior?.current && (
          <>
            <div className="section-title">
              速度画像 · {behavior.hours}h
            </div>
            <div className="behavior-card">
              {/* v0.2 时间线可用时，状态条与叙事由时间线区块承担，这里只留速度曲线避免重复 */}
              {!p.timeline?.points && <StateBar b={behavior} />}
              {!p.timeline?.points && (
                <div className="behavior-legend">
                  {[
                    ['anchored', '锚泊'],
                    ['slow', '低速'],
                    ['transit', '巡航'],
                    ['fast', '高速'],
                  ].map(([k, label]) => (
                    <span key={k}>
                      <i style={{ background: STATE_COLORS[k] }} />
                      {label}
                    </span>
                  ))}
                </div>
              )}
              <div className="spark-row">
                <div className="spark-label">
                  逐小时均速
                  <em>
                    {behavior.trend?.verdict}
                    {behavior.trend?.delta_sog != null ? ` (${behavior.trend.delta_sog > 0 ? '+' : ''}${behavior.trend.delta_sog} kn)` : ''}
                  </em>
                </div>
                <Spark series={behavior.hourly ?? []} />
              </div>
              {!p.timeline?.points && behavior.narrative && (
                <p className="behavior-text">{behavior.narrative}</p>
              )}
              {stat && (
                <div className="behavior-facts">
                  <span>直线度 {stat.straightness ?? '—'}</span>
                  <span>活动半径 {stat.radius_nm} nm</span>
                  <span>峰值 {stat.max_sog ?? '—'} kn</span>
                  <span>最后报文 {Math.round(behavior.freshness_min ?? 0)} 分钟前</span>
                </div>
              )}
            </div>
          </>
        )}

        <BehaviorTimelinePanel
          timeline={p.timeline}
          quality={p.quality}
          fingerprint={p.fingerprint}
          loading={p.timelineLoading}
          hours={p.hours}
          onLocate={p.onLocate}
        />

        <OceanPanel mmsi={vessel.mmsi} hours={p.hours} onLocate={p.onLocate} />

        <div className="section-title">证据卡</div>
        <EvidenceCards mmsi={vessel.mmsi} hours={p.hours} />

        {/* v0.1 的简易分段只在 v0.2 时间线不可用时作为降级显示 */}
        {!p.timeline?.points && !!behavior?.segments?.length && (
          <>
            <div className="section-title">状态分段</div>
            {behavior.segments
              .slice()
              .reverse()
              .slice(0, 8)
              .map((s, i) => (
                <div className="seg" key={i}>
                  <i className="seg-dot" style={{ background: STATE_COLORS[s.state] || '#3b4a5e' }} />
                  <span className="seg-label">
                    {s.label}
                    {s.fishing && <em className="seg-fish">作业片段</em>}
                  </span>
                  <span className="seg-time">
                    {fmtTime(s.start_ts)}–{fmtTime(s.end_ts)}
                  </span>
                  <span className="seg-dur">{Math.round(s.duration_min)}′</span>
                </div>
              ))}
          </>
        )}

        <div className="section-title">事件时间线</div>
        {!p.loading && !(track?.fishing_events.length || track?.gap_events.length) && (
          <div className="empty" style={{ padding: '14px 20px' }}>该窗口内未识别到捕捞片段或信号中断</div>
        )}
        {(track?.fishing_events ?? []).map((f, i) => (
          <div className="mini-event fishing" key={`f${i}`}>
            <span className="t">{fmtTime(f.start_ts)}</span>
            <span className="d">
              疑似捕捞 {Math.round(f.duration_s / 60)} 分钟 · 均速 {f.avg_sog} kn · 置信{' '}
              {(f.confidence * 100).toFixed(0)}%
            </span>
          </div>
        ))}
        {(track?.gap_events ?? []).map((g, i) => (
          <div className="mini-event gap" key={`g${i}`}>
            <span className="t">{fmtTime(g.gap_start)}</span>
            <span className="d">
              信号中断 {g.duration_s ? `${Math.round(g.duration_s / 60)} 分钟` : '进行中'} · {g.event_status} · 覆盖{' '}
              {g.coverage_quality}
            </span>
          </div>
        ))}

        {p.events.length > 0 && (
          <>
            <div className="section-title">关联事件</div>
            {p.events.slice(0, 8).map((e, i) => (
              <div
                className="mini-event"
                key={`e${i}`}
                style={{ cursor: 'pointer', borderLeftColor: e.kind === 'fishing' ? '#34d399' : '#f0645f' }}
                onClick={() => p.onPickEvent(e)}
              >
                <span className="t">{fmt(e.ts)}</span>
                <span className="d">
                  {e.title} · {e.detail}
                </span>
              </div>
            ))}
          </>
        )}

        <div className="section-title">档案</div>
        <div style={{ padding: '0 12px 14px', display: 'grid', gap: 6, fontSize: 11.5 }}>
          <Line k="身份判定来源" v={(vessel as any).identity_source || '—'} />
          <Line k="船型" v={vessel.ship_type_text || '—'} />
          <Line k="长度" v={vessel.length ? `${vessel.length} m` : '—'} />
          <Line k="目的地" v={vessel.destination || '—'} />
          <Line k="最后报告" v={fmt(vessel.ts)} />
        </div>
        {!p.timeline?.points && behavior?.caveat && (
          <div className="hot-caveat">{behavior.caveat}</div>
        )}
      </div>

      {pts.length > 1 && (
        <div className="replay">
          <button className={`icon-btn ${p.playing ? 'play' : ''}`} onClick={p.onPlay} title="回放">
            {p.playing ? '❚❚' : '▶'}
          </button>
          <input
            type="range"
            min={0}
            max={pts.length - 1}
            value={Math.min(p.idx, pts.length - 1)}
            onChange={(e) => p.onIdx(Number(e.target.value))}
          />
          <span className="replay-time">
            {cur ? fmtTime(cur.ts) : '—'} · {cur?.sog ?? '—'}kn
          </span>
        </div>
      )}
    </div>
  )
}

function Line({ k, v }: { k: string; v: string }) {
  return (
    <div style={{ display: 'flex', justifyContent: 'space-between', gap: 12 }}>
      <span style={{ color: 'var(--muted)', flex: 'none' }}>{k}</span>
      <span
        style={{
          fontFamily: 'var(--mono)',
          color: 'var(--text)',
          textAlign: 'right',
          overflow: 'hidden',
          textOverflow: 'ellipsis',
          whiteSpace: 'nowrap',
        }}
      >
        {v}
      </span>
    </div>
  )
}
