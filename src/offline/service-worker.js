/* eslint-env serviceworker */
/* global __OFFLINE_BUILD__, __OFFLINE_RECOVERY__ */
const BUILD = __OFFLINE_BUILD__
const RECOVERY = __OFFLINE_RECOVERY__
const PREFIX = `morse-offline:${self.registration.scope}:`
const CACHE = PREFIX + BUILD.version
const COMPLETE = new URL('__offline_complete__', self.registration.scope).href
const INDEX = new URL('index.html', self.registration.scope).href
const assets = BUILD.assets.map(asset => ({ ...asset, url: new URL(asset.url, self.registration.scope).href }))
const byURL = new Map(assets.map(asset => [asset.url, asset]))
let downloading = null

async function announce (message) {
  const clients = await self.clients.matchAll({ includeUncontrolled: true })
  clients.filter(client => client.url.startsWith(self.registration.scope))
    .forEach(client => client.postMessage({ ...message, version: BUILD.version }))
}

async function complete () {
  const cache = await caches.open(CACHE)
  if (!await cache.match(COMPLETE)) return false
  const keys = new Set((await cache.keys()).map(key => key.url))
  return assets.every(asset => keys.has(asset.url))
}

async function verifiedFetch (asset) {
  const response = await fetch(asset.url, { cache: 'no-store', credentials: 'same-origin' })
  if (!response.ok || response.type === 'opaque' || !response.url.startsWith(self.registration.scope)) throw new Error('Asset unavailable')
  const hash = await crypto.subtle.digest('SHA-256', await response.clone().arrayBuffer())
  const hex = Array.from(new Uint8Array(hash), byte => byte.toString(16).padStart(2, '0')).join('')
  if (hex !== asset.hash) throw new Error('Build changed during download')
  return response
}

async function download () {
  const cache = await caches.open(CACHE)
  await cache.delete(COMPLETE)
  let next = 0
  let count = 0
  let failed = false
  await announce({ type: 'PROGRESS', count, total: assets.length })
  // Bound requests/memory; wait for every writer before completing or failing.
  const results = await Promise.allSettled(Array.from({ length: 6 }, async () => {
    while (next < assets.length && !failed) {
      const asset = assets[next++]
      try {
        if (!await cache.match(asset.url)) await cache.put(asset.url, await verifiedFetch(asset))
        count++
        if (count % 25 === 0) await announce({ type: 'PROGRESS', count, total: assets.length })
      } catch (error) {
        failed = true
        throw error
      }
    }
  }))
  if (results.some(result => result.status === 'rejected')) throw new Error('Offline download incomplete')
  await cache.put(COMPLETE, new Response(BUILD.version))
  await announce({ type: 'COMPLETE' })
}

function repair () {
  if (!downloading) downloading = download().finally(() => { downloading = null })
  return downloading
}

self.addEventListener('install', event => {
  event.waitUntil(repair().catch(async error => {
    // A failed candidate must never replace or delete the working version.
    await caches.delete(CACHE)
    await announce({ type: 'FAILED' })
    throw error
  }))
  // Deliberately no skipWaiting: active practice and other tabs keep their build.
})

self.addEventListener('activate', event => {
  event.waitUntil((async () => {
    if (!await complete()) throw new Error('Offline cache incomplete')
    // Keep the previous working build as well; trim older successful builds.
    const versions = (await caches.keys()).filter(key => key.startsWith(PREFIX) && key !== CACHE)
    const successful = []
    for (const key of versions) {
      if (await (await caches.open(key)).match(COMPLETE)) successful.push(key)
    }
    // A terminated install can leave a partial cache. It must not displace the
    // previous complete build when choosing the one backup to retain.
    const previous = successful[successful.length - 1]
    await Promise.all(versions.filter(key => key !== previous).map(key => caches.delete(key)))
  })())
  // No clients.claim or forced reload: the next navigation uses this build.
})

self.addEventListener('message', event => {
  if (event.data?.type === 'STATUS') {
    event.waitUntil(complete().then(ready => event.ports[0]?.postMessage({ ready, version: BUILD.version })))
  } else if (event.data?.type === 'REPAIR') {
    event.waitUntil(repair().catch(() => announce({ type: 'FAILED' })))
  }
})

self.addEventListener('fetch', event => {
  const url = new URL(event.request.url)
  if (event.request.method !== 'GET' || url.origin !== self.location.origin) return
  // Only the app's document routes get a fallback. Other scopes/external links
  // must not be swallowed by the offline app.
  const appNavigation = event.request.mode === 'navigate' &&
    (url.pathname === new URL(self.registration.scope).pathname || url.pathname === new URL(INDEX).pathname)
  const target = appNavigation ? INDEX : url.href
  const asset = byURL.get(target)
  if (!asset) return
  event.respondWith((async () => {
    const cache = await caches.open(CACHE)
    const hit = await cache.match(target)
    if (hit) return hit
    try {
      // Recover evicted entries online only if they still match this build.
      const response = await verifiedFetch(asset)
      await cache.put(target, response.clone())
      return response
    } catch (_) {
      await announce({ type: 'FAILED' })
      // The app cannot render its retry controls without its document. Embed
      // recovery in the worker so it survives eviction of every cached asset.
      if (appNavigation) {
        return new Response(RECOVERY, {
          status: 503,
          headers: { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store' }
        })
      }
      return new Response('Offline download incomplete. Reconnect and use Retry offline download.', { status: 503 })
    }
  })())
})
