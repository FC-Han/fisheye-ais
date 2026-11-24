import { useEffect, useRef, useState } from 'react'
import type { AgentReply } from '../types'

interface Msg {
  role: 'user' | 'assistant'
  content: string
  tools?: AgentReply['tools']
  map?: AgentReply['map']
  note?: string
  mode?: string
  model?: string
}

const SUGGESTIONS = [
  '现在哪里渔船作业最集中？',
  '东海过去 24 小时有渔船热点吗？',
  '最近 24 小时有哪些 AIS 中断候选？',
  '这些热点里哪艘船在作业？',
]

const TOOL_LABEL: Record<string, string> = {
  query_fishing_hotspots: '热点聚合',
  get_vessel_behavior: '运动状态',
  get_vessel_track: '轨迹',
  get_vessel_info: '船舶档案',
  query_fishing_activity: '捕捞事件',
  get_gap_events: '中断事件',
  query_geofence_events: '围栏事件',
  get_live_vessels: '实时船位',
  get_stream_health: '链路状态',
  find_vessels: '船舶检索',
}

/** 轻量渲染：只处理 **加粗**，其余原样保留换行 */
function Rich({ text }: { text: string }) {
  const parts = text.split(/(\*\*[^*]+\*\*)/g)
  return (
    <>
      {parts.map((p, i) =>
        p.startsWith('**') && p.endsWith('**') ? <strong key={i}>{p.slice(2, -2)}</strong> : <span key={i}>{p}</span>,
      )}
    </>
  )
}

export default function AgentChat({
  onAsk,
  onFocus,
  seed,
}: {
  onAsk: (q: string, history: Array<{ role: string; content: string }>) => Promise<AgentReply>
  onFocus: (m: AgentReply['map']) => void
  seed?: { q: string; a: AgentReply } | null
}) {
  const [msgs, setMsgs] = useState<Msg[]>([
    {
      role: 'assistant',
      content:
        '我是 FishEye 分析助手，接的是这台机器上正在跑的实时 AIS 数据库。\n\n你可以直接问：**哪片海域渔船最集中**、**某艘船最近在干什么**、**哪些船信号消失过**。所有结论都会标明是观测、推断还是无数据。',
    },
  ])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const logRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (seed) {
      setMsgs((m) => [
        ...m,
        { role: 'user', content: seed.q },
        { role: 'assistant', content: seed.a.answer, tools: seed.a.tools, map: seed.a.map, note: seed.a.note, mode: seed.a.mode },
      ])
    }
  }, [seed])

  useEffect(() => {
    logRef.current?.scrollTo({ top: logRef.current.scrollHeight, behavior: 'smooth' })
  }, [msgs, busy])

  async function send(text?: string) {
    const q = (text ?? input).trim()
    if (!q || busy) return
    setInput('')
    setMsgs((m) => [...m, { role: 'user', content: q }])
    setBusy(true)
    try {
      const history = msgs
        .slice(-6)
        .filter((m) => m.content)
        .map((m) => ({ role: m.role, content: m.content }))
      const a = await onAsk(q, history)
      setMsgs((m) => [
        ...m,
        { role: 'assistant', content: a.answer, tools: a.tools, map: a.map, note: a.note, mode: a.mode, model: a.model },
      ])
      if (a.map && (a.map.focus_mmsi || a.map.bbox || a.map.mmsis?.length || a.map.hotspots?.length)) onFocus(a.map)
    } catch (e: any) {
      setMsgs((m) => [...m, { role: 'assistant', content: `查询失败：${e.message}` }])
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="agent">
      <div className="agent-log" ref={logRef}>
        {msgs.map((m, i) => (
          <div className={`msg ${m.role === 'user' ? 'user' : 'bot'}`} key={i}>
            <div className="bubble">
              <Rich text={m.content} />
            </div>
            {m.tools && m.tools.length > 0 && (
              <div className="tool-chips">
                {m.tools.map((t, j) => (
                  <span className={`tool-chip ${t.ok ? '' : 'bad'}`} key={j}>
                    {TOOL_LABEL[t.name] || t.name}
                    {Object.keys(t.args || {}).length > 0 && (
                      <em>
                        {Object.entries(t.args || {})
                          .filter(([, v]) => v !== undefined && v !== null && v !== '')
                          .map(([k, v]) =>
                            Array.isArray(v)
                              ? `${k}=[${v.length}]`
                              : `${k}=${typeof v === 'string' && v.length > 10 ? v.slice(0, 10) + '…' : v}`,
                          )
                          .join(' · ')}
                      </em>
                    )}
                  </span>
                ))}
              </div>
            )}
            {m.role === 'assistant' && m.map && (m.map.focus_mmsi || m.map.bbox || m.map.hotspots?.length) && (
              <button className="msg-jump" onClick={() => onFocus(m.map!)}>
                ⌖ 在地图上定位这次结果
              </button>
            )}
            {(m.note || m.mode) && (
              <div className="msg-note">
                {m.mode === 'llm' ? `模型 ${m.model || 'LLM'} · 工具调用` : m.mode === 'rules' ? '规则解析模式' : ''}
                {m.note ? ` · ${m.note}` : ''}
              </div>
            )}
          </div>
        ))}
        {busy && (
          <div className="msg bot">
            <div className="bubble" style={{ color: 'var(--muted)' }}>
              <span className="loading-dot" /> 正在查询数据库并分析…
            </div>
          </div>
        )}
      </div>

      <div className="suggest">
        {SUGGESTIONS.map((s) => (
          <button key={s} onClick={() => send(s)} disabled={busy}>
            {s}
          </button>
        ))}
      </div>

      <div className="agent-input">
        <textarea
          rows={1}
          placeholder="问点什么… 例：北海南部这艘船在作业吗"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault()
              send()
            }
          }}
        />
        <button className="send-btn" onClick={() => send()} disabled={busy || !input.trim()}>
          ↑
        </button>
      </div>
    </div>
  )
}
