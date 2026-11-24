import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import MapView, { MapHandle } from './components/MapView'
import TopBar from './components/TopBar'
import LeftRail from './components/LeftRail'
import EventsPanel from './components/EventsPanel'
import HotspotsPanel from './components/HotspotsPanel'
import VesselPanel from './components/VesselPanel'
import AgentChat from './components/AgentChat'
import { api } from './api'
import { useVesselStream } from './hooks/useVesselStream'
import type { BaseKey } from './basemaps'
import type {
  AgentReply,
  Behavior,
  BehaviorTimeline,
  FingerprintResult,
  FishEvent,
  Hotspot,
  HotspotsResponse,
  Identity,
  ObservationQuality,
  StreamStatus,
  Summary,
  TrackResponse,
  Vessel,
  Zone,
} from './types'

type Tab = 'events' | 'hotspots' | 'vessel' | 'agent'

const DEFAULT_REGION = 'global'

export default function App() {
  const mapRef = useRef<MapHandle>(null)

  const [bbox, setBbox] = useState<[number, number, number, number]>([-20, 95, 50, 145])
  const [debounced, setDebounced] = useState(bbox)
  const [region, setRegion] = useState(DEFAULT_REGION)
  const [regions, setRegions] = useState<Array<{ key: string; name: string; bbox: number[] }>>([])
  const [base, setBase] = useState<BaseKey>(() => {
    const saved = localStorage.getItem('fisheye.base')
    return saved === 'satellite' || saved === 'ocean' ? saved : 'deep'
  })

  const [summary, setSummary] = useState<Summary | null>(null)
  const [stream, setStream] = useState<StreamStatus | null>(null)
  const [events, setEvents] = useState<FishEvent[]>([])
  const [eventsLoading, setEventsLoading] = useState(false)
  const [zones, setZones] = useState<Zone[]>([])

  const [hotdata, setHotdata] = useState<HotspotsResponse | null>(null)
  const [hotHours, setHotHours] = useState(24)
  const [hotLoading, setHotLoading] = useState(false)
  const [activeHotspot, setActiveHotspot] = useState<Hotspot | null>(null)

  const [visible, setVisible] = useState<Record<Identity, boolean>>({
    fishing: true,
    candidate: true,
    unknown: true,
    non_fishing: true,
  })
  const [showEvents, setShowEvents] = useState(true)
  const [showZones, setShowZones] = useState(true)
  const [showHeat, setShowHeat] = useState(true)

  const [tab, setTab] = useState<Tab>('hotspots')
  const [kinds, setKinds] = useState<Record<string, boolean>>({ fishing: true, gap: true, geofence: true })
  const [hours, setHours] = useState(24)

  const [selected, setSelected] = useState<Vessel | null>(null)
  const [track, setTrack] = useState<TrackResponse | null>(null)
  const [behavior, setBehavior] = useState<Behavior | null>(null)
  const [behaviorLoading, setBehaviorLoading] = useState(false)
  const [timeline, setTimeline] = useState<BehaviorTimeline | null>(null)
  const [quality, setQuality] = useState<ObservationQuality | null>(null)
  const [fingerprint, setFingerprint] = useState<FingerprintResult | null>(null)
  const [timelineLoading, setTimelineLoading] = useState(false)
  const [trackHours, setTrackHours] = useState(24)
  const [trackLoading, setTrackLoading] = useState(false)
  const [follow, setFollow] = useState(false)
  const [idx, setIdx] = useState(0)
  const [playing, setPlaying] = useState(false)
  const [agentSeed, setAgentSeed] = useState<{ q: string; a: AgentReply } | null>(null)

  const { vessels } = useVesselStream(debounced)

  // 视野防抖
  useEffect(() => {
    const t = window.setTimeout(() => setDebounced(bbox), 900)
    return () => window.clearTimeout(t)
  }, [bbox])

  // 概览 / 链路
  useEffect(() => {
    let alive = true
    const tick = async () => {
      try {
        const [s, st] = await Promise.all([api.summary(), api.stream()])
        if (alive) {
          setSummary(s)
          setStream(st)
        }
      } catch {
        /* 忽略瞬时失败 */
      }
    }
    tick()
    const iv = window.setInterval(tick, 10000)
    return () => {
      alive = false
      window.clearInterval(iv)
    }
  }, [])

  useEffect(() => {
    api.regions().then((r) => setRegions(r.regions)).catch(() => {})
    api.zones().then((z) => setZones(z.zones)).catch(() => {})
  }, [])

  useEffect(() => {
    localStorage.setItem('fisheye.base', base)
  }, [base])

  const loadEvents = useCallback(async () => {
    setEventsLoading(true)
    try {
      const r = await api.events({ hours, limit: 300 })
      setEvents(r.events)
    } catch {
      /* noop */
    } finally {
      setEventsLoading(false)
    }
  }, [hours])

  const loadHotspots = useCallback(async () => {
    setHotLoading(true)
    try {
      const r = await api.hotspots({ hours: hotHours, cell: 1.5, limit: 40 })
      setHotdata(r)
    } catch {
      /* noop */
    } finally {
      setHotLoading(false)
    }
  }, [hotHours])

  useEffect(() => {
    loadEvents()
    const iv = window.setInterval(loadEvents, 30000)
    return () => window.clearInterval(iv)
  }, [loadEvents])

  useEffect(() => {
    loadHotspots()
    const iv = window.setInterval(loadHotspots, 60000)
    return () => window.clearInterval(iv)
  }, [loadHotspots])

  const loadTrack = useCallback(async (mmsi: number, h: number) => {
    setTrackLoading(true)
    setBehaviorLoading(true)
    setTimelineLoading(true)
    try {
      const t = await api.track(mmsi, h)
      setTrack(t)
      setIdx(Math.max(0, t.track.length - 1))
    } catch {
      setTrack(null)
    } finally {
      setTrackLoading(false)
    }
    try {
      setBehavior(await api.behavior(mmsi, h))
    } catch {
      setBehavior(null)
    } finally {
      setBehaviorLoading(false)
    }
    // v0.2 分析层：时间线、观测质量、行为指纹，三个并行拉，任一失败不影响其它
    const [tl, q, fp] = await Promise.allSettled([
      api.timeline(mmsi, h),
      api.quality(mmsi, h),
      api.fingerprint(mmsi, 30),
    ])
    setTimeline(tl.status === 'fulfilled' ? tl.value : null)
    setQuality(q.status === 'fulfilled' ? q.value : null)
    setFingerprint(fp.status === 'fulfilled' ? fp.value : null)
    setTimelineLoading(false)
  }, [])

  const selectVessel = useCallback(
    async (mmsi: number, fly = true, switchTab = true) => {
      if (switchTab) setTab('vessel')
      // 把 MMSI 写进地址栏：刷新/分享都能回到同一艘船
      try {
        const u = new URL(window.location.href)
        u.searchParams.set('mmsi', String(mmsi))
        window.history.replaceState(null, '', u.toString())
      } catch {
        /* noop */
      }
      const known = vessels.find((v) => v.mmsi === mmsi)
      if (known) setSelected(known)
      else {
        try {
          const d = await api.vessel(mmsi)
          setSelected(d as Vessel)
        } catch {
          return
        }
      }
      loadTrack(mmsi, trackHours)
      if (fly) {
        try {
          const d = known || ((await api.vessel(mmsi)) as Vessel)
          mapRef.current?.flyTo(d.lat, d.lon, 9)
          setActiveHotspot(null)
        } catch {
          /* noop */
        }
      }
    },
    [vessels, trackHours, loadTrack],
  )

  // 首次加载时按 ?mmsi= 打开对应船舶（便于分享与复现同一艘船的分析）
  const deepLinked = useRef(false)
  useEffect(() => {
    if (deepLinked.current) return
    deepLinked.current = true
    const raw = new URLSearchParams(window.location.search).get('mmsi')
    const mmsi = raw ? Number(raw) : NaN
    if (Number.isFinite(mmsi) && mmsi > 0) selectVessel(mmsi, true)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // 选中的船在实时数据里更新
  useEffect(() => {
    if (!selected) return
    const fresh = vessels.find((v) => v.mmsi === selected.mmsi)
    if (fresh && fresh.ts !== selected.ts)
      setSelected({ ...selected, ...fresh, stats_24h: (selected as any).stats_24h } as unknown as Vessel)
  }, [vessels, selected])

  // 回放
  useEffect(() => {
    if (!playing || !track?.track.length) return
    const iv = window.setInterval(() => {
      setIdx((i) => {
        if (i >= track.track.length - 1) {
          setPlaying(false)
          return i
        }
        return i + 1
      })
    }, 130)
    return () => window.clearInterval(iv)
  }, [playing, track])

  // 切轨迹窗口时重载
  useEffect(() => {
    if (selected) loadTrack(selected.mmsi, trackHours)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [trackHours])

  // Esc：退出追踪 / 关闭面板
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return
      if (follow) setFollow(false)
      else if (selected) {
        setSelected(null)
        setTrack(null)
        setBehavior(null)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [follow, selected])

  const counts = useMemo(() => {
    const c: Record<string, number> = { fishing: 0, candidate: 0, unknown: 0, non_fishing: 0 }
    for (const v of vessels) c[v.identity] = (c[v.identity] || 0) + 1
    return c
  }, [vessels])

  const shownVessels = useMemo(() => vessels.filter((v) => visible[v.identity]), [vessels, visible])
  const fishingCount = (counts.fishing || 0) + (counts.candidate || 0)
  const vesselEvents = useMemo(
    () => (selected ? events.filter((e) => e.mmsi === selected.mmsi) : []),
    [events, selected],
  )

  const ask = useCallback(
    (q: string, history: Array<{ role: string; content: string }>) => api.chat(q, history),
    [],
  )

  const pickHotspot = useCallback((h: Hotspot) => {
    setActiveHotspot(h)
    setTab('hotspots')
    setFollow(false)
    mapRef.current?.zoomToHotspot(h)
  }, [])

  const focusFromAgent = useCallback(
    (m: AgentReply['map']) => {
      // 对话中只移动地图，不抢走用户正在看的 AI 面板
      setFollow(false)
      if (m.hotspots?.length) {
        const top = m.hotspots[0]
        setActiveHotspot(top)
        mapRef.current?.zoomToHotspot(top)
        return
      }
      if (m.focus_mmsi) selectVessel(m.focus_mmsi, true, false)
      else if (m.bbox) mapRef.current?.fitBbox(m.bbox)
      else if (m.mmsis?.length) {
        const v = vessels.find((x) => x.mmsi === m.mmsis![0])
        if (v) mapRef.current?.flyTo(v.lat, v.lon, 7)
      }
    },
    [selectVessel, vessels],
  )

  return (
    <>
      <MapView
        ref={mapRef}
        vessels={shownVessels}
        events={showEvents ? events.filter((e) => kinds[e.kind]) : []}
        zones={showZones ? zones : []}
        track={track}
        hotspots={hotdata?.hotspots ?? []}
        showHeat={showHeat}
        selectedMmsi={selected?.mmsi ?? null}
        playback={playing || track ? track?.track[Math.min(idx, Math.max(0, (track?.track.length ?? 1) - 1))] ?? null : null}
        follow={follow}
        base={base}
        onSelectVessel={(m) => selectVessel(m, false)}
        onSelectEvent={(ev) => {
          if (ev.lat != null) mapRef.current?.flyTo(ev.lat, ev.lon!, 9)
          selectVessel(ev.mmsi, false)
        }}
        onSelectHotspot={pickHotspot}
        onUserPan={() => setFollow(false)}
        onViewportChange={(b) => setBbox(b)}
        onClusterZoom={(lat, lon, z) => mapRef.current?.flyTo(lat, lon, z)}
      />

      <TopBar
        stream={stream}
        regions={regions}
        activeRegion={region}
        onRegion={(key, b) => {
          setRegion(key)
          setBbox(b as [number, number, number, number])
          mapRef.current?.fitBbox(b)
        }}
        onPickVessel={(m) => selectVessel(m, true)}
        totalVessels={vessels.length}
        fishingVessels={fishingCount}
        base={base}
        onBase={setBase}
      />

      <LeftRail
        summary={summary}
        stream={stream}
        counts={counts}
        visible={visible}
        onToggle={(k) => setVisible({ ...visible, [k]: !visible[k] })}
        showEvents={showEvents}
        showZones={showZones}
        showHeat={showHeat}
        onToggleEvents={() => setShowEvents(!showEvents)}
        onToggleZones={() => setShowZones(!showZones)}
        onToggleHeat={() => setShowHeat(!showHeat)}
        hotspots={hotdata?.hotspots ?? []}
        onPickHotspot={pickHotspot}
        onOpenHotspots={() => setTab('hotspots')}
      />

      <div className="right-panel">
        <div className="tabs">
          <button className={tab === 'hotspots' ? 'active' : ''} onClick={() => setTab('hotspots')}>
            热点
            <span className="tab-badge">{hotdata?.hotspot_count ?? 0}</span>
          </button>
          <button className={tab === 'events' ? 'active' : ''} onClick={() => setTab('events')}>
            事件<span className="tab-badge">{events.length}</span>
          </button>
          <button className={tab === 'vessel' ? 'active' : ''} onClick={() => setTab('vessel')}>
            船舶{selected ? <span className="tab-badge">1</span> : null}
          </button>
          <button className={tab === 'agent' ? 'active' : ''} onClick={() => setTab('agent')}>
            AI 助手
          </button>
        </div>

        {tab === 'hotspots' && (
          <HotspotsPanel
            data={hotdata}
            hours={hotHours}
            onHours={setHotHours}
            loading={hotLoading}
            active={activeHotspot}
            onPick={pickHotspot}
            onVessel={(m) => selectVessel(m, true)}
          />
        )}

        {tab === 'events' && (
          <EventsPanel
            events={events}
            kinds={kinds}
            onKinds={setKinds}
            hours={hours}
            onHours={setHours}
            loading={eventsLoading}
            onPick={(ev) => {
              if (ev.lat != null) mapRef.current?.flyTo(ev.lat, ev.lon!, 9)
              selectVessel(ev.mmsi, false)
            }}
          />
        )}

        {tab === 'vessel' && (
          <VesselPanel
            vessel={selected}
            track={track}
            behavior={behavior}
            behaviorLoading={behaviorLoading}
            timeline={timeline}
            timelineLoading={timelineLoading}
            quality={quality}
            fingerprint={fingerprint}
            onLocate={(lat, lon) => mapRef.current?.flyTo(lat, lon, 10)}
            events={vesselEvents}
            hours={trackHours}
            onHours={setTrackHours}
            playing={playing}
            onPlay={() => setPlaying(!playing)}
            idx={idx}
            onIdx={(i) => {
              setPlaying(false)
              setIdx(i)
            }}
            onPickEvent={(ev) => {
              if (ev.lat != null) mapRef.current?.flyTo(ev.lat, ev.lon!, 9)
            }}
            onClose={() => {
              setSelected(null)
              setTrack(null)
              setBehavior(null)
              setFollow(false)
            }}
            loading={trackLoading}
            follow={follow}
            onToggleFollow={() => setFollow(!follow)}
            onCenter={() => selected && mapRef.current?.flyTo(selected.lat, selected.lon, 9)}
          />
        )}

        {tab === 'agent' && <AgentChat onAsk={ask} onFocus={focusFromAgent} seed={agentSeed} />}
      </div>

      {follow && selected && (
        <div className="follow-chip">
          <span className="dot" />
          正在追踪 <b>{selected.ship_name || `MMSI ${selected.mmsi}`}</b>
          <button onClick={() => setFollow(false)}>停止</button>
        </div>
      )}

      <div className="bottom-bar">
        <span className="hint">滚轮缩放 · 点击船只看详情 · 点击热点进入区域</span>
        <span className="hint" style={{ pointerEvents: 'none' }}>
          仅统计已接收 AIS，空白不代表无船
        </span>
      </div>
    </>
  )
}
