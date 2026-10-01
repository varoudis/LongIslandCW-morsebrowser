# Offline Morse Browser on iPhone

This is a Home Screen web app installed from Safari. No paid Apple developer
account, signing, or App Store submission is needed.

## Build and host

```sh
npm ci
npm run build
```

Host the **entire `dist/` directory** at one HTTPS URL, for example
`https://example.org/morsebrowser/` or GitHub Pages. Keep its directory structure;
publish `index.html`, `service-worker.js`, `manifest.webmanifest` and every build
asset together. Do not use `file://` or the webpack development server for
installation. A localhost HTTP server is sufficient for desktop development.

The default Webpack `publicPath: auto` works at both `/` and nested paths such as
`/morsebrowser/dev/`. Manifest links, launch URL and worker scope use the app's
own directory. If your host needs an explicit asset prefix, use:

```sh
PUBLIC_PATH=/morsebrowser/ npm run build
```

The existing `GITHUB_PAGES=true` fork build still uses `/morsebrowser_dev/`.
Serve JavaScript with a JavaScript MIME type, the manifest with
`application/manifest+json`, and avoid HTML fallback responses for missing assets.
Prefer `Cache-Control: no-cache` for `service-worker.js` and `index.html`; do not
rewrite/inject the emitted files. Hash verification rejects altered files or a
partly published build. Publish atomically when your host supports it. The generated
asset list includes all lazy lesson/preset chunks and emitted styles, images and
fonts, without maintaining a manual list. Source maps and the downloadable ZIP
are not required for practice and are excluded.

## Install and confirm readiness

1. Open the hosted URL in Safari while online, outside Private Browsing.
2. Use **Share → Add to Home Screen**. Keep **Open as Web App** enabled if shown,
   then tap **Add**. The app uses the existing 300px club logo as its icon.
3. Open the icon from your Home Screen **while still online**. Keep the app open
   until the status near the top says **Ready for offline use**. Expand **Offline
   app** to see progress, storage information and the retry button. Safari and
   the installed app can have separate storage: readiness in Safari alone is
   insufficient. Set your preferred settings in the installed app.
4. If the download fails, reconnect, free some device space if needed and use
   **Retry offline download**. A failed update keeps the previous offline build.

Readiness requires a completion marker and the presence of every required cached
asset. Each newly downloaded asset must match its build's SHA-256 hash. The app
rechecks readiness on reopening, reconnecting and returning from the background;
missing cached files revoke readiness and can be repaired online. The browser can
still evict data after a successful check. Clearing website data removes both
settings and offline downloads. Existing year-long settings cookies remain in
use; persistent storage is requested where supported. Refusal or lack of support
does not stop practice, but storage retention is not guaranteed.

## Test on your iPhone

After readiness is confirmed in the Home Screen app:

1. Close the app. Enable airplane mode, and explicitly turn Wi-Fi off.
2. Reopen using the Home Screen icon. Confirm readiness and your saved settings.
3. Load a lesson **and settings preset you have never opened**, then press Play.
   Check audible tones, cards, Pause/Stop, another lesson, dark mode and your usual
   practice features. Try Voice First and recap with a locally installed voice.
4. Check the offline status/control with VoiceOver and enlarged text. Try portrait
   and landscape layouts. Reopen once more to check settings persistence.
5. Reconnect to test recovery and the update process below.

Morse tones are generated on-device by Web Audio. Offline speech depends on the
voices iOS exposes as local, their installed data, and the synthesis engine.
Remote-only voices are skipped offline; errors and a bounded timeout allow Morse
to continue when synthesis is unavailable. RSS polling stops offline and must be
started again online. External help/video/feed links and the ZIP download require
network access. Analytics is asynchronous, skipped when the browser reports
being offline, and never cached or required for startup. A browser's online flag
is only a hint; a failed connection still cannot block the main app.

**Background or locked-screen playback has not been verified.** Keep the app
visible for practice. Check your phone's audio output/volume and Safari audio
permissions if tones are silent; browser tests cannot confirm audible iPhone
output, iOS storage lifetime, local voice availability or Home Screen behavior.

## Updates

Open the app online and expand **Offline app → Check for updates / retry download**.
A complete new build waits until **all Morse Browser windows using that app
installation are closed**. Stop practice, close those windows, and reopen to apply
it. Reloading a window alone may keep the update waiting. There is no forced
reload, `skipWaiting`, or takeover during practice. The previous cache is retained
alongside the active build; older caches for that scope are trimmed on activation.
A failed candidate cannot activate, overwrite the old build, or remove its cache.
If an old build's missing asset is no longer hosted, its repair cannot complete;
a fully downloaded new build and closing/reopening the app restores readiness.
If the cached page itself is missing, a **Restore Morse Browser** page checks for
updates and provides **Retry opening app**. When it reports an update is ready,
close every app window, including the recovery page, then reopen. Saved settings
are kept; clearing website data is unnecessary.

## Automated checks

```sh
npm test
npm run build
npx playwright install chromium
npx playwright test
# Optional Safari-related engine coverage (desktop WebKit, emulated phone):
npx playwright install webkit
TEST_WEBKIT=1 npx playwright test e2e/offline.spec.ts --project=webkit-offline
```

`e2e/offline.spec.ts` serves a real build under `/morsebrowser/dev/`. It covers
online installation, page closure, offline reopening, unused lesson/preset imports,
Web Audio oscillator startup, cookie settings, query-string navigation, storage
refusal, partial and corrupt downloads, missing-cache repair, failed updates,
multiple-window update deferral, keyboard controls, axe and 375/390px layouts.
Chromium uses networking disabled by Playwright and the origin server. WebKit uses
an origin socket cutoff, disabled HTTP caching and an emulated offline signal,
because Playwright's WebKit offline toggle rejects even cached navigations
([upstream issue](https://github.com/microsoft/playwright/issues/42775)). These are
browser tests, **not tests on an actual iPhone**.

References: [Apple installation instructions](https://support.apple.com/guide/iphone/iphea86e5236/ios),
[WebKit storage policy](https://webkit.org/blog/14403/updates-to-storage-policy/),
[separate Home Screen storage](https://webkit.org/blog/14787/webkit-features-in-safari-17-2/),
[service-worker update lifecycle](https://developer.mozilla.org/en-US/docs/Web/API/Service_Worker_API/Using_Service_Workers).
