import type { FishEvent } from '../types'

interface Props {
  events: FishEvent[]
  kinds: Record<string, boolean>
  onKinds: (k: Record<string, boolean>) => void
  hours: number
  onHours: (h: number) => void
  onPick: (ev: FishEvent) => void
  loading: boolean
}

const KIND_LABEL: Record<string, string> = { fishing: '捕捞', gap: '中断', geofence: '区域' }
const STATUS_TEXT: Record<string, string> = {
  candidate: '候选',
  coverage_unclear: '覆盖不足',
  stream_unclear: '链路异常',
  enter: '进入',
  exit: '离开',
  suspected: '疑似',
}

function fmt(ts?: number | null) {
  if (!ts) return '—'
  const d = new Date(ts * 1000)
  return `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} ${String(
    d.getHours(),
  ).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

export default function EventsPanel({ events, kinds, onKinds, hours, onHours, onPick, loading }: Props) {
  const filtered = events.filter((e) => kinds[e.kind])
  const counts = {
    fishing: events.filter((e) => e.kind === 'fishing').length,
    gap: events.filter((e) => e.kind === 'gap').length,
    geofence: events.filter((e) => e.kind === 'geofence').length,
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      <div className="filter-row">
        {(['fishing', 'gap', 'geofence'] as const).map((k) => (
          <button
            key={k}
            className={`chip ${kinds[k] ? 'active' : ''}`}
            onClick={() => onKinds({ ...kinds, [k]: !kinds[k] })}
          >
            {KIND_LABEL[k]}
            <span className="n">{counts[k]}</span>
          </button>
        ))}
        <div style={{ flex: 1 }} />
        {[6, 24, 72].map((h) => (
          <button key={h} className={`chip ${hours === h ? 'active' : ''}`} onClick={() => onHours(h)}>
            {h}h
          </button>
        ))}
      </div>

      <div className="panel-scroll">
        {loading && !events.length && <div className="empty">正在加载事件…</div>}
        {!loading && !filtered.length && (
          <div className="empty">
            该时间窗内没有事件。
            <br />
            捕捞片段需要至少 30 分钟连续低速作业才会生成；
            <br />
            中断候选需要排除覆盖与链路因素。
          </div>
        )}
        {filtered.map((e, i) => (
          <div className="event" key={`${e.kind}-${e.mmsi}-${e.ts}-${i}`} onClick={() => onPick(e)}>
            <div className="event-top">
              <span className={`event-kind ${e.kind}`}>{KIND_LABEL[e.kind]}</span>
              <span className="event-name">{e.ship_name || `MMSI ${e.mmsi}`}</span>
              <span className="event-ts">{fmt(e.ts)}</span>
            </div>
            <div className="event-detail">{e.detail}</div>
            <div className="event-meta">
              <span>{e.mmsi}</span>
              {e.kind !== 'geofence' && <span>置信 {(e.confidence * 100).toFixed(0)}%</span>}
              {STATUS_TEXT[e.status] && <span>{STATUS_TEXT[e.status]}</span>}
              {e.lat != null && (
                <span>
                  {e.lat.toFixed(2)}, {e.lon!.toFixed(2)}
                </span>
              )}
            </div>
            {e.kind !== 'geofence' && (
              <div className="conf-bar">
                <i
                  style={{
                    width: `${Math.round(e.confidence * 100)}%`,
                    background: e.kind === 'fishing' ? '#34d399' : e.confidence > 0.5 ? '#f0645f' : '#55647a',
                  }}
                />
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  )
}
