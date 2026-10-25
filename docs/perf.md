# Portal performance

Numbers are from the sample portal on localhost with headless Chromium (cold cache, new browser context per page), so they show
bytes and work, not your network. Transfer is the response body plus headers for the portal's own requests; Google Fonts (about
73 KB, 2 requests) are listed apart. Caddy already encodes responses with zstd and gzip in production, so app level compression
was left out; the gzip column is what Caddy sends.

Reproduce:

    python tools/sample_portal.py --port 8765
    python tools/perf_measure.py --shots screenshots-perf --tag run

## Before and after

| Measure | Before | After |
|---|---|---|
| Home page (NOC, Map) own requests | 10 | 8 |
| Home page own transfer, desktop | 1,783,593 B | 383,481 B |
| Home page own transfer, 390 px | 1,648,661 B | 248,601 B |
| Home page decoded bytes | 1,793,507 B | 393,977 B |
| Home page JS fetched | noc.js, three.module.js (1.3 MB), OrbitControls, 3D scene inline in the HTML | noc.js |
| 3D view opened (total transfer) | 1,783,593 B | 1,784,284 B, the 3D parts load on the first press of 2 |
| Dashboard HTML | 180,219 B | 117,695 B (the 3D scene moved to static/map3d.js, 1,141 lines) |
| `/floorplan` own transfer, 6 requests | 277,514 B | unchanged |
| `/incidents` own transfer, 2 requests | 235,735 B | unchanged |
| Home DOM nodes (Map / NOC) | 1,146 / 981 | 1,145 / 980 |
| `/api/snapshot` body | 20,226 B (5,776 B gzip) | 20,308 B (5,761 B gzip), 0 B when unchanged (304) |
| `/api/snapshot` headers | no ETag, no Cache-Control | `ETag`, `Cache-Control: private, no-cache` |
| `/api/snapshot` median server time (sample data) | 6.1 ms | 6.1 ms |
| Snapshot poll every 30 s | full body each time | 304 with no body when nothing changed |

First contentful paint on localhost is 80 to 130 ms in every run, so it did not move; the win is bytes (about 1.4 MB less on every
home load, which matters most on a cold iPhone connection) and the work of parsing Three.js and building the scene only when 3D is opened.
The 3D scene itself takes about 3.5 s to build and animate in headless software rendering, unchanged.

## What was done

1. The 3D module moved out of `dashboard.html` into `static/map3d.js`. `setView("3d")` imports it once; the module shows itself if 3D is
   still the open view when it finishes loading. `window.HLT` is still the one export contract (the exports test now reads `map3d.js`).
2. `/api/snapshot` sends an ETag and answers `If-None-Match` with 304; the page's poll uses `cache: "no-cache"` so the browser revalidates.

## Looked at and left alone

- Three.js was already served from `static/vendor`; static files already carry cache headers (a day for images and vendor, ETag revalidation for the portal's own JS and CSS).
- `noc.js` (28 KB): NOC is the default view, so deferring it would not move a number.
- Station and login photos are already resized (station-1920.jpg is 218 KB). webp at quality 80 would save about 65 KB on the largest; skipped because it touches every page's background rule for little gain.
- Google Fonts: render path already uses `display=swap` and preconnect. Self hosting would touch about nine templates; see the questions in the report.
- Database indexes: no slow query found (snapshot response is 6 ms), so none added.
