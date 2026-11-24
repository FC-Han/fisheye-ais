import type { BehaviorTimeline, FingerprintResult, ObservationQuality, TimelineSegment } from '../types'
import { RELIABILITY_COLORS, TIMELINE_COLORS } from '../types'

interface Props {
  timeline: BehaviorTimeline | null
  quality: ObservationQuality | null
  fingerprint: FingerprintResult | null
  loading: boolean
  hours: number
  onLocate: (lat: number, lon: number, ts: number) => void
}

function fmtTime(ts: number) {
  const d = new Date(ts * 1000)
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}
/** 跨天窗口里只显示 HH:MM 会误读，非今天的时刻带上日期 */
function fmtSmart(ts: number) {
  const d = new Date(ts * 1000)
  const now = new Date()
  const hm = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
  return d.toDateString() === now.toDateString()
    ? hm
    : `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} ${hm}`
}
function fmtDur(minutes: number) {
  if (minutes >= 60) {
    const h = Math.floor(minutes / 60)
    const m = Math.round(minutes % 60)
    return m ? `${h}h${m}m` : `${h}h`
  }
  return `${Math.round(minutes)}m`
}

const GRADE_LABEL: Record<string, string> = {
  good: '观测良好',
  fair: '观测一般',
  poor: '观测较差',
  no_data: '无观测',
}

const FLAG_TEXT: Record<string, string> = {
  low_coverage: '覆盖率低',
  long_gap: '长缺口',
  sparse_reporting: '报文稀疏',
  upstream_unstable: '链路不稳',
  poor_segments: '分段不可靠',
  internal_gap: '段内断档',
  too_few_points: '点数过少',
  speed_inferred: '速度推算',
  very_short: '段过短',
  no_observation: '无观测',
}

/** 行为时间线甘特条：一段时间轴，按行为着色，缺口用深红斜纹明确暴露 */
function Gantt({ segments, onLocate }: { segments: TimelineSegment[]; onLocate: Props['onLocate'] }) {
  const total = segments.reduce((s, x) => s + x.duration_s, 0) || 1
  return (
    <div className="gantt">
      {segments.map((s, i) => {
        const pct = (s.duration_s / total) * 100
        const isGap = s.activity === 'observation_gap'
        const isFish = s.sub_activity === 'fishing_candidate'
        const isPort = s.sub_activity === 'port_stay'
        const color = TIMELINE_COLORS[s.activity] || '#3b4a5e'
        return (
          <i
            key={s.seg_id || i}
            className={`gantt-seg ${isGap ? 'gap' : ''} ${isFish ? 'fish' : ''} ${
              isPort ? 'port' : ''
            } ${s.reliability.grade === 'poor' ? 'unreliable' : ''}`}
            style={{
              width: `calc(${pct}% - 1px)`,
              background: isGap ? undefined : color,
              cursor: isGap || s.lat == null ? 'default' : 'pointer',
            }}
            title={`${s.label}${s.sub_label ? ' · ' + s.sub_label : ''}\n${fmtTime(s.start_ts)}–${fmtTime(
              s.end_ts,
            )}（${fmtDur(s.duration_min)}）${isGap ? '\n该时段无任何报文' : ''}${
              isFish ? '\n规则推断，未经概率校准' : ''
            }${s.reliability.grade === 'poor' ? '\n该段观测质量差' : ''}`}
            onClick={() => {
              if (!isGap && s.lat != null && s.lon != null) onLocate(s.lat, s.lon, (s.start_ts + s.end_ts) / 2)
            }}
          />
        )
      })}
    </div>
  )
}

/** 指纹区块：有基线就展示差异，没基线就明确说明还缺什么 */
function DnaBlock({ fp, hours }: { fp: FingerprintResult | null; hours: number }) {
  if (!fp) return null
  if (fp.status !== 'ok') {
    return (
      <div className="dna-empty">
        <div className="dna-empty-title">行为指纹尚未建立</div>
        <div className="dna-empty-body">
          已闭合航次 {fp.n_trips_closed ?? 0} / 需要 {fp.n_trips_required ?? 3} 个。
          指纹要能回答「它现在和平时有什么不同」，必须先有同一艘船多个完整「出海—返港」循环。
        </div>
        <div className="dna-empty-hint">
          强行用一两个航次算基线，得到的差异没有统计意义，所以系统选择不输出。
        </div>
      </div>
    )
  }
  const items = Object.entries(fp.baseline ?? {}).filter(([, v]) => v.median != null)
  return (
    <div className="dna-card">
      <div className="dna-head">
        基线来自 {fp.n_trips_used} 个航次
        <em>窗口 {hours}h</em>
      </div>
      {items.map(([k, v]) => (
        <div className="dna-row" key={k}>
          <span className="dna-k">{FEATURE_LABEL[k] || k}</span>
          <span className="dna-v">
            {v.median}
            <em>
              {v.p10}–{v.p90}
            </em>
          </span>
        </div>
      ))}
    </div>
  )
}

const FEATURE_LABEL: Record<string, string> = {
  duration_h: '航次时长 (h)',
  distance_nm: '航行距离 (nm)',
  fishing_hours: '疑似作业 (h)',
  fishing_segments: '作业片段数',
  longest_fishing_h: '最长连续作业 (h)',
  low_speed_ratio: '低速占比',
  night_ratio: '夜间占比',
  median_sog: '中位航速 (kn)',
  straightness: '航迹直线度',
  activity_radius_nm: '活动半径 (nm)',
}

export default function BehaviorTimelinePanel(p: Props) {
  const { timeline, quality, fingerprint, loading } = p
  const segs = timeline?.segments ?? []
  const sum = timeline?.summary

  if (loading && !timeline) return <div className="empty">重建行为时间线…</div>
  if (!timeline || !timeline.points) {
    return <div className="empty">该窗口内没有轨迹点，无法重建行为</div>
  }

  const grade = quality?.grade ?? 'no_data'

  return (
    <>
      <div className="section-title">
        行为时间线 · {timeline.hours}h
        <span className={`qual-badge ${grade}`}>{GRADE_LABEL[grade] || grade}</span>
      </div>

      <div className="behavior-card">
        <Gantt segments={segs} onLocate={p.onLocate} />

        <div className="gantt-axis">
          <span>{fmtSmart(timeline.segments[0]?.start_ts ?? 0)}</span>
          <span>
            覆盖 {Math.round((sum?.coverage_ratio ?? 0) * 100)}% · 观测{' '}
            {sum?.observed_hours?.toFixed(1)}h · 缺口 {sum?.gap_hours?.toFixed(1)}h
          </span>
          <span>{fmtSmart(timeline.segments[segs.length - 1]?.end_ts ?? 0)}</span>
        </div>

        <div className="behavior-legend">
          {(['transit', 'low_speed', 'stationary'] as const).map((k) => (
            <span key={k}>
              <i style={{ background: TIMELINE_COLORS[k] }} />
              {k === 'transit' ? '航行' : k === 'low_speed' ? '低速' : '静止'}
            </span>
          ))}
          <span>
            <i className="hatch" />
            观测中断
          </span>
          <span>
            <i style={{ background: '#34d399', boxShadow: '0 0 0 1px #34d399' }} />
            疑似捕捞
          </span>
        </div>

        {sum && (
          <div className="behavior-facts">
            <span>碎片段 {sum.segments}</span>
            <span>疑似捕捞 {sum.fishing_candidate_count} 段</span>
            <span>航迹 {sum.distance_nm} nm</span>
            <span>报告间隔中位 {sum.median_interval_s}s</span>
          </div>
        )}

        {timeline.narrative && <p className="behavior-text">{timeline.narrative}</p>}

        {quality?.advice && (
          <p className="behavior-advice">
            <b>{GRADE_LABEL[grade]}</b> · {quality.advice}
            {!!quality.flags?.length && (
              <span className="qflags">
                {quality.flags.map((f) => (
                  <em key={f}>{FLAG_TEXT[f] || f}</em>
                ))}
              </span>
            )}
          </p>
        )}
      </div>

      <div className="section-title">片段明细</div>
      <div className="seg-list">
        {segs
          .slice()
          .reverse()
          .slice(0, 14)
          .map((s, i) => {
            const isGap = s.activity === 'observation_gap'
            const isFish = s.sub_activity === 'fishing_candidate'
            const isPort = s.sub_activity === 'port_stay'
            const isAnchor = s.sub_activity === 'anchorage_candidate'
            return (
              <div
                className={`seg ${isGap ? 'is-gap' : ''}`}
                key={s.seg_id || i}
                onClick={() => {
                  if (!isGap && s.lat != null && s.lon != null) p.onLocate(s.lat, s.lon, (s.start_ts + s.end_ts) / 2)
                }}
                style={{ cursor: isGap || s.lat == null ? 'default' : 'pointer' }}
              >
                <i
                  className="seg-dot"
                  style={{
                    background: isGap ? 'transparent' : TIMELINE_COLORS[s.activity],
                    border: isGap ? '1px dashed #7a2f36' : undefined,
                  }}
                />
                <span className="seg-label">
                  {s.label}
                  {isFish && <em className="seg-fish">疑似捕捞</em>}
                  {isPort && (
                    <em className="seg-port" title={s.sub_label || '距最近港口在港半径内'}>
                      在港
                    </em>
                  )}
                  {isAnchor && (
                    <em className="seg-anch" title={s.sub_label || '不在任何已知港口范围内'}>
                      锚泊
                    </em>
                  )}
                  {s.reliability.grade === 'poor' && (
                    <em className="seg-bad" title={s.reliability.flags.map((f) => FLAG_TEXT[f] || f).join('、')}>
                      质量差
                    </em>
                  )}
                </span>
                <span className="seg-time">
                  {fmtSmart(s.start_ts)}–
                  {new Date(s.start_ts * 1000).toDateString() === new Date(s.end_ts * 1000).toDateString()
                    ? fmtTime(s.end_ts)
                    : fmtSmart(s.end_ts)}
                </span>
                <span className="seg-dur">{fmtDur(s.duration_min)}</span>
                <i
                  className="rel-dot"
                  style={{ background: RELIABILITY_COLORS[s.reliability.grade] }}
                  title={`观测可靠性：${s.reliability.grade}`}
                />
              </div>
            )
          })}
      </div>

      <DnaBlock fp={fingerprint} hours={timeline.hours} />

      {timeline.caveat && <div className="hot-caveat">{timeline.caveat}</div>}
    </>
  )
}
