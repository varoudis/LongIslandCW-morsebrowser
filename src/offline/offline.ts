// The worker owns download completeness. Never infer readiness from navigator.onLine.
export function initOfflineApp (enabled = true): void {
  const status = document.getElementById('offline-status')
  const detail = document.getElementById('offline-detail')
  const storage = document.getElementById('offline-storage')
  const button = document.getElementById('offline-download') as HTMLButtonElement
  if (!status || !detail || !storage || !button) return
  if (!enabled) {
    status.textContent = 'Offline installation is available in the built app.'
    button.disabled = true
    return
  }
  if (!('serviceWorker' in navigator) || !window.isSecureContext) {
    status.textContent = 'Offline installation requires HTTPS and service worker support.'
    button.disabled = true
    return
  }
  const base = new URL('./', document.baseURI)
  let registration: ServiceWorkerRegistration
  let ready = false
  let notice = ''
  let refreshId = 0
  const render = () => {
    const label = ready ? 'Ready for offline use' : 'Offline download not ready'
    if (status.textContent !== label) status.textContent = label
    if (detail.textContent !== notice) detail.textContent = notice
    button.textContent = ready ? 'Check for updates / retry download' : 'Retry offline download'
  }
  const check = (worker: ServiceWorker): Promise<boolean> => new Promise(resolve => {
    const channel = new MessageChannel()
    const finish = (value: boolean) => {
      clearTimeout(timer)
      channel.port1.close()
      resolve(value)
    }
    const timer = setTimeout(() => finish(false), 5000)
    channel.port1.onmessage = event => finish(event.data.ready === true)
    try { worker.postMessage({ type: 'STATUS' }, [channel.port2]) } catch (_) { finish(false) }
  })
  const refresh = async () => {
    const id = ++refreshId
    const active = registration?.active
    const verified = active ? await check(active) : false
    if (id !== refreshId) return
    ready = verified
    if (registration?.waiting && await check(registration.waiting)) {
      notice = 'Update downloaded. Close all Morse Browser windows, then reopen to apply it.'
    }
    render()
  }
  const persistentStorage = async () => {
    try {
      if (!navigator.storage?.persist) {
        storage.textContent = 'This browser manages offline storage; downloads may be removed when space is low.'
      } else {
        const kept = await navigator.storage.persisted() || await navigator.storage.persist()
        storage.textContent = kept ? 'Persistent storage granted.' : 'Persistent storage was not granted; the browser may remove downloads when space is low.'
      }
    } catch (_) {
      storage.textContent = 'Persistent storage is unavailable; offline downloads may be removed by the browser.'
    }
  }
  const watched = new WeakSet<ServiceWorker>()
  const watch = (worker: ServiceWorker) => {
    if (watched.has(worker)) return
    watched.add(worker)
    button.disabled = true
    worker.addEventListener('statechange', () => {
      if (worker.state === 'installed' || worker.state === 'activated' || worker.state === 'redundant') {
        button.disabled = false
        if (worker.state === 'redundant') notice = 'Download incomplete. Reconnect and retry; any previous offline version is kept.'
        refresh()
      }
    })
  }
  const observe = (reg: ServiceWorkerRegistration) => {
    if (reg !== registration) {
      registration = reg
      reg.addEventListener('updatefound', () => { if (reg.installing) watch(reg.installing) })
    }
    if (reg.installing) watch(reg.installing)
  }
  navigator.serviceWorker.addEventListener('message', event => {
    // Do not accept status from another app installed on the same origin.
    if (![registration?.active, registration?.installing, registration?.waiting].includes(event.source as ServiceWorker)) return
    if (event.data.type === 'PROGRESS') {
      notice = `Downloading offline files: ${event.data.count} of ${event.data.total}. Keep this window open.`
      render()
    } else if (event.data.type === 'FAILED') {
      notice = 'Download incomplete. Reconnect and retry; any previous offline version is kept.'
      button.disabled = false
      refresh()
    } else if (event.data.type === 'COMPLETE') {
      notice = ''
      button.disabled = false
      refresh()
    }
  })
  const retry = async () => {
    button.disabled = true
    notice = navigator.onLine ? 'Checking offline download…' : 'Reconnect to download or update offline files.'
    render()
    await persistentStorage()
    try {
      // Failed first installs can remove their registration. Register afresh on
      // every retry rather than updating a now-detached registration object.
      observe(await navigator.serviceWorker.register(new URL('service-worker.js', base), { scope: base.href, updateViaCache: 'none' }))
      if (!ready && registration.active) registration.active.postMessage({ type: 'REPAIR' })
      if (!registration.installing) await registration.update()
      if (notice === 'Checking offline download…' && ready && !registration.installing && !registration.waiting) notice = 'Offline download is current.'
    } catch (_) {
      notice = 'Could not download updates. Reconnect and retry; any previous offline version is kept.'
    } finally {
      button.disabled = !!registration?.installing
      refresh()
    }
  }
  button.addEventListener('click', () => { retry() })
  navigator.serviceWorker.register(new URL('service-worker.js', base), { scope: base.href, updateViaCache: 'none' })
    .then(reg => {
      observe(reg)
      refresh()
      persistentStorage()
    }).catch(() => {
      notice = 'Offline download unavailable. Reconnect and retry on the built HTTPS site.'
      render()
    })
  // Detect missing/evicted files after returning to the app.
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') refresh()
  })
  window.addEventListener('online', () => { refresh() })
  window.addEventListener('offline', () => { refresh() })
}
