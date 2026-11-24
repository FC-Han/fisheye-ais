// 开发用截图脚本：复用本机 Chrome（playwright-core + executablePath）
// 运行: NODE_PATH=C:/Users/34953/pwrunner/node_modules node scripts/shot.cjs <url> <out.png> [waitMs]
const { chromium } = require('playwright-core')

;(async () => {
  const url = process.argv[2] || 'http://127.0.0.1:8000/'
  const out = process.argv[3] || 'logs/shot.png'
  const wait = Number(process.argv[4] || 12000)
  const exe = [
    'C:/Program Files/Google/Chrome/Application/chrome.exe',
    'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
  ].find(Boolean)
  const browser = await chromium.launch({
    executablePath: exe,
    headless: true,
    args: ['--enable-unsafe-swiftshader', '--use-angle=swiftshader'],
  })
  const page = await browser.newPage({ viewport: { width: 1680, height: 950 } })
  const errors = []
  page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()) })
  page.on('pageerror', (e) => errors.push('PAGEERROR: ' + e.message))
  await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 60000 })
  await page.waitForTimeout(wait)
  await page.screenshot({ path: out })
  console.log('saved', out)
  if (errors.length) console.log('CONSOLE ERRORS:/n' + errors.slice(0, 15).join('\n'))
  await browser.close()
})()
