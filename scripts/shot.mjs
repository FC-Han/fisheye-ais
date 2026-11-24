// 开发用截图脚本：复用本机 Chrome，避免下载 playwright 自带 chromium
// 用法: npx --yes -p playwright node scripts/shot.mjs <url> <out.png> [waitMs]
import { chromium } from 'playwright'

const url = process.argv[2] || 'http://127.0.0.1:8000/'
const out = process.argv[3] || 'logs/shot.png'
const wait = Number(process.argv[4] || 12000)

const candidates = [
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
]

const browser = await chromium.launch({
  executablePath: candidates.find((p) => p),
  headless: true,
  args: ['--enable-unsafe-swiftshader', '--use-angle=swiftshader', '--disable-gpu-sandbox'],
})
const page = await browser.newPage({ viewport: { width: 1680, height: 950 }, deviceScaleFactor: 1 })
const errors = []
page.on('console', (m) => {
  if (m.type() === 'error') errors.push(m.text())
})
page.on('pageerror', (e) => errors.push('PAGEERROR: ' + e.message))
await page.goto(url, { waitUntil: 'domcontentloaded' })
await page.waitForTimeout(wait)
await page.screenshot({ path: out })
console.log('saved', out)
if (errors.length) console.log('CONSOLE ERRORS:\n' + errors.slice(0, 12).join('\n'))
await browser.close()
