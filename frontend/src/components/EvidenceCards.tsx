import { useEffect, useState } from 'react'
import type { EvidenceCard } from '../types'
import { EVIDENCE_KIND_LABELS } from '../types'
import { api } from '../api'

interface Props {
  mmsi: number
  hours: number
}

const REVIEW_LABELS: Record<string, string> = {
  unreviewed: '未复核',
  confirmed: '已确认',
  rejected: '已排除',
  unverifiable: '无法核实',
}

function fmt(ts: number) {
  const d = new Date(ts * 1000)
  return `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} ${String(
    d.getHours(),
  ).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

function Card({ card }: { card: EvidenceCard }) {
  const [open, setOpen] = useState(false)
  const observed = card.evidence.filter((e) => e.kind === 'observed')
  const inferred = card.evidence.filter((e) => e.kind === 'inferred')
  const missing = card.evidence.filter((e) => e.kind === 'missing')

  return (
    <div className="ev-card">
      <div className="ev-head">
        <span className="ev-title">{card.headline}</span>
        <span className={`ev-rev ${card.review_status}`}>{REVIEW_LABELS[card.review_status] || card.review_status}</span>
      </div>
      <div className="ev-time">
        {fmt(card.start_ts)} → {fmt(card.end_ts)}
        {card.attribution_reason && <em>{card.attribution_reason}</em>}
      </div>

      {[
        ['observed', observed],
        ['inferred', inferred],
      ].map(([kind, items]) =>
        (items as typeof observed).length ? (
          <div className={`ev-block ${kind}`} key={kind as string}>
            <div className="ev-block-k">{EVIDENCE_KIND_LABELS[kind as string]}</div>
            {(items as typeof observed).map((e, i) => (
              <div className="ev-row" key={i}>
                <span className="ev-claim">{e.claim}</span>
                <span className="ev-val">{e.value}</span>
                <span className="ev-detail">{e.detail}</span>
              </div>
            ))}
          </div>
        ) : null,
      )}

      {(missing.length > 0 || card.unexplained.length > 0) && (
        <div className="ev-block missing">
          <div className="ev-block-k">无法得知</div>
          {missing.map((e, i) => (
            <div className="ev-row" key={`m${i}`}>
              <span className="ev-claim">{e.claim}</span>
              <span className="ev-detail">{e.detail}</span>
            </div>
          ))}
          {card.unexplained.map((u, i) => (
            <div className="ev-row" key={`u${i}`}>
              <span className="ev-detail">{u}</span>
            </div>
          ))}
        </div>
      )}

      <button className="ev-more" onClick={() => setOpen(!open)}>
        {open ? '收起替代解释' : `替代解释 ${card.alternative_explanations.length} 条`}
      </button>
      {open && (
        <ul className="ev-alts">
          {card.alternative_explanations.map((a, i) => (
            <li key={i}>{a}</li>
          ))}
          {card.caveat && <li className="ev-caveat">{card.caveat}</li>}
        </ul>
      )}
    </div>
  )
}

export default function EvidenceCards({ mmsi, hours }: Props) {
  const [cards, setCards] = useState<EvidenceCard[] | null>(null)
  const [loading, setLoading] = useState(false)
  const [expanded, setExpanded] = useState(false)

  useEffect(() => {
    let alive = true
    setLoading(true)
    setCards(null)
    // 断和捕捞是两种事件，各取一次；任一没有记录都不算错误（接口会 404）
    Promise.allSettled([api.evidence(mmsi, hours, 'gap', 4), api.evidence(mmsi, hours, 'fishing', 4)])
      .then(([g, f]) => {
        if (!alive) return
        const merged = [
          ...(g.status === 'fulfilled' ? g.value.cards ?? [] : []),
          ...(f.status === 'fulfilled' ? f.value.cards ?? [] : []),
        ].sort((a, b) => b.start_ts - a.start_ts)
        setCards(merged)
      })
      .finally(() => {
        if (alive) setLoading(false)
      })
    return () => {
      alive = false
    }
  }, [mmsi, hours])

  if (loading) return <div className="empty" style={{ padding: '10px 16px' }}>生成证据卡…</div>
  if (!cards) return null
  if (!cards.length)
    return (
      <div className="empty" style={{ padding: '10px 16px' }}>
        该窗口内没有 AIS 中断或疑似捕捞片段，因此没有证据卡
      </div>
    )

  const shown = expanded ? cards : cards.slice(0, 2)
  return (
    <>
      {shown.map((c) => (
        <Card key={c.card_id} card={c} />
      ))}
      {cards.length > 2 && (
        <button className="ev-more" style={{ margin: '0 12px 10px' }} onClick={() => setExpanded(!expanded)}>
          {expanded ? '收起' : `还有 ${cards.length - 2} 张证据卡`}
        </button>
      )}
      <div className="ev-note">
        证据卡只陈述可复核的事实与统计对比，不对中断原因下结论；「更可能来自船舶侧」不等于「故意关闭 AIS」。
      </div>
    </>
  )
}
