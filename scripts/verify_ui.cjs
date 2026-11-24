// UI 交互验证：AI 面板提问 + 搜索造船 + 事件面板
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

  // 1) AI 面板
  await page.getByRole('button', { name: 'AI 助手' }).click()
  await page.waitForTimeout(400)
  const ta = page.locator('.agent-input textarea')
  await ta.fill('过去6小时有哪些疑似捕捞')
  await ta.press('Enter')
  await page.waitForTimeout(3500)
  await page.screenshot({ path: `${OUT}/ui_agent.png` })

  // 2) 搜索一艘真实渔船
  const mmsi = process.argv[3]
  if (mmsi) {
    await page.locator('.search-box input').fill(mmsi)
    await page.waitForTimeout(1200)
    const res = page.locator('.search-results button').first()
    if (await res.count()) await res.click()
    await page.waitForTimeout(4200)
    await page.screenshot({ path: `${OUT}/ui_vessel.png` })
  }
  if (errs.length) console.log('ERRORS:/n' + errs.slice(0, 10).join('\n'))
  else console.log('no console errors')
  await browser.close()
})()
