import type { Hotspot, HotspotsResponse } from '../types'

interface Props {
  data: HotspotsResponse | null
  hours: number
  onHours: (h: number) => void
  loading: boolean
  active: Hotspot | null
  onPick: (h: Hotspot) => void
  onVessel: (mmsi: number) => void
}

const BASIS_LABEL: Record<string, string> = {
  event: '捕捞片段',
  low_speed: '低速聚集',
  mixed: '多源印证',
}

export default function HotspotsPanel({ data, hours, onHours, loading, active, onPick, onVessel }: Props) {
  const spots = data?.hotspots ?? []

  return (
    <div className="panel-scroll">
      <div className="filter-row">
        <span style={{ fontSize: 10.5, color: 'var(--dim)', alignSelf: 'center', marginRight: 4 }}>窗口</span>
        {[6, 12, 24, 72].map((h) => (
          <button key={h} className={`chip ${hours === h ? 'active' : ''}`} onClick={() => onHours(h)}>
            {h}h
          </button>
        ))}
      </div>

      {data && (
        <div className="hot-totals">
          <div>
            <b>{data.totals.live_low_speed_fishing}</b>
            <span>此刻低速渔船</span>
          </div>
          <div>
            <b>{data.totals.suspected_fishing_events}</b>
            <span>疑似捕捞片段</span>
          </div>
          <div>
            <b>{data.totals.cells_with_activity}</b>
            <span>有活动格网</span>
          </div>
        </div>
      )}

      {loading && <div className="empty">正在聚合网格…</div>}
      {!loading && !spots.length && (
        <div className="empty">
          该窗口内没有形成明显热点
          <br />
          可能是 AIS 覆盖稀疏，或渔船低速作业时长不足 30 分钟
        </div>
      )}

      {spots.map((h, i) => {
        const isActive = active?.lat === h.lat && active?.lon === h.lon
        return (
          <div className={`hot ${isActive ? 'active' : ''}`} key={`${h.lat}-${h.lon}`} onClick={() => onPick(h)}>
            <div className="hot-head">
              <span className="hot-rank">{i + 1}</span>
              <span className="hot-area">{h.area}</span>
              <span className="hot-score">{Math.round(h.score)}</span>
            </div>
            <div className="hot-bar">
              <i style={{ width: `${Math.max(3, h.score)}%` }} />
            </div>
            <div className="hot-meta">
              <span>{BASIS_LABEL[h.basis] || h.basis}</span>
              {h.n_events > 0 && <span>{h.n_events} 段疑似捕捞</span>}
              <span>{h.n_vessels} 艘船</span>
              {h.low_speed_vessels > 0 && <span className="teal">{h.low_speed_vessels} 艘低速</span>}
              {h.gap_candidates > 0 && <span className="red">{h.gap_candidates} 条中断线索</span>}
            </div>
            <div className="hot-coord">
              {h.lat.toFixed(1)}°, {h.lon.toFixed(1)}°
              {h.vessel_hours > 0 && <span> · 累计 {h.vessel_hours} 船·小时</span>}
            </div>
            {h.top_vessels.length > 0 && (
              <div className="hot-vessels">
                {h.top_vessels.slice(0, 3).map((v) => (
                  <button
                    key={v.mmsi}
                    onClick={(e) => {
                      e.stopPropagation()
                      onVessel(v.mmsi)
                    }}
                  >
                    {v.name || `MMSI ${v.mmsi}`}
                    {v.sog != null && <em>{v.sog} kn</em>}
                  </button>
                ))}
              </div>
            )}
          </div>
        )
      })}

      {data?.caveat && (
        <div className="hot-caveat">
          热点 = 已接收 AIS 渔船的低速聚集程度排序，<b>不等于实际捕捞量</b>；AIS 稀疏海域会偏低。
        </div>
      )}
    </div>
  )
}
