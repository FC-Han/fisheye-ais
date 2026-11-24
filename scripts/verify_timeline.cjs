// v0.2 行为时间线验收：热点 → 代表船 → 船舶面板 → 时间线/质量/指纹区块
const { chromium } = require('playwright-core')
const OUT = process.argv[2] || 'logs'

;(async () => {
  const browser = await chromium.launch({
    executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe',
    headless: true,
    args: ['--enable-unsafe-swiftshader', '--use-angle=swiftshader'],
  })
  const page = await browser.newPage({ viewport: { width: 1680, height: 950 } })
  const errs = []
  page.on('pageerror', (e) => errs.push('PAGEERROR ' + e.message))
  page.on('console', (m) => { if (m.type() === 'error') errs.push('CONSOLE ' + m.text()) })

  await page.goto('http://127.0.0.1:8000/', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(12000)

  // 打开一条有轨迹的船：先用接口挑一个轨迹点足够多的 MMSI
  const mmsi = await page.evaluate(async () => {
    const r = await fetch('/api/events?kinds=fishing&hours=24&limit=5')
    const j = await r.json()
    return j.events && j.events.length ? j.events[0].mmsi : null
  })

  let picked = null
  if (mmsi) {
    // 通过搜索框进入
    const box = page.locator('input[placeholder*="搜索"], input[placeholder*="船名"], input[placeholder*="MMSI"]').first()
    if (await box.count()) {
      await box.fill(String(mmsi))
      await page.waitForTimeout(1200)
      const item = page.locator('.search-item, .search-result, .result-item').first()
      if (await item.count()) {
        await item.click()
        picked = mmsi
        await page.waitForTimeout(7000)
      }
    }
  }

  // 兜底：点热点里的代表船
  if (!picked) {
    const chip = page.locator('.hot-vessels button').first()
    if (await chip.count()) {
      await chip.click()
      await page.waitForTimeout(7000)
    }
  }

  // 确保在「船舶」标签
  const tab = page.getByRole('button', { name: /^船舶/ })
  if (await tab.count()) {
    await tab.first().click()
    await page.waitForTimeout(3500)
  }

  await page.screenshot({ path: `${OUT}/timeline_panel.png` })
  // 单独截右面板，便于看细节
  const panel = page.locator('.right-panel').first()
  if (await panel.count()) await panel.screenshot({ path: `${OUT}/timeline_panel_only.png` })

  // 结构化检查
  const probe = await page.evaluate(() => {
    const t = (s) => document.querySelector(s)?.textContent?.trim() || null
    const gantt = document.querySelectorAll('.gantt-seg').length
    const segs = document.querySelectorAll('.seg-list .seg').length
    return {
      hasTimelineTitle: !!Array.from(document.querySelectorAll('.section-title'))
        .find((e) => e.textContent.includes('行为时间线')),
      ganttBands: gantt,
      segRows: segs,
      qualBadge: t('.qual-badge'),
      coverageLine: t('.gantt-axis span:nth-child(2)'),
      advice: t('.behavior-advice'),
      dna: t('.dna-empty-title') || t('.dna-head'),
      dnaBody: t('.dna-empty-body'),
      gapBands: document.querySelectorAll('.gantt-seg.gap').length,
      caveat: !!document.querySelector('.hot-caveat'),
    }
  })

  // 点击一个片段看是否能定位地图（不报错即可）
  const seg = page.locator('.seg-list .seg:not(.is-gap)').first()
  if (await seg.count()) {
    await seg.click()
    await page.waitForTimeout(2200)
    await page.screenshot({ path: `${OUT}/timeline_locate.png` })
  }

  console.log(JSON.stringify({ mmsi: picked, probe, errors: errs }, null, 2))
  await browser.close()
})()
