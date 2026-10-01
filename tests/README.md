# Tests

The repo uses Vitest for unit/integration coverage and Playwright for browser E2E coverage.

## Vitest

```bash
npm test
npm run test:watch
npm run test:coverage
```

Coverage currently includes utilities, timing, settings serialization, Speed Racer settings, theme behavior, lesson plugin behavior, and selected DOM helpers. Vitest runs in `jsdom` by default; integration tests can opt into Node.

Vitest is not the first-pass target for full Web Audio playback, browser speech synthesis, or full Knockout rendering.

## Playwright

Playwright serves the built app from `dist/`, so build first:

```bash
npm run build
npm run test:e2e
```

First-time browser install:

```bash
npx playwright install chromium
```

Specs live in `e2e/` and cover app load, lesson pickers, settings layout, playback behavior, dark mode, mobile settings width, and accessibility.

Run the focused accessibility checks:

```bash
npx playwright test e2e/accessibility.spec.ts
```

The accessibility spec uses `@axe-core/playwright` and also asserts screen-reader-facing names/descriptions for keyboard shortcuts, Speed Racer, Voice, Tone, Input, Output, Flagged cards, Noise, RSS, and the separately named playback and offline live regions.

Offline tests exercise a real build in a nested deployment directory and cache every lazy import before closing and reopening with networking disabled. They also cover storage refusal, partial/corrupt downloads, missing-cache repair, multi-window updates and narrow accessible controls. Optional WebKit coverage uses an origin socket cutoff because Playwright's WebKit offline toggle rejects cached navigation; it does not substitute for testing on an iPhone.

```bash
npx playwright install webkit
TEST_WEBKIT=1 npx playwright test e2e/offline.spec.ts --project=webkit-offline
```

See [the offline guide](../docs/OFFLINE_IPHONE.md) for the device checklist and limitations.

## CI

`develop2.yml` runs on `develop` pushes and PRs with Node 20:

```bash
npm ci
npm run test
npm run build --if-present
npx playwright install --with-deps chromium
npm run test:e2e
```

Fork hosting is Cloudflare Workers. Some workflow files still contain legacy GitHub Pages deploy steps; those are not the current fork preview/deploy path.
