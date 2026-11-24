import { forwardRef, useEffect, useImperativeHandle, useRef } from 'react'
import maplibregl, { LngLatBoundsLike, Map as MLMap } from 'maplibre-gl'
import type { FishEvent, Hotspot, TrackPoint, TrackResponse, Vessel, Zone } from '../types'
import { IDENTITY_COLORS } from '../types'
import { BaseKey, graticule, styleFor } from '../basemaps'

const EMPTY = { type: 'FeatureCollection' as const, features: [] as any[] }

function vesselIcon(color: string) {
  const size = 26
  const c = document.createElement('canvas')
  c.width = size
  c.height = size
  const ctx = c.getContext('2d')!
  ctx.clearRect(0, 0, size, size)
  ctx.beginPath()
  ctx.moveTo(size / 2, 2)
  ctx.lineTo(size - 5, size - 4)
  ctx.lineTo(size / 2, size - 8)
  ctx.lineTo(5, size - 4)
  ctx.closePath()
  ctx.fillStyle = color
  ctx.fill()
  ctx.lineWidth = 1.6
  ctx.strokeStyle = 'rgba(3,8,14,0.85)'
  ctx.stroke()
  return ctx.getImageData(0, 0, size, size)
}

function bboxOf(pts: TrackPoint[]): LngLatBoundsLike | null {
  if (!pts.length) return null
  let minLon = 180,
    maxLon = -180,
    minLat = 90,
    maxLat = -90
  for (const p of pts) {
    minLon = Math.min(minLon, p.lon)
    maxLon = Math.max(maxLon, p.lon)
    minLat = Math.min(minLat, p.lat)
    maxLat = Math.max(maxLat, p.lat)
  }
  return [
    [minLon, minLat],
    [maxLon, maxLat],
  ]
}

export interface MapHandle {
  flyTo: (lat: number, lon: number, zoom?: number) => void
  fitTrack: (pts: TrackPoint[]) => void
  fitBbox: (bbox: number[]) => void
  zoomToHotspot: (h: Hotspot) => void
}

interface Props {
  vessels: Vessel[]
  events: FishEvent[]
  zones: Zone[]
  track: TrackResponse | null
  hotspots: Hotspot[]
  showHeat: boolean
  selectedMmsi: number | null
  playback: TrackPoint | null
  follow: boolean
  base: BaseKey
  onSelectVessel: (mmsi: number) => void
  onSelectEvent: (ev: FishEvent) => void
  onSelectHotspot: (h: Hotspot) => void
  onUserPan: () => void
  onViewportChange: (bbox: [number, number, number, number]) => void
  onClusterZoom: (lat: number, lon: number, zoom: number) => void
}

const MapView = forwardRef<MapHandle, Props>(function MapView(props, ref) {
  const { base, follow, selectedMmsi, onViewportChange } = props
  const holder = useRef<HTMLDivElement>(null)
  const map = useRef<MLMap | null>(null)
  const ready = useRef(false)
  const lastVessels = useRef<Map<number, Vessel>>(new Map())
  const tooltip = useRef<HTMLDivElement>(null)
  const latest = useRef(props)
  latest.current = props

  useImperativeHandle(ref, () => ({
    flyTo(lat: number, lon: number, zoom = 10) {
      map.current?.flyTo({ center: [lon, lat], zoom, duration: 1400 })
    },
    fitTrack(pts: TrackPoint[]) {
      const b = bboxOf(pts)
      if (b && map.current) map.current.fitBounds(b, { padding: 90, maxZoom: 12, duration: 1200 })
    },
    fitBbox(bbox: number[]) {
      const [minlat, minlon, maxlat, maxlon] = bbox
      map.current?.fitBounds(
        [
          [minlon, minlat],
          [maxlon, maxlat],
        ],
        { padding: 80, duration: 1200 },
      )
    },
    zoomToHotspot(h: Hotspot) {
      map.current?.flyTo({ center: [h.lon, h.lat], zoom: 7, duration: 1300 })
    },
  }))

  // ---------- 图层搭建（可在切换底图后重复调用） ----------
  function setup() {
    const m = map.current
    if (!m || m.getSource('vessels')) return
    for (const [key, color] of Object.entries(IDENTITY_COLORS)) {
      const id = `v-${key}`
      if (!m.hasImage(id)) m.addImage(id, vesselIcon(color))
    }

    m.addSource('zones', { type: 'geojson', data: EMPTY })
    m.addLayer({
      id: 'zones-fill',
      type: 'fill',
      source: 'zones',
      paint: { 'fill-color': '#4b9fe1', 'fill-opacity': 0.05 },
    })
    m.addLayer({
      id: 'zones-line',
      type: 'line',
      source: 'zones',
      paint: { 'line-color': '#4b9fe1', 'line-width': 1.2, 'line-opacity': 0.5, 'line-dasharray': [3, 2] },
    })
    m.addLayer({
      id: 'zones-label',
      type: 'symbol',
      source: 'zones',
      layout: {
        'text-field': ['get', 'name'],
        'text-font': ['Noto Sans Regular'],
        'text-size': 10.5,
        'text-letter-spacing': 0.08,
      },
      paint: { 'text-color': '#7db6e4', 'text-halo-color': '#050a12', 'text-halo-width': 1.4 },
    })

    // ---- 热点热力 ----
    m.addSource('hotspots', { type: 'geojson', data: EMPTY })
    m.addLayer({
      id: 'hotspots-heat',
      type: 'heatmap',
      source: 'hotspots',
      layout: { visibility: 'visible' },
      paint: {
        'heatmap-weight': ['interpolate', ['linear'], ['get', 'score'], 0, 0.05, 100, 1],
        'heatmap-intensity': ['interpolate', ['linear'], ['zoom'], 1, 1.1, 6, 2.0],
        'heatmap-radius': ['interpolate', ['linear'], ['zoom'], 1, 28, 4, 46, 9, 66],
        'heatmap-opacity': 0.85,
        'heatmap-color': [
          'interpolate',
          ['linear'],
          ['heatmap-density'],
          0,
          'rgba(6,30,48,0)',
          0.12,
          'rgba(23,120,120,0.35)',
          0.3,
          'rgba(52,211,153,0.5)',
          0.52,
          'rgba(242,176,53,0.62)',
          0.76,
          'rgba(240,100,95,0.75)',
          1,
          'rgba(255,92,132,0.9)',
        ],
      },
    })
    m.addLayer({
      id: 'hotspot-ring',
      type: 'circle',
      source: 'hotspots',
      filter: ['>=', ['get', 'score'], 18],
      paint: {
        'circle-radius': [
          'interpolate',
          ['linear'],
          ['zoom'],
          3,
          ['*', ['get', 'r'], 0.6],
          7,
          ['get', 'r'],
        ],
        'circle-color': 'rgba(0,0,0,0)',
        'circle-stroke-color': 'rgba(255,196,110,0.55)',
        'circle-stroke-width': ['interpolate', ['linear'], ['zoom'], 2, 0.8, 6, 1.4],
        'circle-stroke-opacity': 0.9,
      },
    })
    m.addLayer({
      id: 'hotspot-label',
      type: 'symbol',
      source: 'hotspots',
      filter: ['>=', ['get', 'score'], 40],
      layout: {
        'text-field': ['get', 'label'],
        'text-font': ['Noto Sans Regular'],
        'text-size': 11,
        'text-offset': [0, 1.6],
        'text-anchor': 'top',
        'text-allow-overlap': false,
      },
      paint: { 'text-color': '#ffd9a0', 'text-halo-color': '#050a12', 'text-halo-width': 1.7 },
    })

    // ---- 船舶 ----
    m.addSource('vessels', {
      type: 'geojson',
      data: EMPTY,
      cluster: true,
      clusterRadius: 46,
      clusterMaxZoom: 8,
    } as any)
    m.addLayer({
      id: 'vessels-cluster',
      type: 'circle',
      source: 'vessels',
      filter: ['has', 'point_count'],
      paint: {
        'circle-color': [
          'step',
          ['get', 'point_count'],
          'rgba(52,211,153,0.22)',
          50,
          'rgba(52,211,153,0.3)',
          200,
          'rgba(242,176,53,0.32)',
          800,
          'rgba(240,100,95,0.34)',
        ],
        'circle-radius': ['step', ['get', 'point_count'], 14, 50, 19, 200, 25, 800, 32],
        'circle-stroke-color': 'rgba(52,211,153,0.5)',
        'circle-stroke-width': 1,
      },
    })
    m.addLayer({
      id: 'vessels-cluster-count',
      type: 'symbol',
      source: 'vessels',
      filter: ['has', 'point_count'],
      layout: {
        'text-field': ['get', 'point_count_abbreviated'],
        'text-font': ['Noto Sans Regular'],
        'text-size': 11.5,
      },
      paint: { 'text-color': '#dce6f2' },
    })
    m.addLayer({
      id: 'vessels-fishing-halo',
      type: 'circle',
      source: 'vessels',
      filter: [
        'all',
        ['!', ['has', 'point_count']],
        ['in', ['get', 'identity'], ['literal', ['fishing', 'candidate']]],
      ],
      paint: {
        'circle-radius': ['interpolate', ['linear'], ['zoom'], 6, 6, 12, 15],
        'circle-color': ['match', ['get', 'identity'], 'fishing', '#34d399', '#f2b035'],
        'circle-opacity': 0.14,
        'circle-blur': 0.6,
      },
    })
    m.addLayer({
      id: 'vessels-point',
      type: 'symbol',
      source: 'vessels',
      filter: ['!', ['has', 'point_count']],
      layout: {
        'icon-image': [
          'match',
          ['get', 'identity'],
          'fishing',
          'v-fishing',
          'candidate',
          'v-candidate',
          'non_fishing',
          'v-non_fishing',
          'v-unknown',
        ],
        'icon-size': ['interpolate', ['linear'], ['zoom'], 4, 0.42, 9, 0.62, 14, 0.85],
        'icon-rotate': ['coalesce', ['get', 'cog'], 0],
        'icon-rotation-alignment': 'map',
        'icon-allow-overlap': true,
        'icon-ignore-placement': true,
      },
    })

    // ---- 轨迹 / 事件 / 回放 ----
    m.addSource('track-tail', { type: 'geojson', data: EMPTY })
    m.addLayer({
      id: 'track-tail-line',
      type: 'line',
      source: 'track-tail',
      paint: { 'line-color': 'rgba(52,211,153,0.35)', 'line-width': 2 },
      layout: { 'line-join': 'round', 'line-cap': 'round' },
    })

    m.addSource('track', { type: 'geojson', data: EMPTY })
    m.addLayer({
      id: 'track-line',
      type: 'line',
      source: 'track',
      paint: { 'line-color': 'rgba(125,182,228,0.55)', 'line-width': 1.5 },
      layout: { 'line-join': 'round', 'line-cap': 'round' },
    })

    m.addSource('track-fishing', { type: 'geojson', data: EMPTY })
    m.addLayer({
      id: 'track-fishing-line',
      type: 'line',
      source: 'track-fishing',
      paint: { 'line-color': '#34d399', 'line-width': 3.4, 'line-opacity': 0.9 },
      layout: { 'line-join': 'round', 'line-cap': 'round' },
    })

    m.addSource('gaps', { type: 'geojson', data: EMPTY })
    m.addLayer({
      id: 'gaps-point',
      type: 'circle',
      source: 'gaps',
      paint: {
        'circle-radius': 6,
        'circle-color': 'rgba(240,100,95,0.18)',
        'circle-stroke-color': '#f0645f',
        'circle-stroke-width': 1.4,
      },
    })

    m.addSource('selected', { type: 'geojson', data: EMPTY })
    m.addLayer({
      id: 'selected-ring',
      type: 'circle',
      source: 'selected',
      paint: {
        'circle-radius': 14,
        'circle-color': 'rgba(52,211,153,0.08)',
        'circle-stroke-color': '#34d399',
        'circle-stroke-width': 1.6,
      },
    })
    m.addLayer({
      id: 'selected-label',
      type: 'symbol',
      source: 'selected',
      layout: {
        'text-field': ['get', 'label'],
        'text-font': ['Noto Sans Regular'],
        'text-size': 11,
        'text-offset': [0, -1.9],
        'text-anchor': 'bottom',
        'text-allow-overlap': true,
      },
      paint: { 'text-color': '#d9fff1', 'text-halo-color': '#04120c', 'text-halo-width': 1.8 },
    })

    m.addSource('events', { type: 'geojson', data: EMPTY })
    m.addLayer({
      id: 'events-point',
      type: 'circle',
      source: 'events',
      paint: {
        'circle-radius': ['interpolate', ['linear'], ['zoom'], 3, 3.2, 10, 5.6],
        'circle-color': [
          'match',
          ['get', 'kind'],
          'fishing',
          '#34d399',
          'gap',
          '#f0645f',
          'geofence',
          '#4b9fe1',
          '#888',
        ],
        'circle-stroke-color': '#050a12',
        'circle-stroke-width': 1.2,
        'circle-opacity': 0.9,
      },
    })

    m.addSource('playback', { type: 'geojson', data: EMPTY })
    m.addLayer({
      id: 'playback-point',
      type: 'circle',
      source: 'playback',
      paint: {
        'circle-radius': 7,
        'circle-color': '#ffffff',
        'circle-stroke-color': '#34d399',
        'circle-stroke-width': 2.5,
      },
    })

    ready.current = true
  }

  // ---------- 数据推送 ----------
  function pushVessels() {
    const m = map.current
    if (!m) return
    const store = lastVessels.current
    for (const v of latest.current.vessels) store.set(v.mmsi, v)
    if (store.size > 60000) lastVessels.current = new Map(Array.from(store).slice(-50000))
    const features = Array.from(lastVessels.current.values()).map((v) => ({
      type: 'Feature' as const,
      geometry: { type: 'Point' as const, coordinates: [v.lon, v.lat] },
      properties: { mmsi: v.mmsi, name: v.ship_name || '', identity: v.identity, sog: v.sog, cog: v.cog },
    }))
    ;(m.getSource('vessels') as any)?.setData({ type: 'FeatureCollection', features })
  }

  function pushZones() {
    const m = map.current
    if (!m) return
    const polys = latest.current.zones.map((z) => {
      const ring = [...z.polygon]
      if (ring.length && (ring[0][0] !== ring[ring.length - 1][0] || ring[0][1] !== ring[ring.length - 1][1]))
        ring.push(ring[0])
      return {
        type: 'Feature' as const,
        geometry: { type: 'Polygon' as const, coordinates: [ring] },
        properties: { name: z.name },
      }
    })
    ;(m.getSource('zones') as any)?.setData({ type: 'FeatureCollection', features: polys })
  }

  function pushHotspots() {
    const m = map.current
    if (!m) return
    const spots = latest.current.hotspots
    const heat = m.getLayer('hotspots-heat')
    if (heat) m.setLayoutProperty('hotspots-heat', 'visibility', latest.current.showHeat ? 'visible' : 'none')
    const features = spots.map((h) => ({
      type: 'Feature' as const,
      geometry: { type: 'Point' as const, coordinates: [h.lon, h.lat] },
      properties: {
        score: h.score,
        area: h.area,
        r: 8 + (h.score / 100) * 14,
        label: `${h.area} ${Math.round(h.score)}`,
        h: JSON.stringify(h),
      },
    }))
    ;(m.getSource('hotspots') as any)?.setData({ type: 'FeatureCollection', features })
  }

  function pushEvents() {
    const m = map.current
    if (!m) return
    const features = latest.current.events
      .filter((e) => e.lat != null && e.lon != null)
      .slice(0, 400)
      .map((e) => ({
        type: 'Feature' as const,
        geometry: { type: 'Point' as const, coordinates: [e.lon!, e.lat!] },
        properties: { kind: e.kind, mmsi: e.mmsi, ev: JSON.stringify(e) },
      }))
    ;(m.getSource('events') as any)?.setData({ type: 'FeatureCollection', features })
  }

  function pushTrack() {
    const m = map.current
    if (!m) return
    const track = latest.current.track
    const src = m.getSource('track') as any
    const fishSrc = m.getSource('track-fishing') as any
    const gapSrc = m.getSource('gaps') as any
    const tailSrc = m.getSource('track-tail') as any
    if (!track || track.track.length < 2) {
      src?.setData(EMPTY)
      fishSrc?.setData(EMPTY)
      gapSrc?.setData(EMPTY)
      tailSrc?.setData(EMPTY)
      return
    }
    src.setData({
      type: 'FeatureCollection',
      features: [
        {
          type: 'Feature',
          geometry: { type: 'LineString', coordinates: track.track.map((p) => [p.lon, p.lat]) },
          properties: {},
        },
      ],
    })
    tailSrc.setData({
      type: 'FeatureCollection',
      features: [
        {
          type: 'Feature',
          geometry: {
            type: 'LineString',
            coordinates: track.track.slice(-26).map((p) => [p.lon, p.lat]),
          },
          properties: {},
        },
      ],
    })

    const fishFeatures: any[] = []
    for (const ev of track.fishing_events) {
      const seg = track.track.filter((p) => p.ts >= ev.start_ts - 60 && p.ts <= ev.end_ts + 60)
      if (seg.length < 2) continue
      fishFeatures.push({
        type: 'Feature',
        geometry: { type: 'LineString', coordinates: seg.map((p) => [p.lon, p.lat]) },
        properties: { confidence: ev.confidence },
      })
    }
    fishSrc.setData({ type: 'FeatureCollection', features: fishFeatures })
    gapSrc.setData({
      type: 'FeatureCollection',
      features: track.gap_events
        .filter((g) => g.lat != null)
        .map((g) => ({
          type: 'Feature',
          geometry: { type: 'Point', coordinates: [g.lon!, g.lat!] },
          properties: {},
        })),
    })
  }

  function pushSelected() {
    const m = map.current
    if (!m) return
    const mmsi = latest.current.selectedMmsi
    const v = mmsi ? lastVessels.current.get(mmsi) : null
    ;(m.getSource('selected') as any)?.setData(
      v
        ? {
            type: 'FeatureCollection',
            features: [
              {
                type: 'Feature',
                geometry: { type: 'Point', coordinates: [v.lon, v.lat] },
                properties: {
                  label: `${v.ship_name || 'MMSI ' + v.mmsi} · ${v.sog != null ? v.sog + ' kn' : '速度未知'}`,
                },
              },
            ],
          }
        : EMPTY,
    )
  }

  function pushPlayback() {
    const m = map.current
    if (!m) return
    const pb = latest.current.playback
    ;(m.getSource('playback') as any)?.setData(
      pb
        ? {
            type: 'FeatureCollection',
            features: [
              { type: 'Feature', geometry: { type: 'Point', coordinates: [pb.lon, pb.lat] }, properties: {} },
            ],
          }
        : EMPTY,
    )
  }

  function pushAll() {
    pushZones()
    pushHotspots()
    pushVessels()
    pushEvents()
    pushTrack()
    pushSelected()
    pushPlayback()
  }

  // ---------- 初始化 ----------
  useEffect(() => {
    if (!holder.current || map.current) return
    const m = new maplibregl.Map({
      container: holder.current,
      style: styleFor(latest.current.base),
      center: [25, 22],
      zoom: 1.9,
      minZoom: 1,
      maxZoom: 13,
      attributionControl: { compact: true },
      dragRotate: false,
      pitchWithRotate: false,
    })
    map.current = m
    ;(window as any).__fisheyeMap = m
    m.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'bottom-right')
    m.addControl(new maplibregl.ScaleControl({ unit: 'metric', maxWidth: 90 }), 'bottom-right')

    m.on('load', () => {
      if (m.getSource('graticule')) (m.getSource('graticule') as any).setData(graticule(20))
      setup()
      pushAll()
      emitViewport()
    })

    function emitViewport() {
      const b = m.getBounds()
      onViewportChange([b.getSouth(), b.getWest(), b.getNorth(), b.getEast()])
    }
    m.on('moveend', emitViewport)
    // 用户手动拖图时退出追踪，避免地图被强行拉回
    m.on('dragstart', () => latest.current.onUserPan?.())

    m.on('click', 'vessels-cluster', (e: any) => {
      const f = e.features?.[0]
      if (!f) return
      const coords = f.geometry.coordinates
      latest.current.onClusterZoom(coords[1], coords[0], m.getZoom() + 2)
    })
    m.on('mouseenter', 'vessels-cluster', () => (m.getCanvas().style.cursor = 'pointer'))
    m.on('mouseleave', 'vessels-cluster', () => (m.getCanvas().style.cursor = ''))

    m.on('click', 'vessels-point', (e: any) => {
      const f = e.features?.[0]
      if (f) latest.current.onSelectVessel(Number(f.properties.mmsi))
    })
    m.on('mousemove', 'vessels-point', (e: any) => {
      const f = e.features?.[0]
      if (!f || !tooltip.current) return
      const p = f.properties
      m.getCanvas().style.cursor = 'pointer'
      tooltip.current.style.display = 'block'
      tooltip.current.style.left = `${e.point.x}px`
      tooltip.current.style.top = `${e.point.y}px`
      tooltip.current.innerHTML = `<div>${p.name || '未知船名'}</div><div class="m">MMSI ${p.mmsi} · ${
        p.sog ?? '-'
      } kn</div>`
    })
    m.on('mouseleave', 'vessels-point', () => {
      m.getCanvas().style.cursor = ''
      if (tooltip.current) tooltip.current.style.display = 'none'
    })

    m.on('click', 'hotspot-ring', (e: any) => {
      const f = e.features?.[0]
      if (f?.properties?.h) latest.current.onSelectHotspot(JSON.parse(f.properties.h))
    })
    m.on('mousemove', 'hotspot-ring', (e: any) => {
      const f = e.features?.[0]
      if (!f || !tooltip.current) return
      m.getCanvas().style.cursor = 'pointer'
      tooltip.current.style.display = 'block'
      tooltip.current.style.left = `${e.point.x}px`
      tooltip.current.style.top = `${e.point.y}px`
      tooltip.current.innerHTML = `<div>${f.properties.area}</div><div class="m">聚集度 ${Math.round(
        f.properties.score,
      )} · 点击查看</div>`
    })
    m.on('mouseleave', 'hotspot-ring', () => {
      m.getCanvas().style.cursor = ''
      if (tooltip.current) tooltip.current.style.display = 'none'
    })

    m.on('click', 'events-point', (e: any) => {
      const f = e.features?.[0]
      if (f) latest.current.onSelectEvent(JSON.parse(f.properties.ev))
    })
    m.on('mouseenter', 'events-point', () => (m.getCanvas().style.cursor = 'pointer'))
    m.on('mouseleave', 'events-point', () => (m.getCanvas().style.cursor = ''))

    return () => {
      m.remove()
      map.current = null
      ready.current = false
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // ---------- 底图切换 ----------
  const baseInit = useRef(true)
  useEffect(() => {
    if (baseInit.current) {
      baseInit.current = false
      return
    }
    const m = map.current
    if (!m) return
    let done = false
    let tries = 0
    const apply = () => {
      if (done) return
      if (!m.isStyleLoaded()) {
        // 外部底图可能较慢，最多轮询约 6 秒
        if (++tries < 120) requestAnimationFrame(apply)
        return
      }
      done = true
      setup()
      pushAll()
      if (m.getSource('graticule')) (m.getSource('graticule') as any).setData(graticule(20))
    }
    m.setStyle(styleFor(base) as any)
    m.once('styledata', apply)
    const t = window.setTimeout(apply, 600)
    return () => window.clearTimeout(t)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [base])

  // ---------- 数据变化 ----------
  useEffect(() => {
    if (ready.current) pushVessels()
  }, [props.vessels])
  useEffect(() => {
    if (ready.current) pushZones()
  }, [props.zones])
  useEffect(() => {
    if (ready.current) pushEvents()
  }, [props.events])
  useEffect(() => {
    if (ready.current) pushTrack()
  }, [props.track])
  useEffect(() => {
    if (ready.current) pushHotspots()
  }, [props.hotspots, props.showHeat])
  useEffect(() => {
    if (ready.current) pushSelected()
  }, [props.selectedMmsi, props.vessels])
  useEffect(() => {
    if (ready.current) pushPlayback()
  }, [props.playback])

  // ---------- 单船追踪：跟随目标船自动居中 ----------
  useEffect(() => {
    const m = map.current
    if (!m || !follow || !selectedMmsi) return
    const v = lastVessels.current.get(selectedMmsi)
    if (!v) return
    const c = m.getCenter()
    const far = Math.abs(c.lng - v.lon) > 0.004 || Math.abs(c.lat - v.lat) > 0.004
    if (!far) return
    m.easeTo({ center: [v.lon, v.lat], zoom: Math.max(m.getZoom(), 8), duration: 1400 })
  }, [props.vessels, follow, selectedMmsi])

  return (
    <div style={{ position: 'absolute', inset: 0 }}>
      <div id="map" ref={holder} />
      <div className="map-tooltip" ref={tooltip} style={{ display: 'none' }} />
    </div>
  )
})

export default MapView
