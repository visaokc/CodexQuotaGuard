# Offline desktop frontend · 0.4.1

Vue 3.5.42 renders the compact desktop interface in the host's WebView2. Python remains authoritative for accounts, Token journals, billing, quota protection and peer synchronization.

```powershell
cd frontend
npm ci --no-audit --no-fund
npm run build
npm test
node tests/ui.test.mjs
```

The host opens `frontend/dist/index.html`. Ship all four generated files in `dist/`: `index.html`, `app.js`, `style.css` and `THIRD_PARTY_LICENSES.txt`. Assets never require a CDN or external font service. Vue templates compile during the build; production uses the runtime-only Vue bundle, with no `eval` permission in the content security policy.

The `window.pywebview.api` contract is in `../docs/webview-contract.md`. Snapshot polling patches keyed Vue components. Chart animations only run when numeric values change. Dropdown state, form edits and selected device IDs remain local across polls. SVG supplies resolution-independent curves, rings and bars; opacity/transform transitions use the browser compositor. Native dragging and window transitions are delegated to the host.

## Isolated verification

`tests/ui.test.mjs` starts its own loopback server and headless Edge process through Playwright. It never attaches to the user's browser or production ledger. The test fixture is outside the production entrypoint and is not bundled. The UI test requires Playwright in the local Node resolution path and an installed Edge runtime.

The fixture bridge can only be selected explicitly with `?test=1` plus an injected `window.__CQG_TEST_BRIDGE__`. Under that explicit mode, `window.__CQG_TEST__.applySnapshot(snapshot)` and `getState()` support controlled layout and performance probes. Production startup passes no test query or fixture. Stable selectors include `data-testid="nav-overview"` (and the other page IDs), `pie-chart`, `trend-chart`, and `device-row`.

The cycle/all-model donut reads Token directly from the same active `summary.devices` rows as the table. Model-filtered cycle and rolling/historical views read raw Token events in `analytics`. Removed devices are excluded in every view. The table's estimated quota uses weighted allocation and the selected account or personal-cap denominator; graph values always remain Token.

Click a device to edit its note and preset color in the shared user-settings dialog, also available from Settings. Drag a row beyond the click threshold to change the locally saved, account-scoped device order; the list, user selector and donut legend share that order. Model choices sort by semantic numeric version (for example 5.10 before 5.9). `codex-auto-review` is hidden only in the model menu; its usage remains part of all-model totals. The trend tooltip holds its baseline unless the curve below it needs clearance, in which case it rises above the local peak.
