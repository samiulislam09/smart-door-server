# Dashboard multi-page navigation — design

**Date:** 2026-05-27
**Status:** Approved, pending implementation

## Goal

Split the single-page dashboard into two pages — **Overview** and **Users** — and add a top
navigation menu to move between them. The Users management UI moves onto its own page.

## Background

The dashboard is a Flask blueprint (`dashboard.py`) rendering one template
(`templates/dashboard.html`) at `GET /dashboard` (endpoint `dashboard.home`). That single
page contains: the topbar, a door-status hero, a KPI card strip, the **Users** card
(`<section class="users-card">`), a live-camera + chart row, and the recent-events table
with its detail modal. User-management forms POST to `/users`, `/users/<id>/photos`,
`/users/<id>/delete`, `/photos/<id>/delete` and currently redirect back to
`dashboard.home`. `login.html` is a separate standalone template.

## Architecture

Introduce a shared Jinja **base template** so the topbar/nav/toast are defined once; both
pages extend it. This avoids duplicating chrome and keeps each page template focused on its
own content.

### Files

- **New `templates/base.html`** — the shared shell:
  - `<head>` loading `style.css`, plus a `{% block head %}{% endblock %}` for
    page-specific head content.
  - `<header class="topbar">` containing: the brand, a **nav menu** with links to Overview
    and Users, the online status indicator, and the Logout link.
  - The existing toast: `{% if request.args.get('msg') %}<div class="toast">…</div>{% endif %}`.
  - `{% block content %}{% endblock %}` for the page body and
    `{% block scripts %}{% endblock %}` before `</body>` for page JS.

- **`templates/dashboard.html` (rewritten as the Overview page)** — `{% extends "base.html" %}`:
  - `{% block content %}`: the hero, KPI strip, live-camera + chart row, and the
    recent-events table **plus its `#event-modal`** — i.e. everything currently on the page
    **except** the Users card.
  - `{% block head %}`: `<script src="{{ url_for('static', filename='chart.min.js') }}"></script>`
    (loaded only here, so the Users page doesn't fetch ~205 KB it doesn't use).
  - `{% block scripts %}`: the existing dashboard JS (stats, events, chart, modal),
    unchanged in behavior.

- **New `templates/users.html` (the Users page)** — `{% extends "base.html" %}`:
  - `{% block content %}`: the entire existing `<section class="card users-card">…</section>`
    block moved here verbatim (user list, per-user photos, add-photo, delete, add-user form).
  - No extra JS needed (the add-user filename display is an inline `onchange`).

- **`login.html`** — unchanged, stays standalone (different layout, no nav).

### `dashboard.py` changes

- Add route `GET /dashboard/users` → `users_page()` (login-required), rendering
  `users.html` with `users=db.list_users()`.
- `home()` drops the `users=db.list_users()` argument — the Overview no longer lists users,
  removing one DB query per Overview load.
- Change the redirect target in `users_create`, `users_add_photo`, `users_delete`, and
  `photo_delete` from `dashboard.home` to `dashboard.users_page` (preserving the existing
  `msg=` query parameter), so the user stays on the Users page after an action and the toast
  appears there.
- The user-management POST routes keep their existing paths and methods; only the redirect
  target changes.

## Navigation behavior

- The nav is a horizontal set of links in the topbar, placed between the brand and the
  status/Logout group: **Overview** (→ `dashboard.home`) and **Users** (→
  `dashboard.users_page`).
- The link for the current page gets an `active` class. The base template determines this by
  comparing `request.endpoint` to `'dashboard.home'` / `'dashboard.users_page'`.

## Styling (`static/style.css`)

Add nav rules themed with the existing CSS variables:
- `.nav` — flex row of links with a gap, vertically centered in the topbar.
- `.nav a` — muted color, no underline, small padding/radius.
- `.nav a:hover` — subtle `var(--panel-2)` background.
- `.nav a.active` — full `var(--text)` color (and/or accent underline) to mark the current
  page.

No changes to existing rules; nav rules are appended.

## Testing

Manual (no automated UI tests in this project):
1. `/dashboard` (Overview) renders hero, KPIs, live camera, chart, recent events, and the
   working detail modal — and no longer shows the Users card.
2. `/dashboard/users` renders the Users card with the enrolled users and forms.
3. The top nav appears on both pages; clicking switches pages; the current page's link is
   highlighted.
4. Adding a user, adding a photo, deleting a photo, and deleting a user each land back on
   the **Users** page with the correct toast message.
5. Logout still works from both pages; visiting either page while logged out redirects to
   login.
6. The Users page does not load `chart.min.js` (check network tab); the Overview does.

## Out of scope

- No changes to matching/door logic, the `/api/*` endpoints, the events modal behavior, or
  the events/stats data flow.
- Only the Users card relocates; Events and Live stay on the Overview.
