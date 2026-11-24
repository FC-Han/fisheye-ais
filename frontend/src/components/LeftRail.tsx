import type { Hotspot, Identity, StreamStatus, Summary } from '../types'
import { IDENTITY_COLORS, IDENTITY_LABELS } from '../types'

interface Props {
  summary: Summary | null
  stream: StreamStatus | null
  counts: Record<string, number>
  visible: Record<Identity, boolean>
  onToggle: (k: Identity) => void
  showEvents: boolean
  showZones: boolean
  showHeat: boolean
  onToggleEvents: () => void
  onToggleZones: () => void
  onToggleHeat: () => void
  hotspots: Hotspot[]
  onPickHotspot: (h: Hotspot) => void
  onOpenHotspots: () => void
}

const ORDER: Identity[] = ['fishing', 'candidate', 'unknown', 'non_fishing']

function RateSpark({ history }: { history: Array<{ ts: number; rate_1m: number }> }) {
  if (!history || history.length < 3) return null
  const w = 100
  const h = 22
  const max = Math.max(1, ...history.map((p) => p.rate_1m))
  const step = w / (history.length - 1)
  const d = history
    .map((p, i) => `${i === 0 ? 'M' : 'L'}${(i * step).toFixed(2)},${(h - (p.rate_1m / max) * (h - 2) - 1).toFixed(2)}`)
    .join(' ')
  return (
    <div className="rate-spark">
      <svg viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none">
        <path d={`${d} L${w},${h} L0,${h} Z`} fill="rgba(75,159,225,0.16)" />
        <path d={d} fill="none" stroke="#4b9fe1" strokeWidth="1.1" vectorEffect="non-scaling-stroke" />
      </svg>
      <span>{max.toFixed(0)}/s 峰值</span>
    </div>
  )
}

export default function LeftRail(p: Props) {
  const { summary, stream, counts, visible, hotspots } = p
  const top = hotspots.slice(0, 3)

  return (
    <div className="left-rail">
      <div className="card" style={{ padding: 0 }}>
        <div className="card-head">
          <span>态势概览</span>
          <span style={{ fontFamily: 'var(--mono)', letterSpacing: 0, color: 'var(--dim)' }}>
            {stream?.region || '—'}
          </span>
        </div>
        <div className="stat-grid">
          <div className="stat">
            <div className="stat-label">实时船舶 30m</div>
            <div className="stat-value blue">{(summary?.vessels_live_30m ?? 0).toLocaleString()}</div>
          </div>
          <div className="stat">
            <div className="stat-label">其中渔船相关</div>
            <div className="stat-value teal">{(summary?.fishing_live ?? 0).toLocaleString()}</div>
          </div>
          <div className="stat">
            <div className="stat-label">疑似捕捞片段 24h</div>
            <div className="stat-value teal">{summary?.fishing_events_24h ?? 0}</div>
          </div>
          <div className="stat">
            <div className="stat-label">中断候选（未恢复）</div>
            <div className="stat-value amber">{summary?.gap_open_candidate ?? 0}</div>
          </div>
        </div>
        {stream?.history && <RateSpark history={stream.history as any} />}
      </div>

      <div className="card" style={{ padding: 0 }}>
        <div className="card-head">
          <span>图层</span>
          <span style={{ color: 'var(--dim)', letterSpacing: 0 }}>点击开关</span>
        </div>
        {ORDER.map((k) => (
          <div className="layer-row" key={k} onClick={() => p.onToggle(k)}>
            <div className={`check ${visible[k] ? 'on' : ''}`} />
            <span className="swatch" style={{ background: IDENTITY_COLORS[k] }} />
            <span style={{ color: visible[k] ? 'var(--text)' : 'var(--dim)' }}>{IDENTITY_LABELS[k]}</span>
            <span className="count">{(counts[k] ?? 0).toLocaleString()}</span>
          </div>
        ))}
        <div className="layer-sep" />
        <div className="layer-row" onClick={p.onToggleHeat}>
          <div className={`check ${p.showHeat ? 'on' : ''}`} />
          <span className="swatch heat" />
          <span style={{ color: p.showHeat ? 'var(--text)' : 'var(--dim)' }}>渔船热点热力</span>
          <span className="count">{hotspots.length}</span>
        </div>
        <div className="layer-row" onClick={p.onToggleEvents}>
          <div className={`check ${p.showEvents ? 'on' : ''}`} />
          <span className="swatch" style={{ background: '#f0645f', borderRadius: 2 }} />
          <span style={{ color: p.showEvents ? 'var(--text)' : 'var(--dim)' }}>异常事件点</span>
        </div>
        <div className="layer-row" onClick={p.onToggleZones}>
          <div className={`check ${p.showZones ? 'on' : ''}`} />
          <span className="swatch" style={{ background: '#4b9fe1', borderRadius: 2 }} />
          <span style={{ color: p.showZones ? 'var(--text)' : 'var(--dim)' }}>监护区 / 围栏</span>
        </div>
      </div>

      <div className="card" style={{ padding: 0 }}>
        <div className="card-head">
          <span>作业热点 TOP3</span>
          <button className="link-btn" onClick={p.onOpenHotspots}>
            全部 →
          </button>
        </div>
        {!top.length && <div className="empty" style={{ padding: '18px 12px' }}>暂无热点</div>}
        {top.map((h, i) => (
          <div className="mini-hot" key={`${h.lat}-${h.lon}`} onClick={() => p.onPickHotspot(h)}>
            <span className="rank">{i + 1}</span>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div className="mini-hot-name">{h.area}</div>
              <div className="mini-hot-meta">
                {h.n_events > 0 ? `${h.n_events} 段疑似捕捞` : '低速聚集'} · {h.n_vessels} 船
              </div>
            </div>
            <span className="mini-hot-score">{Math.round(h.score)}</span>
          </div>
        ))}
      </div>

      <div className="card">
        <div className="card-head">
          <span>数据链路</span>
          <span
            style={{
              color: stream?.collector_alive ? 'var(--teal)' : 'var(--red)',
              letterSpacing: 0,
              fontFamily: 'var(--mono)',
            }}
          >
            {stream?.collector_alive ? 'LIVE' : 'OFFLINE'}
          </span>
        </div>
        <div className="card-body" style={{ display: 'grid', gap: 7 }}>
          <Row k="消息速率" v={stream ? `${stream.rate_1m.toFixed(1)}/s` : '—'} />
          <Row k="累计接收" v={stream ? stream.msgs_total.toLocaleString() : '—'} />
          <Row k="已存轨迹点" v={summary ? summary.positions_total.toLocaleString() : '—'} />
          <Row k="数据库" v={stream ? `${stream.db_size_mb} MB` : '—'} />
          <Row k="智能体" v={stream?.llm_enabled ? 'DeepSeek' : '规则模式'} />
        </div>
      </div>

      <div
        className="card"
        title="地图只显示实际收到的 AIS 信号，空白海域不等于无船；捕捞状态为规则推断；AIS 中断不等于关闭 AIS。"
      >
        <div className="card-body" style={{ fontSize: 10, lineHeight: 1.55, color: 'var(--dim)' }}>
          <b style={{ color: 'var(--muted)' }}>数据边界</b> · 空白≠无船 · 捕捞为推断 · 中断≠关 AIS
        </div>
      </div>
    </div>
  )
}

function Row({ k, v }: { k: string; v: string }) {
  return (
    <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 11 }}>
      <span style={{ color: 'var(--muted)' }}>{k}</span>
      <span style={{ fontFamily: 'var(--mono)', color: 'var(--text)' }}>{v}</span>
    </div>
  )
}
