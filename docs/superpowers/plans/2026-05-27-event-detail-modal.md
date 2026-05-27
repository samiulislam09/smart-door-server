# Event Detail Modal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **Commits:** The user owns commits. Each task ends with a suggested commit command, but do NOT run it automatically — leave changes in the working tree for the user to commit.

**Goal:** Clicking any row in the dashboard's "Recent events" table opens a modal showing that event's full details and a large snapshot.

**Architecture:** Front-end only. The events table already fetches all needed fields (`id, ts, verdict, person, distance, threshold, antispoof_score, snapshot_path`) via `GET /api/events`. We cache the current page's rows in a JS variable, give each `<tr>` a `data-idx`, and a click handler populates a modal from the cached row — no new request, no backend change.

**Tech Stack:** Flask Jinja template (`templates/dashboard.html`), vanilla JS (already in that file), CSS (`static/style.css`). No build step.

**Reference spec:** `docs/superpowers/specs/2026-05-27-event-detail-modal-design.md`

---

## File structure

- **Modify** `templates/dashboard.html` — add the modal markup (between `</main>` and `<script>`), and add JS (cache rows, `data-idx` on rows, open/populate/close handlers) inside the existing `<script>`.
- **Modify** `static/style.css` — add modal + clickable-row styles.

No automated tests exist for the dashboard UI (vanilla JS in a template, no JS test harness), so verification is manual via the running dashboard. MySQL must be running and there must be at least one logged event to click.

---

### Task 1: Modal markup + styling

**Files:**
- Modify: `templates/dashboard.html` (insert markup between `</main>` on line ~139 and `<script>` on line ~141)
- Modify: `static/style.css` (append modal styles)

- [ ] **Step 1: Add the modal markup**

In `templates/dashboard.html`, find the end of `</main>` (the line `</main>` followed by a blank line then `<script>`). Insert the following block **between** `</main>` and `<script>`:

```html

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
```

The modal is hidden by default because `.modal-backdrop` is `display:none` (Step 2); it becomes visible only when JS adds the `.open` class (Task 2).

- [ ] **Step 2: Add the CSS**

Append to the END of `static/style.css`:

```css

/* ---- event detail modal ---- */
.events tbody tr[data-idx] { cursor: pointer; }
.events tbody tr[data-idx]:hover { background: var(--panel-2); }

.modal-backdrop {
  display: none; position: fixed; inset: 0; z-index: 50;
  background: rgba(0,0,0,.6); align-items: center; justify-content: center; padding: 20px;
}
.modal-backdrop.open { display: flex; }
.modal {
  background: var(--panel); border: 1px solid var(--border); border-radius: 14px;
  width: 460px; max-width: 100%; max-height: 90vh; overflow: auto;
  box-shadow: 0 20px 60px rgba(0,0,0,.5);
}
.modal-head {
  display: flex; align-items: center; justify-content: space-between;
  padding: 14px 18px; border-bottom: 1px solid var(--border); font-weight: 600;
}
.modal-close {
  background: none; border: none; color: var(--muted); font-size: 22px;
  line-height: 1; cursor: pointer; padding: 0 4px;
}
.modal-close:hover { color: var(--text); }
.modal-body { padding: 18px; }
.modal-snap img {
  width: 100%; max-height: 50vh; object-fit: contain;
  border-radius: 10px; background: #000; display: block;
}
.modal-snap .no-snap {
  color: var(--muted); font-size: 14px; padding: 24px; text-align: center;
  border: 1px dashed var(--border); border-radius: 10px;
}
.modal-fields {
  display: grid; grid-template-columns: max-content 1fr; gap: 8px 16px;
  margin: 16px 0 0; font-size: 14px;
}
.modal-fields dt { color: var(--muted); }
.modal-fields dd { margin: 0; }
```

- [ ] **Step 3: Verify the page still loads and the modal is hidden**

Start the dashboard if not running (`conda activate smartdoor && python server.py`), open `http://127.0.0.1:8080/dashboard`, log in. Confirm:
- The page renders normally; the events table is unchanged; **no** modal is visible.
- Open the browser devtools console — no JS/HTML errors.
- (Optional sanity-check of the styling) In the console run:
  `document.getElementById('event-modal').classList.add('open')`
  → a dark-overlay modal card appears (empty fields, since JS isn't wired yet). Run
  `document.getElementById('event-modal').classList.remove('open')` to hide it again.

- [ ] **Step 4: Suggested commit (user runs)**

```bash
git add templates/dashboard.html static/style.css
git commit -m "feat(dashboard): event detail modal markup + styles"
```

---

### Task 2: Wire row clicks to populate and open/close the modal

**Files:**
- Modify: `templates/dashboard.html` (the `<script>` block: lines ~152–153 for the state var, ~204–224 `refreshEvents`, and append handlers before the final `refreshStats(); refreshEvents();` calls)

- [ ] **Step 1: Cache the current page's rows**

In `templates/dashboard.html`, find:

```javascript
    const EVENTS_PAGE_SIZE = 25;
    let eventsPage = 0;
    let chart;
```

Change it to add a `currentEvents` variable:

```javascript
    const EVENTS_PAGE_SIZE = 25;
    let eventsPage = 0;
    let currentEvents = [];   // rows of the page currently shown, indexed by data-idx
    let chart;
```

- [ ] **Step 2: Tag each row with its index and store the page**

In `refreshEvents()`, find this block:

```javascript
      const body = document.getElementById('events-body');
      body.innerHTML = page.map(r => `
        <tr>
          <td>${r.snapshot_path ? `<img class="thumb" src="/snapshots/${r.snapshot_path}">` : ''}</td>
          <td>${fmtTime(r.ts)}</td>
          <td><span class="badge ${r.verdict === 'granted' ? 'ok' : (r.verdict === 'spoof' ? 'spoof' : 'bad')}">${r.verdict}</span></td>
          <td>${esc(r.person) || '—'}</td>
          <td>${r.distance != null ? r.distance.toFixed(3) : (r.antispoof_score != null ? 'spoof ' + Number(r.antispoof_score).toFixed(2) : '—')}</td>
        </tr>`).join('') || `<tr><td colspan="5" class="muted">No events yet.</td></tr>`;
```

Replace it with (note `currentEvents = page;` and `page.map((r, i) =>` with `data-idx="${i}"`):

```javascript
      const body = document.getElementById('events-body');
      currentEvents = page;
      body.innerHTML = page.map((r, i) => `
        <tr data-idx="${i}">
          <td>${r.snapshot_path ? `<img class="thumb" src="/snapshots/${r.snapshot_path}">` : ''}</td>
          <td>${fmtTime(r.ts)}</td>
          <td><span class="badge ${r.verdict === 'granted' ? 'ok' : (r.verdict === 'spoof' ? 'spoof' : 'bad')}">${r.verdict}</span></td>
          <td>${esc(r.person) || '—'}</td>
          <td>${r.distance != null ? r.distance.toFixed(3) : (r.antispoof_score != null ? 'spoof ' + Number(r.antispoof_score).toFixed(2) : '—')}</td>
        </tr>`).join('') || `<tr><td colspan="5" class="muted">No events yet.</td></tr>`;
```

The empty-state row has no `data-idx`, so it stays non-clickable.

- [ ] **Step 3: Add the modal open/populate/close logic**

In `templates/dashboard.html`, find the end of the event-handler wiring:

```javascript
    document.getElementById('next-page').onclick = () => { eventsPage++; refreshEvents(); };
    // Load once on page load. Reload the page to refresh stats/events.
    refreshStats();
    refreshEvents();
```

Insert the modal logic **between** the `next-page` handler line and the `// Load once` comment, so it becomes:

```javascript
    document.getElementById('next-page').onclick = () => { eventsPage++; refreshEvents(); };

    // ---- event detail modal ----
    function fmtDistance(r) {
      const d = r.distance != null ? r.distance.toFixed(3) : '—';
      const t = r.threshold != null ? r.threshold.toFixed(3) : '—';
      return `${d} / ${t}`;
    }
    function openEventModal(r) {
      const snap = document.getElementById('em-snapshot');
      snap.innerHTML = r.snapshot_path
        ? `<img src="/snapshots/${esc(r.snapshot_path)}" alt="event snapshot">`
        : `<div class="no-snap">no snapshot</div>`;
      const badgeClass = r.verdict === 'granted' ? 'ok' : (r.verdict === 'spoof' ? 'spoof' : 'bad');
      document.getElementById('em-verdict').innerHTML = `<span class="badge ${badgeClass}">${esc(r.verdict)}</span>`;
      document.getElementById('em-who').textContent = r.person || '—';
      document.getElementById('em-time').textContent = `${fmtTime(r.ts)} (${relTime(r.ts)})`;
      document.getElementById('em-distance').textContent = fmtDistance(r);
      const hasSpoof = r.antispoof_score != null;
      document.getElementById('em-spoof-dt').style.display = hasSpoof ? '' : 'none';
      document.getElementById('em-spoof-dd').style.display = hasSpoof ? '' : 'none';
      document.getElementById('em-spoof-dd').textContent = hasSpoof ? Number(r.antispoof_score).toFixed(2) : '';
      document.getElementById('em-id').textContent = r.id;
      document.getElementById('event-modal').classList.add('open');
    }
    function closeEventModal() {
      document.getElementById('event-modal').classList.remove('open');
    }
    document.getElementById('events-body').addEventListener('click', (e) => {
      const tr = e.target.closest('tr[data-idx]');
      if (!tr) return;
      const row = currentEvents[Number(tr.dataset.idx)];
      if (row) openEventModal(row);
    });
    document.getElementById('em-close').onclick = closeEventModal;
    document.getElementById('event-modal').addEventListener('click', (e) => {
      if (e.target.id === 'event-modal') closeEventModal();   // click on backdrop only
    });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') closeEventModal();
    });

    // Load once on page load. Reload the page to refresh stats/events.
    refreshStats();
    refreshEvents();
```

- [ ] **Step 4: Manual verification (dashboard running, with logged events)**

Reload the dashboard with a hard refresh (Cmd+Shift+R, to bust the cached `style.css`/template). Then:
1. Hover a row → it highlights and the cursor is a pointer.
2. Click a **granted** row → modal opens showing the large snapshot, a green "granted" badge, the person's name, full time + relative time, `distance / threshold` (e.g. `0.312 / 0.593`), the event ID. No "Anti-spoof score" line.
3. Click a **denied** row → "Who" shows `—`; distance shown or `—`; no spoof line.
4. Click a **spoof** row (if any) → an "Anti-spoof score" line appears with a 2-decimal number.
5. Click a row for an event with **no snapshot** → a dashed "no snapshot" placeholder shows instead of a broken image.
6. Close the modal three ways: the `×` button, clicking the dark area outside the card, and pressing `Esc`. All three close it. Clicking *inside* the card does NOT close it.
7. Devtools console shows no errors throughout.

- [ ] **Step 5: Suggested commit (user runs)**

```bash
git add templates/dashboard.html
git commit -m "feat(dashboard): open event detail modal on row click"
```

---

## Notes for the implementer

- **No backend touched.** Every field shown comes from the row object already returned by `/api/events`. Do not add routes or DB columns.
- **XSS hygiene:** match the existing dashboard convention — string values inserted via `innerHTML` (verdict, snapshot_path) go through the existing `esc()` helper; values set via `textContent` (who, time, distance, id, spoof score) are inherently safe.
- **Why a class toggle, not the `hidden` attribute:** `.modal-backdrop` sets `display:flex` when open, which would override the `hidden` attribute's `display:none`. Toggling `.open` against a base `display:none` is unambiguous.
- **Image sizing is explicit** (`max-height: 50vh; object-fit: contain`) — deliberately constrained so the snapshot can never blow out the layout (same failure class as the earlier missing `.user-avatar` sizing).
