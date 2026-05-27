# Dashboard Multi-Page Navigation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (inline) to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **Commits:** The user owns commits. Do NOT commit automatically; leave changes in the working tree.

**Goal:** Split the single-page dashboard into an Overview page and a Users page, with a top nav menu to switch between them.

**Architecture:** Add a shared Jinja `base.html` (topbar + nav + toast + blocks). `dashboard.html` (Overview) and a new `users.html` extend it. Add a `users_page` route; retarget user-CRUD redirects to it; drop the users query from the Overview.

**Tech Stack:** Flask blueprint (`dashboard.py`), Jinja templates, CSS. No build step, no automated UI tests (verification is manual).

**Reference spec:** `docs/superpowers/specs/2026-05-27-dashboard-multipage-nav-design.md`

---

## File structure

- **New** `templates/base.html` — shared shell: head + topbar (brand, nav, status, logout) + toast + `{% block head %}`, `{% block content %}`, `{% block scripts %}`.
- **New** `templates/users.html` — extends base; the Users card.
- **Rewrite** `templates/dashboard.html` — extends base; Overview content + JS; loads `chart.min.js` in its head block.
- **Modify** `dashboard.py` — add `users_page` route; `home()` drops `users=`; retarget 4 redirects.
- **Modify** `static/style.css` — append `.nav` rules.

---

### Task 1: Create `templates/base.html`

- [ ] **Step 1: Create the file** with this exact content:

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Smart Door · Dashboard</title>
  <link rel="stylesheet" href="{{ url_for('static', filename='style.css') }}">
  {% block head %}{% endblock %}
</head>
<body>
  <header class="topbar">
    <div class="brand">▦ <span>Smart Door</span></div>
    <nav class="nav">
      <a href="{{ url_for('dashboard.home') }}"
         class="{{ 'active' if request.endpoint == 'dashboard.home' else '' }}">Overview</a>
      <a href="{{ url_for('dashboard.users_page') }}"
         class="{{ 'active' if request.endpoint == 'dashboard.users_page' else '' }}">Users</a>
    </nav>
    <div class="topbar-right">
      <span class="status"><span class="dot"></span> online</span>
      <a class="btn-ghost" href="{{ url_for('dashboard.logout') }}">Logout</a>
    </div>
  </header>

  {% if request.args.get('msg') %}
  <div class="toast">{{ request.args.get('msg') }}</div>
  {% endif %}

  {% block content %}{% endblock %}

  {% block scripts %}{% endblock %}
</body>
</html>
```

- [ ] **Step 2: Verify** the file exists and braces/blocks are balanced (3 `{% block %}`/`{% endblock %}` pairs, one `{% if %}`/`{% endif %}`).

---

### Task 2: Create `templates/users.html` (the Users page)

This is the Users `<section>` currently in `dashboard.html` (lines ~42–96), moved verbatim into a content block.

- [ ] **Step 1: Create the file** with this exact content:

```html
{% extends "base.html" %}
{% block content %}
  <main class="wrap">
    <section class="card users-card">
      <div class="card-title">Users <span class="muted">· {{ users|length }} enrolled</span></div>

      {% for u in users %}
      <div class="user-row">
        <div class="user-head">
          {% if u.photos %}
          <img class="user-avatar" src="{{ url_for('dashboard.user_photo', pid=u.photos[0]) }}" alt="{{ u.name }}">
          {% else %}
          <span class="user-avatar" style="display:inline-flex;align-items:center;justify-content:center;color:var(--muted)">?</span>
          {% endif %}
          <span class="user-name">{{ u.name }}</span>
          <form method="post" action="{{ url_for('dashboard.users_delete', uid=u.id) }}"
                data-name="{{ u.name }}"
                onsubmit="return confirm('Remove ' + this.dataset.name + ' and all their photos?');">
            <button class="btn-del" type="submit">delete user</button>
          </form>
        </div>
        <div class="user-photos">
          {% for pid in u.photos %}
          <div class="user-photo">
            <img src="{{ url_for('dashboard.user_photo', pid=pid) }}" alt="{{ u.name }}">
            <form method="post" action="{{ url_for('dashboard.photo_delete', pid=pid) }}">
              <button class="btn-del-x" type="submit" title="remove photo">&times;</button>
            </form>
          </div>
          {% else %}
          <span class="muted">no photos — this user can't match</span>
          {% endfor %}
          <form class="add-photo" method="post"
                action="{{ url_for('dashboard.users_add_photo', uid=u.id) }}"
                enctype="multipart/form-data">
            <label class="file">
              <input type="file" name="photo" accept="image/*" onchange="this.form.submit()">
              <span class="file-btn">+ add photo</span>
            </label>
          </form>
        </div>
      </div>
      {% else %}
      <p class="muted">No users enrolled yet. Add one below.</p>
      {% endfor %}

      <form class="add-user" method="post" action="{{ url_for('dashboard.users_create') }}"
            enctype="multipart/form-data">
        <input type="text" name="name" placeholder="Name" maxlength="64" required>
        <label class="file">
          <input type="file" name="photo" accept="image/*" required
                 onchange="this.closest('.file').querySelector('.file-name').textContent = this.files[0] ? this.files[0].name : ''">
          <span class="file-btn">Choose photo</span>
          <span class="file-name"></span>
        </label>
        <button class="btn-primary" type="submit">Add user</button>
      </form>
    </section>
  </main>
{% endblock %}
```

---

### Task 3: Rewrite `templates/dashboard.html` (Overview) to extend base

Transform the current standalone page into a base-extending template:
- Remove the `<!doctype>`/`<head>`/`<header class="topbar">`/toast wrappers (now in base).
- Move `chart.min.js` into a `{% block head %}`.
- Wrap the Overview body (hero, KPI strip, live+chart, recent events + `#event-modal`) — **but NOT the Users section** — in `{% block content %}`.
- Wrap the existing `<script>` JS in `{% block scripts %}`.

- [ ] **Step 1: Replace the whole file** so it reads:

```html
{% extends "base.html" %}
{% block head %}
  <script src="{{ url_for('static', filename='chart.min.js') }}"></script>
{% endblock %}

{% block content %}
  <main class="wrap">
    <!-- door-status hero -->
    <section class="hero">
      <div class="hero-ring" id="hero-ring">◷</div>
      <div>
        <div class="hero-label">Door status</div>
        <div class="hero-state" id="hero-state">Loading…</div>
      </div>
      <div class="hero-time" id="hero-time"></div>
    </section>

    <!-- KPI strip -->
    <section class="cards">
      <div class="card stat"><div class="stat-label">✓ Granted today</div><div class="stat-value" id="s-granted">–</div></div>
      <div class="card stat"><div class="stat-label">✕ Denied today</div><div class="stat-value" id="s-denied">–</div></div>
      <div class="card stat"><div class="stat-label">⬡ Spoof today</div><div class="stat-value" id="s-spoof">–</div></div>
      <div class="card stat"><div class="stat-label">◷ Last seen</div><div class="stat-value sm" id="s-last">–</div></div>
    </section>

    <section class="grid-2">
      <div class="card">
        <div class="card-title">Live camera</div>
        <img class="live" src="/annotated_stream" alt="live camera">
        <p class="legend">
          <span><span class="lg match"></span>match</span>
          <span><span class="lg nomatch"></span>no match</span>
          <span><span class="lg spoof"></span>spoof</span>
          <span><span class="lg noface"></span>no face</span>
        </p>
      </div>
      <div class="card">
        <div class="card-title">Access over time
          <select id="range">
            <option value="7">7 days</option>
            <option value="14">14 days</option>
            <option value="30">30 days</option>
          </select>
        </div>
        <canvas id="chart" height="200"></canvas>
      </div>
    </section>

    <section class="card">
      <div class="card-title">Recent events
        <select id="filter">
          <option value="">all</option>
          <option value="granted">granted</option>
          <option value="denied">denied</option>
        </select>
      </div>
      <table class="events">
        <thead><tr><th></th><th>Time</th><th>Verdict</th><th>Who</th><th>Distance</th></tr></thead>
        <tbody id="events-body"></tbody>
      </table>
      <div class="pager">
        <button id="prev-page" type="button">‹ Prev</button>
        <span class="page-info" id="page-info"></span>
        <button id="next-page" type="button">Next ›</button>
      </div>
    </section>
  </main>

  <div id="event-modal" class="modal-backdrop">
    <div class="modal" role="dialog" aria-modal="true" aria-labelledby="em-title">
      <div class="modal-head">
        <span id="em-title">Event details</span>
        <button type="button" id="em-close" class="modal-close" aria-label="Close">&times;</button>
      </div>
      <div class="modal-body">
        <div id="em-snapshot" class="modal-snap"></div>
        <dl class="modal-fields">
          <dt>Verdict</dt><dd id="em-verdict"></dd>
          <dt>Who</dt><dd id="em-who"></dd>
          <dt>Time</dt><dd id="em-time"></dd>
          <dt>Distance / threshold</dt><dd id="em-distance"></dd>
          <dt id="em-spoof-dt">Anti-spoof score</dt><dd id="em-spoof-dd"></dd>
          <dt>Event ID</dt><dd id="em-id"></dd>
        </dl>
      </div>
    </div>
  </div>
{% endblock %}

{% block scripts %}
  <script>
    <!-- THE EXISTING DASHBOARD JS — copied verbatim from the current file's <script> body
         (the block from `const fmtTime = ...` through `refreshStats(); refreshEvents();`).
         No JS logic changes. -->
  </script>
{% endblock %}
```

NOTE for the implementer: in Step 1 the `<script>` body must be the **exact existing JS**
already in `dashboard.html` (helpers `fmtTime/esc/relTime`, `EVENTS_PAGE_SIZE`,
`currentEvents`, `updateHero`, `refreshStats`, `drawChart`, `refreshEvents`, all the handler
wiring, the event-modal functions, and the closing `refreshStats(); refreshEvents();`).
Copy it unchanged — do not retype or alter it.

- [ ] **Step 2: Verify** the file starts with `{% extends "base.html" %}`, contains the three blocks (`head`, `content`, `scripts`), the Users `<section class="users-card">` is GONE, and the event-modal + all JS are present.

---

### Task 4: `dashboard.py` — add the Users page route and retarget redirects

- [ ] **Step 1: `home()` drops the users query.** Change:

```python
@bp.route("/dashboard")
@login_required
def home():
    return render_template("dashboard.html", users=db.list_users())
```

to:

```python
@bp.route("/dashboard")
@login_required
def home():
    return render_template("dashboard.html")


@bp.route("/dashboard/users")
@login_required
def users_page():
    return render_template("users.html", users=db.list_users())
```

- [ ] **Step 2: Retarget the four user-CRUD redirects** from `dashboard.home` to `dashboard.users_page`. In `users_create`, `users_add_photo`, `users_delete`, and `photo_delete`, change every `url_for("dashboard.home", msg=...)` to `url_for("dashboard.users_page", msg=...)`. Specifically:
  - `users_create`: both the "No file selected" redirect and the `msg=message` redirect.
  - `users_add_photo`: both the "No file selected" redirect and the `msg=message` redirect.
  - `users_delete`: the `msg="User removed."` redirect.
  - `photo_delete`: the `msg="Photo removed."` redirect.

  (Leave `index`, `login` redirects to `dashboard.home` unchanged.)

- [ ] **Step 3: Verify** `python -c "import ast; ast.parse(open('dashboard.py').read()); print('parse ok')"` prints `parse ok`, and `grep -n "dashboard.home" dashboard.py` shows only the `index` and `login` redirects remain.

---

### Task 5: `static/style.css` — nav styles

- [ ] **Step 1: Append** to the end of `static/style.css`:

```css

/* ---- top nav ---- */
.nav { display: flex; align-items: center; gap: 6px; }
.nav a {
  color: var(--muted); text-decoration: none; font-size: 14px; font-weight: 500;
  padding: 6px 12px; border-radius: 8px;
}
.nav a:hover { background: var(--panel-2); color: var(--text); }
.nav a.active { color: var(--text); background: var(--panel-2); }
```

- [ ] **Step 2: Verify** braces balanced and the file still ends cleanly.

---

## Manual verification (after all tasks; dashboard running)

Hard-refresh (Cmd+Shift+R) and check:
1. `/dashboard` shows the Overview (hero, KPIs, live, chart, events + working modal) and **no** Users card.
2. The topbar shows **Overview · Users** nav; the active page is highlighted.
3. Clicking **Users** goes to `/dashboard/users` and shows the Users card; **Users** is now highlighted.
4. Add a user / add a photo / delete a photo / delete a user → each lands back on the **Users** page with the right toast.
5. Logout works from both pages; visiting either while logged out redirects to login.
6. (Network tab) the Users page does not load `chart.min.js`; the Overview does.

## Notes
- No backend/matching/API logic changes. Only template structure, one new GET route, redirect targets, and CSS.
- The event-modal markup and all dashboard JS move with the Overview unchanged — do not edit them.
