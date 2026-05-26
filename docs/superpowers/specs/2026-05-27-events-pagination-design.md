# Recent-events pagination — design

**Date:** 2026-05-27
**Status:** Approved, ready for implementation planning

## Problem

The dashboard's "Recent events" table fetches the newest 50 rows in one shot and renders
them all. There's no way to page back through older entries (the log retains up to
`MAX_EVENTS` = 1000 rows).

## Goal

Add **Prev / Next** pagination to the Recent-events table, **25 rows per page**,
server-side via an `offset`, with no `COUNT(*)` query.

## Non-goals

- Numbered page jumps (rejected: needs a total count + more UI for a capped log).
- Auto-refresh / live polling (out of scope; the dashboard stays load-on-demand).
- Any schema change.

## Approach

Server-side offset paging. The frontend requests **one extra row** (`limit = pageSize + 1
= 26`) at `offset = page × 25`, renders the first 25, and treats "a 26th row came back"
as "there is a next page." This avoids a separate count query and avoids a dead "Next"
button on an exact-multiple boundary.

## Backend

- `db.query_events(limit=50, verdict=None, offset=0)` — add an `offset` parameter; SQL
  becomes `... ORDER BY ts DESC LIMIT %s OFFSET %s` (offset appended after limit). Offset
  coerced to a non-negative int.
- `dashboard.api_events` (`/api/events`) — read `offset` from the query string
  (`int(request.args.get("offset", 0))`, clamped to ≥ 0); keep `limit` capped at 200; pass
  both to `db.query_events`. Row shape unchanged.

## Frontend (`templates/dashboard.html`)

- Constants/state: `const EVENTS_PAGE_SIZE = 25; let eventsPage = 0;`
- `refreshEvents()` fetches
  `/api/events?limit=${EVENTS_PAGE_SIZE + 1}&offset=${eventsPage * EVENTS_PAGE_SIZE}&verdict=${verdict}`,
  computes `hasNext = rows.length > EVENTS_PAGE_SIZE`, renders `rows.slice(0, EVENTS_PAGE_SIZE)`.
- Pager footer under the table: `‹ Prev`, a muted `Showing X–Y` label, `Next ›`.
  - `Prev` disabled when `eventsPage === 0`; `Next` disabled when `!hasNext`.
  - `X = eventsPage * 25 + 1`, `Y = X + renderedCount - 1` (label hidden / "No events" when 0 rows).
- Prev/Next handlers adjust `eventsPage` (never below 0; only increment when `hasNext`) and call `refreshEvents()`.
- Changing the verdict **filter resets `eventsPage = 0`** before refetching.
- Empty-state row (existing `colspan` message) unchanged.

## Styling (`static/style.css`)

A monochrome `.pager` row: fl-ex, space-between/aligned; buttons reuse the existing
ghost-button look (`#fff` bg, `--border`, hover `#f4f4f5`), disabled buttons at reduced
opacity + `cursor:default`; the `Showing X–Y` text in `--muted`.

## Testing

- `db.query_events(limit, offset)` returns the correct slice: insert N events, assert
  `query_events(limit=2, offset=0)` and `query_events(limit=2, offset=2)` return disjoint,
  correctly-ordered (newest-first) rows.
- `/api/events?limit=2&offset=2` honors offset (via the Flask test client).
- Dashboard still renders 200 and contains the pager hooks.
- Manual: Prev/Next paging through events; Next disables on the last page; Prev disabled
  on page 1; filter change returns to page 1.

## Risks / notes

- The peek (`limit+1`) means each page fetches 26 rows and renders 25 — negligible.
- `offset` paging over a live-updating log can shift rows if new events arrive between
  pages; acceptable for this low-frequency household log (and the dashboard doesn't
  auto-refresh, so the list is stable within a viewing session).
