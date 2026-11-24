export type Identity = 'fishing' | 'candidate' | 'non_fishing' | 'unknown'

export interface Vessel {
  mmsi: number
  ts: number
  lat: number
  lon: number
  sog: number | null
  cog: number | null
  heading: number | null
  nav_status: number | null
  identity: Identity
  ship_name: string | null
  ship_type: number | null
  ship_type_text: string | null
  call_sign?: string | null
  length?: number | null
  destination?: string | null
  imo?: string | null
}

export interface FishEvent {
  kind: 'fishing' | 'gap' | 'geofence'
  ts: number
  start_ts?: number
  end_ts?: number | null
  mmsi: number
  ship_name: string | null
  lat: number | null
  lon: number | null
  title: string
  status: string
  confidence: number
  detail: string
  zone_name?: string
  evidence?: Record<string, any>
}

export interface TrackPoint {
  ts: number
  lat: number
  lon: number
  sog: number | null
  cog: number | null
}

export interface TrackResponse {
  mmsi: number
  hours: number
  track: TrackPoint[]
  fishing_events: Array<{
    start_ts: number
    end_ts: number
    duration_s: number
    avg_sog: number
    lat: number
    lon: number
    confidence: number
    evidence: string
  }>
  gap_events: Array<{
    gap_start: number
    gap_end: number | null
    duration_s: number | null
    event_status: string
    coverage_quality: string
    lat: number | null
    lon: number | null
  }>
}

export interface StreamStatus {
  now: number
  connected: boolean
  last_message_ts: number | null
  seconds_since_message: number | null
  rate_1m: number
  msgs_total: number
  subscription: number[][][]
  region: string | null
  collector_alive: boolean
  db_size_mb: number
  history: Array<{ ts: number; rate_1m: number; connected: number }>
  llm_enabled: boolean
  archive_enabled: boolean
  analytics_last_run: number | null
}

export interface Summary {
  vessels_live_30m: number
  vessels_24h: number
  fishing_live: number
  fishing_events_24h: number
  gap_open_candidate: number
  gap_events_24h: number
  geofence_events_24h: number
  positions_total: number
}

export interface Zone {
  id: string
  name: string
  kind: string | null
  source: string | null
  polygon: number[][]
}

export interface AgentReply {
  answer: string
  tools: Array<{ name: string; args: Record<string, any>; ok: boolean; map?: any }>
  map: {
    focus_mmsi?: number
    focus_lat?: number
    focus_lon?: number
    mmsis?: number[]
    bbox?: number[]
    highlight?: string
    hotspots?: Hotspot[]
  }
  mode?: string
  model?: string
  note?: string
}

/* ---------- 渔船热点 ---------- */
export interface Hotspot {
  lat: number
  lon: number
  cell: number
  score: number
  area: string
  n_events: number
  n_vessels: number
  vessel_hours: number
  activity_points: number
  low_speed_vessels: number
  gap_candidates: number
  avg_confidence: number | null
  basis: 'event' | 'low_speed' | 'mixed'
  top_vessels: Array<{ mmsi: number; name: string | null; sog: number | null; identity: string }>
}

export interface HotspotsResponse {
  hours: number
  cell_deg: number
  generated_at: number
  hotspot_count: number
  totals: {
    cells_with_activity: number
    suspected_fishing_events: number
    vessels_in_events: number
    live_low_speed_fishing: number
  }
  hotspots: Hotspot[]
  caveat: string
}

/* ---------- 单船运动状态 ---------- */
export interface BehaviorSegment {
  state: string
  label: string
  start_ts: number
  end_ts: number
  duration_min: number
  distance_nm: number
  avg_sog: number | null
  lat: number
  lon: number
  fishing: boolean
  confidence: number | null
}

export interface Behavior {
  mmsi: number
  found: boolean
  message?: string
  name?: string | null
  identity?: string
  ship_type_text?: string | null
  hours: number
  points?: number
  span_hours?: number
  freshness_min?: number
  current?: {
    state: string
    label: string
    sog: number | null
    since_ts: number | null
    lat: number
    lon: number
  }
  stats?: {
    distance_nm: number
    displacement_nm: number
    straightness: number | null
    avg_sog: number | null
    max_sog: number | null
    radius_nm: number
    anchored_h: number
    slow_h: number
    transit_h: number
    fast_h: number
    longest_gap_min: number
  }
  trend?: {
    verdict: string
    last3h_avg_sog: number | null
    prev3h_avg_sog: number | null
    delta_sog: number | null
  }
  segments?: BehaviorSegment[]
  hourly?: Array<{ hour_ts: number; avg_sog: number | null; max_sog: number | null; points: number; state: string }>
  fishing_events?: any[]
  gap_events?: any[]
  narrative?: string
  caveat?: string
}

export const STATE_COLORS: Record<string, string> = {
  anchored: '#5d7899',
  slow: '#34d399',
  transit: '#4b9fe1',
  fast: '#a78bfa',
  unknown: '#3b4a5e',
}

// ---- v0.2 行为时间线 ----
export interface TimelineSegment {
  seg_id: string
  activity: 'transit' | 'low_speed' | 'stationary' | 'observation_gap' | 'unknown'
  label: string
  sub_activity: string | null
  sub_label: string | null
  start_ts: number
  end_ts: number
  duration_s: number
  duration_min: number
  lat: number | null
  lon: number | null
  confidence: number | null
  gear_type: string | null
  reliability: { grade: 'good' | 'fair' | 'poor' | 'none'; flags: string[] }
  evidence: Record<string, any>
}

export interface TimelineSummary {
  window_hours: number
  segments: number
  observed_hours: number
  gap_hours: number
  coverage_ratio: number
  by_activity: Record<string, number>
  fishing_candidate_count: number
  fishing_candidate_hours: number
  distance_nm: number
  poor_reliability_segments: number
  gap_threshold_s: number
  median_interval_s: number
}

export interface BehaviorTimeline {
  mmsi: number
  found: boolean
  name?: string | null
  identity?: string
  hours: number
  points: number
  model_version: string
  summary: TimelineSummary | null
  segments: TimelineSegment[]
  narrative?: string
  caveat?: string
}

export interface ObservationQuality {
  mmsi: number
  found: boolean
  grade: 'good' | 'fair' | 'poor' | 'no_data'
  flags: string[]
  coverage_ratio: number
  observed_hours?: number
  gap_hours?: number
  gap_count?: number
  longest_gap_h?: number
  median_report_interval_s?: number | null
  points?: number
  upstream?: { verdict?: string; ok_ratio?: number | null; max_down_sec?: number | null }
  advice?: string
  message?: string
  caveat?: string
}

export interface FingerprintResult {
  mmsi: number
  status: 'ok' | 'insufficient_history'
  name?: string | null
  n_trips_used?: number
  n_trips_closed?: number
  n_trips_required?: number
  message?: string
  why?: Record<string, any>
  baseline?: Record<string, { median: number | null; mad: number | null; p10: number | null; p90: number | null; n: number }>
  caveat?: string
}

export const TIMELINE_COLORS: Record<string, string> = {
  transit: '#4b9fe1',
  low_speed: '#35c4a8',
  stationary: '#5d7899',
  observation_gap: '#7a2f36',
  unknown: '#3b4a5e',
}

export const ACTIVITY_LABELS: Record<string, string> = {
  transit: '航行',
  low_speed: '低速移动',
  stationary: '静止/近静止',
  observation_gap: '观测中断',
  unknown: '未知',
}

export const RELIABILITY_COLORS: Record<string, string> = {
  good: '#34d399',
  fair: '#f2b035',
  poor: '#f0645f',
  none: '#4a5a6e',
}

export const IDENTITY_COLORS: Record<string, string> = {
  fishing: '#34d399',
  candidate: '#f2b035',
  non_fishing: '#5d7899',
  unknown: '#8ea2bb',
}

export const IDENTITY_LABELS: Record<string, string> = {
  fishing: '渔船',
  candidate: '疑似渔船',
  non_fishing: '非渔船',
  unknown: '身份未知',
}

// ---- v0.4 环境引擎与证据卡 ----

export interface Stat {
  min: number
  median: number
  max: number
  n: number
}

export interface SeaState {
  code: string
  label: string
  wave_height_m?: number
  scale: string
}

export interface OceanPoint {
  ts: number
  lat: number | null
  lon: number | null
  sst_c: number | null
  u_m_s: number | null
  v_m_s: number | null
  current_speed_m_s: number | null
  ssh_m: number | null
  wave_height_m: number | null
  wave_dir_deg: number | null
  wave_period_s: number | null
  sea_state: SeaState
  wind_speed_m_s: number | null
  wind_dir_deg: number | null
  wind_dir_text: string | null
  wind_u_m_s: number | null
  wind_v_m_s: number | null
  wind_beaufort: { force: number | null; label: string; scale: string }
  wind_basis: string | null
  chl_mg_m3: number | null
  chl_basis: string | null
  chl_ref: string | null
  source_time: string | null
  lag_hours: number | null
  quality: string | null
  products: string[]
  quality_by_product: Record<
    string,
    { quality?: string; basis?: string; source_time?: string | null; lag_hours?: number | null }
  >
}

export interface OceanSummary {
  points_sampled: number
  points_matched: number
  sst_c: Stat | null
  current_speed_m_s: Stat | null
  ssh_m: Stat | null
  wave_height_m: Stat | null
  wave_period_s: Stat | null
  sea_state: {
    dominant: string | null
    dominant_label: string | null
    distribution: Record<string, number>
    rule: string
  }
  wind_speed_m_s: Stat | null
  wind_beaufort: {
    dominant_force: number | null
    dominant_label: string | null
    distribution: Record<string, number>
    rule: string
  }
  wind_direction: {
    mean_deg: number | null
    mean_text: string | null
    convention: string
    note: string | null
  }
  wind_basis: string[] | null
  chl_mg_m3: Stat | null
  chl_basis: string | null
  chl_ref_years: string | null
  lag_hours: { median: number | null; max: number | null }
  quality_counts: Record<string, number>
}

export interface OceanSeries {
  status: 'ok' | 'no_cache' | 'no_track' | 'cached_but_unusable' | 'unavailable' | 'error'
  available?: boolean
  mmsi?: number
  hours?: number
  message?: string
  reason?: string
  suggest?: string
  summary?: OceanSummary
  series?: OceanPoint[]
  caveat?: string
}

export interface EvidenceItem {
  claim: string
  value: string
  detail: string
  kind: 'observed' | 'inferred' | 'missing'
}

export interface EvidenceCard {
  card_id: string
  event_type: string
  mmsi: number
  ship_name?: string | null
  headline: string
  start_ts: number
  end_ts: number
  duration_h: number
  status: string
  review_status: string
  attribution: string
  attribution_reason: string
  evidence: EvidenceItem[]
  alternative_explanations: string[]
  unexplained: string[]
  data_quality?: Record<string, number | null>
  caveat?: string
}

export const ENV_COLORS = {
  sst: '#f2b035',
  current: '#4b9fe1',
  wave: '#35c4a8',
  wind: '#c9a0ff',
  chl: '#34d399',
}

// 风场来源的中文简称。来源必须显示出来：L4 是卫星观测、GFS/ERA5 是模式，
// 数值可以差 1–2 m/s，混在一起看会得出错误结论。
export const WIND_BASIS_LABELS: Record<string, string> = {
  observation_l4: '卫星观测',
  analysis_forecast: 'GFS 模式',
  reanalysis: 'ERA5 再分析',
}

export const EVIDENCE_KIND_LABELS: Record<string, string> = {
  observed: '观测事实',
  inferred: '统计推断',
  missing: '无法得知',
}
