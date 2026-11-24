import type {
  AgentReply,
  Behavior,
  BehaviorTimeline,
  EvidenceCard,
  FingerprintResult,
  FishEvent,
  HotspotsResponse,
  ObservationQuality,
  OceanSeries,
  StreamStatus,
  Summary,
  TrackResponse,
  Vessel,
  Zone,
} from './types'

const BASE = ''

async function get<T>(path: string, params?: Record<string, any>): Promise<T> {
  const qs = params
    ? '?' +
      Object.entries(params)
        .filter(([, v]) => v !== undefined && v !== null && v !== '')
        .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`)
        .join('&')
    : ''
  const r = await fetch(BASE + path + qs)
  if (!r.ok) throw new Error(`${path} ${r.status}`)
  return r.json()
}

export const api = {
  summary: () => get<Summary>('/api/stats/summary'),
  stream: () => get<StreamStatus>('/api/stream/status'),
  vessels: (bbox: [number, number, number, number], identity = 'all', limit = 4000) =>
    get<{ count: number; vessels: Vessel[] }>('/api/vessels', {
      bbox: bbox.map((n) => n.toFixed(4)).join(','),
      identity,
      limit,
    }),
  vessel: (mmsi: number) => get<Vessel & Record<string, any>>(`/api/vessels/${mmsi}`),
  track: (mmsi: number, hours: number) =>
    get<TrackResponse>(`/api/vessels/${mmsi}/track`, { hours, max_points: 4000 }),
  events: (params: { kinds?: string; hours?: number; limit?: number; mmsi?: number }) =>
    get<{ count: number; events: FishEvent[] }>('/api/events', params),
  zones: () => get<{ zones: Zone[] }>('/api/zones'),
  hotspots: (params: { hours?: number; cell?: number; limit?: number; bbox?: string }) =>
    get<HotspotsResponse>('/api/hotspots', params),
  behavior: (mmsi: number, hours: number) => get<Behavior>(`/api/vessels/${mmsi}/behavior`, { hours }),
  timeline: (mmsi: number, hours: number) =>
    get<BehaviorTimeline>(`/api/vessels/${mmsi}/timeline`, { hours }),
  quality: (mmsi: number, hours: number) =>
    get<ObservationQuality>(`/api/vessels/${mmsi}/quality`, { hours }),
  fingerprint: (mmsi: number, days = 30) =>
    get<FingerprintResult>(`/api/vessels/${mmsi}/fingerprint`, { days }),
  regions: () => get<{ regions: Array<{ key: string; name: string; bbox: number[]; note?: string }> }>('/api/regions'),
  search: (q: string) => get<{ results: any[] }>('/api/search', { q }),
  oceanSeries: (mmsi: number, hours: number) =>
    get<OceanSeries>('/api/analysis/ocean/series', { mmsi, hours }),
  oceanSubmit: async (mmsi: number, hours: number, products = 'physics,waves,bgc_clim') => {
    const r = await fetch(
      `/api/analysis/ocean/tracks?mmsi=${mmsi}&hours=${hours}&products=${encodeURIComponent(products)}`,
      { method: 'POST' },
    )
    if (!r.ok) throw new Error(`ocean submit ${r.status}`)
    return (await r.json()) as { job_id: string; status: string; deduped: boolean; products: string[] }
  },
  job: (id: string) =>
    get<{ job_id: string; status: string; progress?: number; error?: string }>(
      `/api/analysis/jobs/${id}`,
      { with_result: false },
    ),
  evidence: (mmsi: number, hours: number, kind: 'gap' | 'fishing' = 'gap', limit = 4) =>
    get<{ count: number; cards: EvidenceCard[] }>(`/api/vessels/${mmsi}/evidence`, {
      kind,
      hours,
      limit,
    }),
  chat: async (message: string, history: Array<{ role: string; content: string }>): Promise<AgentReply> => {
    const r = await fetch('/api/agent/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message, history }),
    })
    if (!r.ok) throw new Error(`agent ${r.status}`)
    return r.json()
  },
}

export function wsUrl(bbox: [number, number, number, number], identity: string) {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  const host = location.host
  return `${proto}://${host}/ws/vessels?bbox=${bbox.map((n) => n.toFixed(3)).join(',')}&interval=2&identity=${identity}`
}
