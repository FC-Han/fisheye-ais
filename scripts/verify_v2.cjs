// v0.2 交互验收：热点 → 单船运动状态 → 追踪 → AI 对话 → 卫星/街图底图
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
  await page.waitForTimeout(14000)
  await page.screenshot({ path: `${OUT}/v2_main.png` })

  const hot = page.locator('.hot').first()
  if (await hot.count()) {
    await hot.click()
    await page.waitForTimeout(3000)
    await page.screenshot({ path: `${OUT}/v2_hot_focus.png` })
  }

  const chip = page.locator('.hot-vessels button').first()
  if (await chip.count()) {
    await chip.click()
    await page.waitForTimeout(6000)
    await page.screenshot({ path: `${OUT}/v2_vessel.png` })
  }

  const trackBtn = page.getByRole('button', { name: /追踪/ })
  if (await trackBtn.count()) {
    await trackBtn.first().click()
    await page.waitForTimeout(5000)
    await page.screenshot({ path: `${OUT}/v2_follow.png` })
  }

  // AI 对话：验证不抢走面板
  await page.getByRole('button', { name: 'AI 助手' }).click()
  await page.waitForTimeout(400)
  const ta = page.locator('.agent-input textarea')
  await ta.fill('现在哪里渔船作业最集中？给个排名')
  await ta.press('Enter')
  await page.waitForTimeout(14000)
  await page.screenshot({ path: `${OUT}/v2_agent.png` })
  const tabNow = await page.locator('.tabs button.active').innerText()

  // 追问：单船运动状态
  const chip2 = page.locator('.hot-vessels button').first()
  if (await chip2.count()) {
    await chip2.click()
    await page.waitForTimeout(5000)
  }
  await page.getByRole('button', { name: 'AI 助手' }).click()
  await page.waitForTimeout(300)
  await ta.fill('这艘船最近在干什么？')
  await ta.press('Enter')
  await page.waitForTimeout(14000)
  await page.screenshot({ path: `${OUT}/v2_agent2.png` })

  // 底图切换
  await page.getByRole('button', { name: '卫星' }).click()
  await page.waitForTimeout(8000)
  await page.screenshot({ path: `${OUT}/v2_sat.png` })
  await page.getByRole('button', { name: '海图' }).click()
  await page.waitForTimeout(9000)
  await page.screenshot({ path: `${OUT}/v2_ocean.png` })
  await page.getByRole('button', { name: '深海' }).click()
  await page.waitForTimeout(4000)

  console.log('active tab after asking:', tabNow)
  console.log(errs.length ? 'ERRORS:/n' + errs.slice(0, 10).join('\n') : 'no console errors')
  await browser.close()
})()
