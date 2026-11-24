/** 底图提供方。
 *
 * 只收录**不需要 API Key**就能直连的方案，避免上线时被额度/水印卡住：
 *  - deep      自托管 Natural Earth 矢量（本项目自带 GeoJSON，零外部依赖，最快）
 *  - satellite Esri World Imagery 栅格（免 Key，做法合规但需保留来源标注）
 *  - vector    OpenFreeMap 开源矢量（免 Key、无配额声明，含地名标注）
 *
 * 若要换成 MapTiler / Mapbox / CARTO 等需要 Key 的服务，
 * 只要在 styleFor 里加一个分支并在 BASEMAPS 里登记条目即可，无需改其它代码。
 */
export type BaseKey = 'deep' | 'satellite' | 'ocean'

export const BASEMAPS: Array<{ key: BaseKey; label: string; hint: string }> = [
  { key: 'deep', label: '深海', hint: '自托管矢量底图 · 零外部依赖 · 全球概览最快' },
  { key: 'satellite', label: '卫星', hint: 'Esri World Imagery · 免 Key · 放大看岸线与港口' },
  { key: 'ocean', label: '海图', hint: 'Esri Ocean 海洋底图 · 免 Key · 含水深与海域标注' },
]

const EMPTY = { type: 'FeatureCollection' as const, features: [] as any[] }

const GRATICULE_SOURCE = { type: 'geojson', data: EMPTY } as any

/** 深海模式：自托管陆地多边形 */
const DEEP = {
  version: 8,
  glyphs: 'https://demotiles.maplibre.org/font/{fontstack}/{range}.pbf',
  sources: {
    land: { type: 'geojson', data: '/geo/land-50m.geojson' },
    // 高精度海岸线：仅在放大后按需加载，避免全球视图拉 5MB
    landHi: { type: 'geojson', data: '/geo/land-10m.geojson' },
    graticule: GRATICULE_SOURCE,
  },
  layers: [
    { id: 'ocean', type: 'background', paint: { 'background-color': '#061019' } },
    {
      id: 'land',
      type: 'fill',
      source: 'land',
      maxzoom: 5,
      paint: { 'fill-color': '#14212f', 'fill-opacity': 1, 'fill-antialias': false },
    },
    {
      id: 'land-outline',
      type: 'line',
      source: 'land',
      maxzoom: 5,
      paint: {
        'line-color': '#2a5573',
        'line-width': ['interpolate', ['linear'], ['zoom'], 1, 0.25, 5, 0.7, 10, 1.1],
        'line-opacity': ['interpolate', ['linear'], ['zoom'], 1, 0.5, 6, 0.9],
      },
    },
    {
      id: 'land-hi',
      type: 'fill',
      source: 'landHi',
      minzoom: 5,
      paint: { 'fill-color': '#14212f', 'fill-opacity': 1, 'fill-antialias': false },
    },
    {
      id: 'land-hi-outline',
      type: 'line',
      source: 'landHi',
      minzoom: 5,
      paint: {
        'line-color': '#2a5573',
        'line-width': ['interpolate', ['linear'], ['zoom'], 5, 0.8, 10, 1.2, 13, 1.6],
        'line-opacity': 0.9,
      },
    },
    {
      id: 'graticule',
      type: 'line',
      source: 'graticule',
      paint: { 'line-color': '#4b9fe1', 'line-width': 0.5, 'line-opacity': 0.09 },
    },
  ],
}

/** 卫星模式：Esri 影像压暗，让船位与热力更跳 */
const SATELLITE = {
  version: 8,
  glyphs: 'https://demotiles.maplibre.org/font/{fontstack}/{range}.pbf',
  sources: {
    sat: {
      type: 'raster',
      tiles: [
        'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
      ],
      tileSize: 256,
      maxzoom: 13,
      attribution: 'Imagery © Esri, Maxar, Earthstar Geographics',
    },
    graticule: GRATICULE_SOURCE,
  },
  layers: [
    { id: 'ocean', type: 'background', paint: { 'background-color': '#060d16' } },
    {
      id: 'sat',
      type: 'raster',
      source: 'sat',
      paint: {
        'raster-opacity': 0.9,
        'raster-saturation': -0.45,
        'raster-contrast': 0.06,
        'raster-brightness-max': 0.78,
      },
    },
    {
      id: 'graticule',
      type: 'line',
      source: 'graticule',
      paint: { 'line-color': '#8ecbff', 'line-width': 0.5, 'line-opacity': 0.1 },
    },
  ],
}

/** 海图模式：Esri Ocean 底图 + 注记层（水深、海域名、岸线），渔业场景最贴合 */
const OCEAN = {
  version: 8,
  glyphs: 'https://demotiles.maplibre.org/font/{fontstack}/{range}.pbf',
  sources: {
    oceanBase: {
      type: 'raster',
      tiles: [
        'https://server.arcgisonline.com/ArcGIS/rest/services/Ocean/World_Ocean_Base/MapServer/tile/{z}/{y}/{x}',
      ],
      tileSize: 256,
      maxzoom: 13,
      attribution: 'Ocean basemap © Esri, GEBCO, NOAA, National Geographic',
    },
    oceanRef: {
      type: 'raster',
      tiles: [
        'https://server.arcgisonline.com/ArcGIS/rest/services/Ocean/World_Ocean_Reference/MapServer/tile/{z}/{y}/{x}',
      ],
      tileSize: 256,
      maxzoom: 13,
    },
    graticule: GRATICULE_SOURCE,
  },
  layers: [
    { id: 'ocean', type: 'background', paint: { 'background-color': '#061019' } },
    {
      id: 'ocean-base',
      type: 'raster',
      source: 'oceanBase',
      paint: { 'raster-opacity': 1, 'raster-brightness-max': 0.7, 'raster-contrast': 0.08, 'raster-saturation': -0.35 },
    },
    { id: 'ocean-ref', type: 'raster', source: 'oceanRef', paint: { 'raster-opacity': 0.72 } },
    // 统一压暗到深色主题，保留水深与注记细节
    { id: 'ocean-dim', type: 'background', paint: { 'background-color': '#04121e', 'background-opacity': 0.46 } },
    {
      id: 'graticule',
      type: 'line',
      source: 'graticule',
      paint: { 'line-color': '#8ecbff', 'line-width': 0.5, 'line-opacity': 0.08 },
    },
  ],
}

export function styleFor(key: BaseKey): any {
  if (key === 'satellite') return SATELLITE
  if (key === 'ocean') return OCEAN
  return DEEP
}

export function graticule(step = 20) {
  const features: any[] = []
  for (let lon = -180; lon <= 180; lon += step) {
    features.push({
      type: 'Feature',
      geometry: {
        type: 'LineString',
        coordinates: Array.from({ length: 37 }, (_, i) => [lon, -90 + i * 5]),
      },
    })
  }
  for (let lat = -80; lat <= 80; lat += step) {
    features.push({
      type: 'Feature',
      geometry: {
        type: 'LineString',
        coordinates: Array.from({ length: 73 }, (_, i) => [-180 + i * 5, lat]),
      },
    })
  }
  return { type: 'FeatureCollection' as const, features }
}
