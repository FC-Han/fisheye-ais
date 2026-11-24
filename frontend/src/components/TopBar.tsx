import { useEffect, useRef, useState } from 'react'
import { api } from '../api'
import type { StreamStatus } from '../types'
import { BASEMAPS, BaseKey } from '../basemaps'

interface Props {
  stream: StreamStatus | null
  regions: Array<{ key: string; name: string; bbox: number[] }>
  activeRegion: string
  onRegion: (key: string, bbox: number[]) => void
  onPickVessel: (mmsi: number) => void
  totalVessels: number
  fishingVessels: number
  base: BaseKey
  onBase: (k: BaseKey) => void
}

export default function TopBar({
  stream,
  regions,
  activeRegion,
  onRegion,
  onPickVessel,
  totalVessels,
  fishingVessels,
  base,
  onBase,
}: Props) {
  const [q, setQ] = useState('')
  const [results, setResults] = useState<any[]>([])
  const [open, setOpen] = useState(false)
  const timer = useRef<number | null>(null)

  useEffect(() => {
    if (!q.trim()) {
      setResults([])
      return
    }
    if (timer.current) window.clearTimeout(timer.current)
    timer.current = window.setTimeout(async () => {
      try {
        const r = await api.search(q.trim())
        setResults(r.results)
        setOpen(true)
      } catch {
        setResults([])
      }
    }, 260)
    return () => {
      if (timer.current) window.clearTimeout(timer.current)
    }
  }, [q])

  const stale = stream?.seconds_since_message ?? null
  const collectorDown = !!stream && !stream.collector_alive
  const linkDown = !!stream && !stream.connected
  const noData = stale !== null && stale > 120
  const dotClass = !stream ? '' : collectorDown || noData ? 'bad' : linkDown || (stale !== null && stale > 30) ? 'warn' : ''
  const statusText = !stream
    ? '连接中…'
    : collectorDown
      ? '采集器离线'
      : linkDown
        ? '重连中…'
        : noData
          ? `无数据 ${Math.round(stale! / 60)} 分钟`
          : `${stream.rate_1m.toFixed(0)} 条/秒`

  const shown = regions.filter((r) =>
    ['global', 'east_china', 'yellow_sea', 'south_china_sea', 'northwest_pacific'].includes(r.key),
  )

  return (
    <div className="topbar">
      <div className="brand">
        <div className="brand-mark" />
        <div>
          <div className="brand-title">FishEye 渔眼</div>
          <div className="brand-sub">Fisheries Intelligence</div>
        </div>
      </div>

      <div className="region-select">
        {shown.map((r) => (
          <button
            key={r.key}
            className={activeRegion === r.key ? 'active' : ''}
            onClick={() => onRegion(r.key, r.bbox)}
            title={r.name}
          >
            {r.name}
          </button>
        ))}
      </div>

      <div style={{ position: 'relative' }}>
        <div className="search-box">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" style={{ color: '#55647a' }}>
            <circle cx="11" cy="11" r="7" />
            <path d="m20 20-3.5-3.5" />
          </svg>
          <input
            placeholder="船名 / MMSI"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            onFocus={() => results.length && setOpen(true)}
            onBlur={() => window.setTimeout(() => setOpen(false), 180)}
          />
        </div>
        {open && results.length > 0 && (
          <div className="search-results">
            {results.map((r) => (
              <button
                key={r.mmsi}
                onClick={() => {
                  onPickVessel(r.mmsi)
                  setOpen(false)
                  setQ('')
                }}
              >
                <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: '#7c8ca3' }}>{r.mmsi}</span>{' '}
                {r.ship_name || '未知船名'}
                <span style={{ float: 'right', color: '#55647a', fontSize: 10 }}>{r.ship_type_text || ''}</span>
              </button>
            ))}
          </div>
        )}
      </div>

      <div className="spacer" />

      <div className="base-select" title="底图来源（均为免 Key 方案）">
        {BASEMAPS.map((b) => (
          <button
            key={b.key}
            className={base === b.key ? 'active' : ''}
            onClick={() => onBase(b.key)}
            title={b.hint}
          >
            {b.label}
          </button>
        ))}
      </div>

      <div className="status-pill" title="视野内船舶 / 其中渔船相关">
        <span style={{ color: '#55647a' }}>视野</span>
        <span style={{ fontFamily: 'var(--mono)', color: '#dce6f2' }}>{totalVessels.toLocaleString()}</span>
        <span style={{ color: '#55647a' }}>渔船</span>
        <span style={{ fontFamily: 'var(--mono)', color: '#34d399' }}>{fishingVessels.toLocaleString()}</span>
      </div>

      <div className="status-pill" title={stream?.region ? `订阅区域：${stream.region}` : ''}>
        <span className={`status-dot ${dotClass}`} />
        <span style={{ fontFamily: 'var(--mono)' }}>{statusText}</span>
      </div>
    </div>
  )
}
