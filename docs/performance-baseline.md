# Legacy UI performance baseline

The baseline is the exact `outputs/0.4.0-refined/CodexQuotaGuard-0.4.0-Source.zip` release, not the current working-tree Tk implementation. `work/performance/prepare.py` extracts only Python sources and icon/image assets into an isolated directory after validating every resolved destination. Source and fixture SHA256 hashes are recorded in `fixture-provenance.json`.

The frozen fixture reads `local.sqlite`'s `last_summary` with SQLite `mode=ro`, then reads matching account/cycle grouped Token events with the current read-only analytics function. Settings are explicitly allowlisted. No authentication decryption, credentials, conversation body, raw journal payload, network connection, or Engine startup is involved. The source settings file and production database files are never written. Other fixture display status is deliberately identified as a frozen performance test, and connection/receipts are empty rather than fabricated.

Run `work/venv/Scripts/python.exe work/performance/legacy_probe.py N` for repetitions 1 through 3 sequentially. Each starts a fresh Python process, verifies imports resolve inside the release extraction, and creates a demo App with a separate empty local database. The 750 × 570 window is disabled, marked NOACTIVATE/TOOLWINDOW, and positioned outside the desktop at coordinates greater than 12000. No physical mouse/keyboard input is dispatched and no existing application is closed.

The pie uses the last hour, and the trend uses one day/all models/all users. The legacy trend raster is 414 × 128 pixels. Once first layout and initial animations settle, twenty unchanged snapshot renders are timed. For line and bar hover, 360 normalized left-to-right pointer positions over two traversals are scheduled at an absolute 120 Hz cadence for approximately three seconds. Only the chart's own motion handler is called. For drag, the App's own drag/move/end handlers receive a bounded sinusoidal trajectory; Windows changes only the isolated off-screen window. Actual delivered input intervals and redraw/move-completion intervals are recorded. Between scenarios, animation tails settle for one second.

`time.perf_counter` measures wall duration. `time.process_time` measures the entire Python process CPU; Windows process accounting is coarse for individual short callbacks, so whole-scenario CPU and per-draw wall time are the primary measurements. `GetProcessMemoryInfo` records working set and private bytes. CPU percentage is normalized to one core, not the entire machine. Idle is a two-second event loop observation after animations stop. First-layout timing begins before importing the GUI and ends after the first fixture render and layout; it is warm-filesystem **source process** startup, not frozen EXE startup.

Results are in `legacy-run-1.json` through `legacy-run-3.json`; `summarize_legacy.py` produces median metrics in `legacy-summary.json`. `legacy-initial-calibration.json` was an instrument calibration run and is excluded: its memory-query signature was corrected before the three retained runs, and a longer settling pause prevents hover-tail work from contaminating drag measurements.

These are callback/raster-completion measurements, **not screen presentation FPS**. Off-screen DWM behavior may differ from an actively visible window, and native WebView2 dragging does not map one-to-one to the old Python move queue. A WebView comparison must use the identical fixture, window size and input trajectory, include host plus all child-process CPU/memory, disclose actual chart dimensions, and distinguish browser rAF intervals from actual displayed frames. Real on-screen user acceptance remains a separate check.

## WebView2 comparison

`webview_host.py` loads the actual local frontend build and actual WebView2 host, but supplies only the same frozen fixture through a demo controller. It creates its own isolated data directory and private WebView profile. The window is positioned at `(12000,12000)` and marked NOACTIVATE/TOOLWINDOW. A separate geometry verification in `web-runtime-9/ready.json` confirms the actual native position is `(12000,12000)`, with outer and client sizes both 750 × 570. The retained three benchmark runs independently assert the browser viewport is exactly 750 × 570, devicePixelRatio 1, on the same 3440 × 1440 primary-screen environment. The new SVG outer dimensions are 402.56 × 127 and its actual plot area is 359.50 × 101.60; the legacy 414 × 128 canvas includes its axes.

`webview_probe.py` uses CDP on the isolated host's own loopback debug port. It dispatches 360 `PointerEvent` objects directly to the chart's own hit target with the same normalized two-traversal trajectory and absolute 120 Hz schedule. Events do not move the actual pointer. JavaScript microtasks are allowed to finish before recording handler/DOM-patch time. Separate rAF timestamps and SVG mutation timestamps are collected. These measure scheduling and DOM work, not compositor presentation. An rAF sampling loop itself adds some instrumentation work. The new native OS drag path is deliberately **not compared** because it checks actual mouse-button state, which was not altered.

Native Toolhelp process enumeration and `GetProcessTimes`/`GetProcessMemoryInfo` aggregate the isolated Python launcher, Python GUI host and all six descendant WebView2 processes (eight processes total). Before/after process sets remained stable in all retained scenarios. CPU is user plus kernel time for the full tree. Working-set sums include shared pages more than once; private bytes are a better measure of additional memory. The legacy records cover its GUI Python process and omit its lightweight Python launcher; this small scope difference is disclosed rather than silently treated as exact process-set equality.

The initial WebView instrumentation run coincided with the frontend's final rebuild and is excluded as `web-initial-calibration.json`. The subsequent `web-run-1.json` through `web-run-3.json` are three sequential runs of the same verified frontend asset hashes with no overlapping agent build/test workload. Each fresh profile was allowed to settle before a two-second idle observation and three-second line/bar observations. `summarize_webview.py` produces medians, ranges, and explicitly nullable screen-FPS/native-drag comparison fields.

The retained results show a tradeoff, not a universal speedup: the new browser runs a much denser animation clock but uses more active CPU and private memory in this controlled test. The CPU comparison includes the cost of that higher animation cadence. Neither rAF counts nor old raster-completion counts prove the number of frames actually displayed. Source first-layout timing is not frozen EXE launch timing and the WebView runs each start with a fresh isolated profile.

## Measured medians (2026-09-10)

| Metric | Previous Tk trial | WebView2 trial | Change |
|---|---:|---:|---:|
| Line hover, full process CPU over 3 s | 1375 ms | 3250 ms | +136.4% |
| Bar hover, full process CPU over 3 s | 1281.25 ms | 2546.88 ms | +98.8% |
| Idle private memory | 90.9 MiB | 291.4 MiB | +220.5% |
| Idle CPU over 2 s | 15.625 ms | 15.625 ms | No measured difference |
| Source first-layout time | 2935.6 ms | 5104.5 ms | +73.9%; fresh WebView profiles |

The new line/bar rAF median interval is 6.9 ms and its p95 is about 7.2 ms. The old line raster-completion p95 is 27.63 ms, but these are different observation points and **must not be divided to advertise an FPS optimization percentage**. Actual presented FPS and native drag improvement remain unmeasured. Three-second scenario CPU can exceed 3000 ms because multiple process threads work in parallel; it is not whole-machine CPU utilization.

The offline Vue UI probe separately confirms that twenty identical snapshots produce zero SVG mutations and preserve the same dropdown and selected device. Native host probes confirm startup, minimize, taskbar restore, hide and tray restore opacity transitions; real on-screen drag feel still requires user acceptance. These benefits do not imply lower resource use.
