import { expect, test as base, Page, BrowserContext } from '@playwright/test'
import AxeBuilder from '@axe-core/playwright'
import { createServer } from 'node:http'
import { readFileSync, readdirSync } from 'node:fs'
import { join } from 'node:path'
import { createHash } from 'node:crypto'

const dist = join(process.cwd(), 'dist')
const sw = readFileSync(join(dist, 'service-worker.js'), 'utf8')
const build = JSON.parse(sw.match(/const BUILD = (\{.*\})\n/)![1]) as {
  version: string, assets: Array<{ url: string, hash: string }>
}
const lazyAsset = build.assets.find(asset => asset.url.startsWith('src_wordfiles_POL_'))!.url
const prefix = '/morsebrowser/dev/'
type Host = { url: string, failed: string | null, corrupt: string | null, version: string, requests: string[], offline: boolean }

// Real HTTP faults are necessary: Playwright routes do not intercept worker fetches.
const test = base.extend<{ host: Host }>({
  host: async ({}, use) => {
    const host: Host = { url: '', failed: null, corrupt: null, version: build.version, requests: [], offline: false }
    const server = createServer((req, res) => {
      if (host.offline) { req.socket.destroy(); return }
      const pathname = decodeURIComponent(new URL(req.url!, 'http://localhost').pathname)
      if (pathname === '/probe') { res.writeHead(200, { 'Content-Type': 'text/html' }).end('<!doctype html><title>Scope probe</title>'); return }
      if (!pathname.startsWith(prefix)) { res.writeHead(404).end(); return }
      const name = pathname.slice(prefix.length) || 'index.html'
      host.requests.push(name)
      if (name.includes('..') || name === host.failed) { res.writeHead(503).end('unavailable'); return }
      try {
        let data = readFileSync(join(dist, name))
        if (name === 'service-worker.js') data = Buffer.from(sw.replace(`"version":"${build.version}"`, `"version":"${host.version}"`))
        if (name === host.corrupt) data = Buffer.from('incorrect deployment bytes')
        const ext = name.split('.').pop()!
        const type = { js: 'text/javascript', html: 'text/html', css: 'text/css', webmanifest: 'application/manifest+json', svg: 'image/svg+xml', png: 'image/png', jpg: 'image/jpeg' }[ext]
        res.writeHead(200, { 'Content-Type': type || 'application/octet-stream', 'Cache-Control': 'no-store' }).end(data)
      } catch (_) { res.writeHead(404).end() }
    })
    await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve))
    host.url = `http://127.0.0.1:${(server.address() as any).port}${prefix}`
    await use(host)
    await new Promise<void>(resolve => server.close(() => resolve()))
  }
})

// WebKit's offline emulation rejects even literal service-worker responses
// (Playwright #42775). Cut off the real origin socket (responses use no-store)
// and emulate the connectivity signal for UI/speech policies instead.
async function network (context: BrowserContext, host: Host, offline: boolean, browserName: string) {
  host.offline = offline
  if (browserName !== 'webkit') {
    await context.setOffline(offline)
  } else {
    await context.addInitScript(value => {
      Object.defineProperty(navigator, 'onLine', { configurable: true, get: () => !value })
    }, offline)
    for (const page of context.pages()) await page.evaluate(value => {
      Object.defineProperty(navigator, 'onLine', { configurable: true, get: () => !value })
      window.dispatchEvent(new Event(value ? 'offline' : 'online'))
    }, offline)
  }
}

async function ready (page: Page) {
  await expect(page.locator('#offline-status')).toHaveText('Ready for offline use', { timeout: 45000 })
}
async function controls (page: Page) {
  await page.locator('.offline-installation > summary').click()
}
async function cacheNames (page: Page) {
  return page.evaluate(async () => (await caches.keys()).filter(key => key.startsWith('morse-offline:')))
}

// Verify the generator covers all emitted practice assets, not just entrypoints.
test('generated list matches the build and all file hashes', async () => {
  const files = readdirSync(dist).filter(name => !['download', 'service-worker.js'].includes(name) && !/\.(map|zip)$/.test(name)).sort()
  expect(build.assets.map(asset => asset.url).sort()).toEqual(files)
  for (const asset of build.assets) {
    expect(createHash('sha256').update(readFileSync(join(dist, asset.url))).digest('hex')).toBe(asset.hash)
  }
  expect(files.some(name => name.startsWith('src_wordfiles_'))).toBe(true)
  expect(files.some(name => name.startsWith('src_presets_configs_'))).toBe(true)
  expect(files.some(name => /analytics|gtag/i.test(name))).toBe(false)
})

test('install, close, reopen offline, load unused lesson/preset, play and keep settings', async ({ page, context, host, browserName }) => {
  const errors: string[] = []
  context.on('page', next => next.on('pageerror', error => errors.push(error.message)))
  await context.addInitScript(() => {
    // Track actual oscillator starts and running audio contexts, not just UI state.
    const contexts: AudioContext[] = []
    ;(window as any).__practiceAudio = { contexts, starts: 0 }
    const Native = window.AudioContext
    window.AudioContext = class extends Native {
      constructor (...args: any[]) { super(...args); contexts.push(this) }
      createOscillator () {
        const node = super.createOscillator()
        const start = node.start.bind(node)
        node.start = (...args) => { (window as any).__practiceAudio.starts++; start(...args) }
        return node
      }
    }
    // Exercise storage refusal without making readiness depend on it.
    if (navigator.storage) {
      navigator.storage.persist = async () => false
      navigator.storage.persisted = async () => false
    }
  })
  await page.goto(host.url)
  await ready(page)
  await controls(page)
  await expect(page.locator('#offline-storage')).toContainText('not granted')
  const manifestURL = await page.locator('link[rel=manifest]').getAttribute('href')
  const manifest = await (await page.request.get(new URL(manifestURL!, host.url).href)).json()
  expect(new URL(manifest.scope, new URL(manifestURL!, host.url)).href).toBe(host.url)
  expect(manifest.display).toBe('standalone')
  await expect(page.locator('link[rel=apple-touch-icon]')).toHaveAttribute('href', /\.png$/)
  // Do not select any lesson/preset before closing the online page.
  await page.getByRole('button', { name: 'Dark mode' }).click()
  await page.locator('#wpm').fill('31')
  await page.locator('#wpm').blur()
  await expect.poll(async () => (await context.cookies()).find(cookie => cookie.name === 'wpm')?.value).toBe('31')
  expect((await context.cookies()).find(cookie => cookie.name === 'wpm')!.expires).toBeGreaterThan(Date.now() / 1000 + 86400)
  await page.close()
  await network(context, host, true, browserName)
  const offline = await context.newPage()
  await offline.goto(host.url + 'index.html?offline-test=1')
  await ready(offline)
  await expect(offline.locator('html')).toHaveAttribute('data-theme', 'dark')
  await expect(offline.locator('#wpm')).toHaveValue('31')
  await expect.poll(() => offline.evaluate(() => navigator.serviceWorker.controller?.scriptURL)).toBe(host.url + 'service-worker.js')
  const served: string[] = []
  offline.on('response', response => { if (response.fromServiceWorker()) served.push(response.url()) })
  await offline.locator('#lessonsPickerClassToggle').click()
  await offline.getByLabel('Class').getByRole('option', { name: 'OVERLEARN' }).click()
  await offline.locator('#lessonsPickerContentToggle').click()
  await offline.getByLabel('Content').getByRole('option', { name: 'SENDING' }).click()
  await offline.locator('#lessonsPickerLessonToggle').click()
  const lesson = offline.getByLabel('Lesson').getByRole('option').first()
  const lessonName = (await lesson.textContent())!.trim()
  await lesson.click()
  await expect(offline.locator('#lessonsPickerLessonToggle')).toContainText(lessonName)
  await offline.locator('#lessonsPickerPresetsToggle').click()
  const preset = offline.getByRole('listbox', { name: 'Settings preset' }).getByRole('option').nth(1)
  const presetName = (await preset.textContent())!.trim()
  await preset.click()
  await expect(offline.locator('#lessonsPickerPresetsToggle')).toContainText(presetName)
  expect(served.some(url => url.includes('src_wordfiles_'))).toBe(true)
  expect(served.some(url => url.includes('src_presets_configs_'))).toBe(true)
  await offline.locator('#btnPlayButton').click()
  await expect(offline.getByRole('status', { name: 'Latest status announcement' })).toContainText(/Playing/i)
  await expect.poll(() => offline.evaluate(() => {
    const audio = (window as any).__practiceAudio
    return audio.starts > 0 && audio.contexts.some((ctx: AudioContext) => ctx.state === 'running')
  })).toBe(true)
  await offline.locator('#btnStop').click()
  await offline.goto(host.url + '?rssEnabled=true&selectedClass=BC1&selectedGroup=REA&selectedLesson=REA')
  await expect(offline.locator('#lessonsPickerLessonToggle')).toContainText('REA')
  await ready(offline)
  await offline.locator('#btnRssAccordionButton').click()
  await expect(offline.getByRole('button', { name: 'Poll RSS', exact: true })).toBeDisabled()
  await expect(offline.getByRole('status').filter({ hasText: 'RSS downloads need' })).toBeVisible()
  expect(await offline.locator('script[src*=googletagmanager]').count()).toBe(0)
  expect(errors).toEqual([])
})

for (const fault of ['failed', 'corrupt'] as const) {
  test(`incomplete ${fault} download never reports ready and retries successfully`, async ({ page, host }) => {
    host[fault] = lazyAsset
    await page.goto(host.url)
    await controls(page)
    await expect(page.locator('#offline-detail')).toContainText('incomplete', { timeout: 30000 })
    await expect(page.locator('#offline-status')).not.toHaveText('Ready for offline use')
    await expect.poll(() => cacheNames(page)).toEqual([])
    host[fault] = null
    await page.locator('#offline-download').click()
    await ready(page)
  })
}

test('missing cached chunk revokes readiness and can be repaired', async ({ page, context, host, browserName }) => {
  await page.goto(host.url)
  await ready(page)
  await page.reload()
  await page.evaluate(async asset => {
    const key = (await caches.keys()).find(key => key.startsWith('morse-offline:'))!
    await (await caches.open(key)).delete(new URL(asset, location.href).href)
  }, lazyAsset)
  await network(context, host, true, browserName)
  await expect(page.locator('#offline-status')).toHaveText('Offline download not ready')
  await controls(page)
  await page.locator('#offline-download').click()
  await expect(page.locator('#offline-status')).not.toHaveText('Ready for offline use')
  await network(context, host, false, browserName)
  await page.locator('#offline-download').click()
  await ready(page)
})

test('failed update keeps old offline build; successful update waits for every open window', async ({ page, context, host, browserName }) => {
  await page.goto(host.url)
  await ready(page)
  await page.reload()
  const oldCaches = await cacheNames(page)
  const second = await context.newPage()
  await second.goto(host.url)
  await ready(second)
  await page.locator('#btnPlayButton').click()
  await controls(page)
  host.version = 'failed-update'
  host.failed = lazyAsset
  await page.locator('#offline-download').click()
  await expect(page.locator('#offline-detail')).toContainText('incomplete', { timeout: 30000 })
  await ready(page)
  expect(await cacheNames(page)).toEqual(oldCaches)
  await network(context, host, true, browserName)
  await second.reload()
  await ready(second)
  await network(context, host, false, browserName)
  host.failed = null
  // Simulate a browser termination during a different candidate's install.
  await page.evaluate(async scope => {
    const partial = await caches.open(`morse-offline:${scope}:abandoned-install`)
    await partial.put(new URL('partial', scope).href, new Response('partial'))
  }, host.url)
  host.version = 'successful-update'
  await page.locator('#offline-download').click()
  await expect(page.locator('#offline-detail')).toContainText('Update downloaded', { timeout: 45000 })
  await expect(page.getByRole('status', { name: 'Latest status announcement' })).toContainText(/Playing/i)
  await expect.poll(() => page.evaluate(async () => (await navigator.serviceWorker.getRegistration())?.waiting?.state)).toBe('installed')
  await page.close()
  // Even after the playing window closes, another client pins the old build.
  await expect.poll(() => second.evaluate(async () => (await navigator.serviceWorker.getRegistration())?.waiting?.state)).toBe('installed')
  await second.close()
  const probe = await context.newPage()
  await probe.goto(new URL('/probe', host.url).href)
  await expect.poll(() => probe.evaluate(async scope => {
    const reg = await navigator.serviceWorker.getRegistration(scope)
    return reg?.active?.state === 'activated' && !reg?.waiting
  }, host.url)).toBe(true)
  await probe.close()
  await network(context, host, true, browserName)
  const reopened = await context.newPage()
  await reopened.goto(host.url)
  await ready(reopened)
  const version = await reopened.evaluate(async () => {
    const reg = await navigator.serviceWorker.getRegistration()
    return new Promise(resolve => {
      const channel = new MessageChannel()
      channel.port1.onmessage = event => resolve(event.data.version)
      reg!.active!.postMessage({ type: 'STATUS' }, [channel.port2])
    })
  })
  expect(version).toBe('successful-update')
  expect(await cacheNames(reopened)).toEqual([...oldCaches, `morse-offline:${host.url}:successful-update`])
})


test('offline controls are accessible and fit narrow viewports', async ({ page, host, browserName }) => {
  await page.goto(host.url)
  await ready(page)
  await controls(page)
  for (const width of [375, 390]) {
    await page.setViewportSize({ width, height: 844 })
    const box = await page.locator('.offline-installation').boundingBox()
    expect(box!.x).toBeGreaterThanOrEqual(0)
    expect(box!.x + box!.width).toBeLessThanOrEqual(width)
    expect(await page.locator('.offline-installation').evaluate(el => el.scrollWidth <= el.clientWidth)).toBe(true)
  }
  const result = await new AxeBuilder({ page }).include('.offline-installation').analyze()
  expect(result.violations).toEqual([])
  await page.locator('.offline-installation > summary').focus()
  await page.keyboard.press('Enter')
  await expect(page.locator('#offline-download')).not.toBeVisible()
  await page.keyboard.press('Enter')
  if (browserName === 'webkit') {
    // Mobile WebKit's tab traversal follows platform keyboard settings; verify
    // the focusable control's native keyboard activation directly here.
    await page.locator('#offline-download').focus()
  } else {
    await page.keyboard.press('Tab')
  }
  await expect(page.locator('#offline-download')).toBeFocused()
  await page.keyboard.press('Enter')
  await expect(page.locator('#offline-detail')).toHaveText('Offline download is current.')
})
