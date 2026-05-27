# Event detail modal — design

**Date:** 2026-05-27
**Status:** Approved, pending implementation

## Goal

Clicking any row in the dashboard's "Recent events" table opens a modal showing the full
details of that event, including a large version of the snapshot image.

## Scope

Front-end only. Touches `templates/dashboard.html` (modal markup + JS) and
`static/style.css` (modal styling). No changes to `db.py`, `dashboard.py`, routes, or the
database — every field shown is already returned by `GET /api/events`.

Event records already carry: `id, ts, verdict, person, distance, threshold,
antispoof_score, snapshot_path`. The table currently surfaces only the snapshot thumbnail,
time, verdict, who, and distance; the modal surfaces the rest.

## Data flow

`refreshEvents()` already fetches the current page's rows (`page`, up to
`EVENTS_PAGE_SIZE`). Store that array in a module-scoped variable so the click handler can
look rows up by index:

- Each rendered `<tr>` gets a `data-idx="<index into page>"` attribute.
- A single delegated click handler on `#events-body` (or per-row handler) reads
  `data-idx`, looks up the row object, and populates + shows the modal.
- The modal renders from the row data already in memory — **no new fetch**.
- The handler reads the fields it needs at click time; the 3s polling refresh rebuilds the
  table body but does not touch the open modal, so an open modal is never disrupted.

## Modal contents (full details + large snapshot)

- **Snapshot:** `<img src="/snapshots/<snapshot_path>">` at a constrained large size. When
  `snapshot_path` is null/empty, render a "no snapshot" placeholder instead of a broken
  image element.
- **Verdict:** badge reusing existing `.badge` styles (`ok` for granted, `spoof` for
  spoof, `bad` otherwise).
- **Who:** `person`, or `—` when null.
- **Time:** full `new Date(ts*1000).toLocaleString()` plus the relative form from the
  existing `relTime(ts)` helper (e.g. "5 min ago").
- **Distance / threshold:** `distance.toFixed(3)` and `threshold.toFixed(3)` shown as
  `0.312 / 0.593`; each part shows `—` when its value is null.
- **Anti-spoof score:** rendered only when `antispoof_score != null` (spoof events),
  formatted to 2 decimals.
- **Event ID:** `id`.

All user-controlled / string values (notably `person`) are escaped with the existing
`esc()` helper before insertion, consistent with the rest of the dashboard JS.

## Open / close behavior

- **Open:** click anywhere on an events row.
- **Close:** (a) the `×` button in the modal header, (b) clicking the backdrop outside the
  dialog, (c) pressing `Esc`. Clicks inside the dialog do not close it (stop propagation /
  check `event.target`).
- The empty-state row ("No events yet.") is not clickable (it has no `data-idx`).

## Styling

- `.modal-backdrop`: fixed full-viewport dim overlay (e.g. `rgba(0,0,0,.6)`), centered
  content, hidden by default (toggled via a class or `style.display`), high `z-index`
  (above the sticky topbar's `z-index: 5`).
- `.modal`: card matching the dark theme — `background: var(--panel)`,
  `border: 1px solid var(--border)`, rounded corners, padding, a sensible `max-width` and
  `max-height` with scroll for overflow.
- Modal snapshot image: `max-width: 100%` and a capped height (e.g. `max-height: 50vh`),
  `object-fit: contain` — explicitly constrained so it can never blow out (the same class
  of bug as the missing `.user-avatar` sizing).
- Detail fields laid out as label/value pairs consistent with the existing card styles.

## Testing

Front-end only — verification is manual:
1. Click a **granted** row → modal shows person, distance/threshold, snapshot, ID.
2. Click a **denied** row → person shows `—`, distance present or `—`.
3. Click a **spoof** row → anti-spoof score line appears; distance/threshold may be `—`.
4. Click a row whose event has **no snapshot** → placeholder shown, no broken image.
5. Close via `×`, via backdrop click, and via `Esc` — all work.
6. While the modal is open, confirm the 3s auto-refresh does not close or corrupt it.

## Out of scope

- No new stored fields or endpoints (full details come from existing data).
- No changes to pagination, filtering, the live stream, or the chart.
