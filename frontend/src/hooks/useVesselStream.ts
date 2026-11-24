import { useEffect, useRef, useState } from 'react'
import { wsUrl } from '../api'
import type { Vessel } from '../types'

export function useVesselStream(bbox: [number, number, number, number]) {
  const store = useRef<Map<number, Vessel>>(new Map())
  const [vessels, setVessels] = useState<Vessel[]>([])
  const [status, setStatus] = useState<'connecting' | 'open' | 'closed'>('connecting')
  const [lastPush, setLastPush] = useState(0)
  const key = bbox.map((n) => n.toFixed(2)).join(',')

  useEffect(() => {
    let ws: WebSocket | null = null
    let disposed = false
    let retry: number | undefined
    let flushTimer: number | undefined

    const flush = () => {
      flushTimer = undefined
      setVessels(Array.from(store.current.values()))
      setLastPush(Date.now())
    }

    const open = () => {
      if (disposed) return
      setStatus('connecting')
      try {
        ws = new WebSocket(wsUrl(bbox, 'all'))
      } catch {
        retry = window.setTimeout(open, 4000)
        return
      }
      ws.onopen = () => setStatus('open')
      ws.onmessage = (ev) => {
        let d: any
        try {
          d = JSON.parse(ev.data)
        } catch {
          return
        }
        if (d.type !== 'vessels' || !Array.isArray(d.vessels)) return
        if (d.full) {
          // 全量帧：按当前视野 + 时间窗裁剪，避免换区域后旧数据一直堆着
          const [minlat, minlon, maxlat, maxlon] = bbox
          const padLat = (maxlat - minlat) * 0.25
          const padLon = (maxlon - minlon) * 0.25
          const cutoff = Math.floor(Date.now() / 1000) - 900
          const keep = new Map<number, Vessel>()
          for (const [k, v] of store.current) {
            const inView =
              v.lat >= minlat - padLat &&
              v.lat <= maxlat + padLat &&
              v.lon >= minlon - padLon &&
              v.lon <= maxlon + padLon
            if (inView && v.ts >= cutoff) keep.set(k, v)
          }
          store.current = keep
        }
        for (const v of d.vessels) store.current.set(v.mmsi, v)
        if (store.current.size > 80000) {
          const keep = new Map<number, Vessel>()
          Array.from(store.current.entries())
            .slice(-60000)
            .forEach(([k, v]) => keep.set(k, v))
          store.current = keep
        }
        if (!flushTimer) flushTimer = window.setTimeout(flush, 700)
      }
      ws.onclose = () => {
        if (disposed) return
        setStatus('closed')
        retry = window.setTimeout(open, 3000)
      }
      ws.onerror = () => ws?.close()
    }

    open()
    return () => {
      disposed = true
      if (retry) window.clearTimeout(retry)
      if (flushTimer) window.clearTimeout(flushTimer)
      ws?.close()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key])

  return { vessels, status, lastPush }
}
