"use strict";

// ---- helpers ---------------------------------------------------------------------------

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

async function api(path, options = {}) {
  const opts = { headers: { Accept: "application/json" }, ...options };
  if (opts.body && typeof opts.body !== "string") {
    opts.body = JSON.stringify(opts.body);
    opts.headers["Content-Type"] = "application/json";
  }
  const resp = await fetch(path, opts);
  let data = null;
  try { data = await resp.json(); } catch (_) { /* empty body */ }
  if (!resp.ok) {
    let detail = data && data.detail;
    if (detail && typeof detail === "object") detail = JSON.stringify(detail.errors || detail);
    throw new Error(detail || `${resp.status} ${resp.statusText}`);
  }
  return data;
}

function store(key, value) {
  try {
    if (value === undefined) return localStorage.getItem(key);
    if (value === null) localStorage.removeItem(key);
    else localStorage.setItem(key, value);
  } catch (_) { /* storage unavailable */ }
  return value ?? null;
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function inline(s) {
  return escapeHtml(s)
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|\W)\*(?!\s)(.+?)\*(?=\W|$)/g, "$1<em>$2</em>");
}

// Minimal markdown: headings, bullet lists, paragraphs.
function renderMarkdown(text) {
  const out = [];
  for (const block of String(text || "").trim().split(/\n\s*\n/)) {
    const lines = block.split("\n").map((l) => l.trim()).filter(Boolean);
    let list = [];
    let para = [];
    const flushPara = () => { if (para.length) { out.push(`<p>${inline(para.join(" "))}</p>`); para = []; } };
    const flushList = () => { if (list.length) { out.push(`<ul>${list.map((i) => `<li>${inline(i)}</li>`).join("")}</ul>`); list = []; } };
    for (const line of lines) {
      const h = line.match(/^#{1,6}\s+(.*)$/);
      const li = line.match(/^(?:[-*•]|\d+[.)])\s+(.*)$/);
      if (h) { flushPara(); flushList(); out.push(`<h2>${inline(h[1])}</h2>`); }
      else if (li) { flushPara(); list.push(li[1]); }
      else { flushList(); para.push(line); }
    }
    flushPara();
    flushList();
  }
  return out.join("\n");
}

function plainText(markdown) {
  return String(markdown || "").replace(/^#+\s*/gm, "").replace(/^[-*•]\s+/gm, "").replace(/\*/g, "");
}

function plural(n, word) { return `${n} ${word}${n === 1 ? "" : "s"}`; }

function icon(name) { return `<svg class="icon"><use href="#i-${name}"/></svg>`; }

function progressBar(n, total, tone = "") {
  const pct = total ? Math.round((100 * n) / total) : 0;
  const cls = tone || (total && n >= total ? "complete" : "");
  const label = `${n} / ${total}`;
  return `<div class="progress" style="--pct:${pct}%"><div class="progress-bar ${cls}"></div>` +
    `<span class="progress-text">${label}</span><span class="progress-text on" aria-hidden="true">${label}</span></div>`;
}

function ago(iso) {
  if (!iso) return "";
  const secs = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (secs < 60) return "just now";
  if (secs < 3600) return `${Math.floor(secs / 60)} min ago`;
  if (secs < 86400) return `${Math.floor(secs / 3600)} h ago`;
  return plural(Math.floor(secs / 86400), "day") + " ago";
}

function duration(seconds) {
  if (seconds === undefined || seconds === null) return "";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  return `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`;
}

function getLength() { return store("www.length") || "standard"; }

// Toolbar dropdowns: <div class="tb-menu" data-menu="name"> with a button and a .menu list.
function initMenu(name, current, onPick) {
  const root = $(`.tb-menu[data-menu="${name}"]`);
  if (!root) return;
  const list = $(".menu", root);
  const mark = (value) => {
    for (const b of $$("button", list)) b.setAttribute("aria-checked", String(b.dataset.value === value));
  };
  mark(current);
  $(".tb-btn", root).addEventListener("click", (e) => {
    e.stopPropagation();
    const open = list.hidden;
    for (const m of $$(".menu")) m.hidden = true;
    list.hidden = !open;
  });
  list.addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    mark(b.dataset.value);
    list.hidden = true;
    onPick(b.dataset.value);
  });
}
document.addEventListener("click", () => { for (const m of $$(".menu")) m.hidden = true; });

// ---- shell: theme, sidebar, viewer, search, queue badge --------------------------------

const THEMES = ["auto", "dark", "light"];

function initTheme() {
  const btn = $("#theme-btn");
  const apply = (theme) => {
    const root = document.documentElement;
    if (theme === "auto") delete root.dataset.theme;
    else root.dataset.theme = theme;
    store("www.theme", theme === "auto" ? null : theme);
    $("use", btn).setAttribute("href", `#i-${theme === "auto" ? "auto" : theme === "dark" ? "moon" : "sun"}`);
    btn.title = `Theme: ${theme}`;
  };
  const saved = store("www.theme");
  let current = THEMES.includes(saved) ? saved : "auto";
  apply(current);
  btn.addEventListener("click", () => {
    current = THEMES[(THEMES.indexOf(current) + 1) % THEMES.length];
    apply(current);
  });
}

function initSidebar() {
  const toggle = $("#sidebar-toggle");
  const set = (open) => {
    document.body.classList.toggle("sidebar-open", open);
    toggle.setAttribute("aria-expanded", String(open));
  };
  toggle.addEventListener("click", () => set(!document.body.classList.contains("sidebar-open")));
  $("#sidebar-scrim").addEventListener("click", () => set(false));
  for (const a of $$("#sidebar a")) a.addEventListener("click", () => set(false));
}

const isAdmin = () => document.body.dataset.admin === "true";

async function refreshQueueBadge() {
  if (!isAdmin()) return;
  try {
    const { counts } = await api("/api/jobs?limit=1");
    const n = (counts.queued || 0) + (counts.running || 0);
    const badge = $("#queue-badge");
    badge.textContent = n;
    badge.hidden = !n;
  } catch (_) { /* cosmetic */ }
}

// Pages register a filter; on other pages Enter jumps to the library with the query.
let searchHandler = null;
function initSearch() {
  const input = $("#header-search");
  const q = new URLSearchParams(location.search).get("q");
  if (q) input.value = q;
  input.addEventListener("input", () => searchHandler && searchHandler(input.value.trim().toLowerCase()));
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !searchHandler && input.value.trim()) {
      location.href = `/library?q=${encodeURIComponent(input.value.trim())}`;
    }
  });
}
function onSearch(fn) {
  searchHandler = fn;
  const q = $("#header-search").value.trim().toLowerCase();
  if (q) fn(q);
}

let users = [];

function currentUserId() {
  const fromUrl = new URLSearchParams(location.search).get("user");
  if (fromUrl) return Number(fromUrl);
  const saved = Number(store("www.user"));
  if (users.some((u) => u.id === saved)) return saved;
  return users.length ? users[0].id : null;
}

async function initUsers() {
  const select = $("#user-select");
  try { users = await api("/api/users"); } catch (_) { users = []; }
  select.innerHTML = users.length
    ? users.map((u) => `<option value="${u.id}">${escapeHtml(u.display_name)}</option>`).join("")
    : "<option>No viewers</option>";
  select.disabled = !users.length;
  const uid = currentUserId();
  if (uid) select.value = String(uid);
  select.addEventListener("change", () => {
    store("www.user", select.value);
    const params = new URLSearchParams(location.search);
    if (params.has("user")) { params.set("user", select.value); location.search = params.toString(); }
    else location.reload();
  });
}

function wireSync(btn, after) {
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    btn.classList.add("busy");
    try { await api("/api/sync", { method: "POST", body: {} }); refreshQueueBadge(); if (after) setTimeout(after, 15000); }
    catch (err) { alert(err.message); }
    setTimeout(() => { btn.disabled = false; btn.classList.remove("busy"); }, 3000);
  });
}

async function startRecap(btn, body) {
  btn.disabled = true;
  try {
    const recap = await api("/api/recaps", { method: "POST", body });
    location.href = `/recaps/${recap.id}`;
  } catch (err) {
    btn.disabled = false;
    alert(`Couldn't start a recap: ${err.message}`);
  }
}

function posterImg(img, showId, hasPoster) {
  if (!hasPoster) return;
  img.src = `/api/shows/${showId}/poster`;
  img.addEventListener("error", () => img.removeAttribute("src"));
}

// ---- watching --------------------------------------------------------------------------

async function pageHome() {
  const status = $("#home-status");
  const postersEl = $("#show-posters");
  const table = $("#show-table");
  wireSync($("#sync-btn"), () => location.reload());

  let view = store("www.view") || "posters";
  let sort = store("www.sort") || "recent";
  let shows = [];
  let query = "";
  const uid = currentUserId();
  const user = users.find((u) => u.id === uid);

  initMenu("length", getLength(), (v) => store("www.length", v));
  initMenu("view", view, (v) => { view = store("www.view", v); render(); });
  initMenu("sort", sort, (v) => { sort = store("www.sort", v); render(); });
  onSearch((q) => { query = q; render(); });

  if (!uid) {
    status.innerHTML = 'No viewers yet. Connect Plex and Tautulli in <a href="/settings">Settings</a>, then press Sync.';
    return;
  }
  try { shows = await api(`/api/users/${uid}/shows`); }
  catch (err) { status.textContent = `Couldn't load shows: ${err.message}`; return; }

  const isLong = (s) => user && s.days_since !== null && s.days_since >= user.effective_gap_days;
  const awayText = (s) => s.days_since === null || s.days_since === undefined ? ""
    : s.days_since < 1 ? "today" : plural(s.days_since, "day");
  const nextText = (s) => s.next_episode
    ? `Next ${s.next_episode}${s.next_episode_title ? ` · ${s.next_episode_title}` : ""}` : "Caught up";
  const tone = (s) => (s.watched_count >= s.episode_count ? "complete" : isLong(s) ? "warning" : "");

  const recapBtn = (s, cls) =>
    `<button class="${cls}" type="button" data-recap="${s.show_id}" title="Recap">${icon("text")}</button>` +
    `<a class="${cls}" href="/shows/${s.show_id}/characters?user=${uid}" title="Characters">${icon("users")}</a>`;

  function render() {
    let list = shows.filter((s) => !query || s.title.toLowerCase().includes(query));
    if (sort === "title") list = [...list].sort((a, b) => a.title.localeCompare(b.title));
    if (sort === "away") list = [...list].sort((a, b) => (b.days_since ?? -1) - (a.days_since ?? -1));
    status.textContent = shows.length
      ? (list.length ? "" : "No shows match.")
      : "Nothing in progress. Finish an episode of something, then press Sync.";
    postersEl.hidden = view !== "posters" || !list.length;
    table.hidden = view !== "table" || !list.length;

    if (view === "posters") {
      postersEl.innerHTML = list.map((s) => `
        <div class="poster-card">
          <div class="poster">
            <img alt="" loading="lazy" data-poster="${s.has_poster ? s.show_id : ""}">
            <div class="poster-fallback">${escapeHtml(s.title)}</div>
            ${awayText(s) ? `<span class="poster-away ${isLong(s) ? "long" : ""}">${awayText(s)}</span>` : ""}
            <div class="poster-actions">${recapBtn(s, "btn btn-small")}</div>
          </div>
          ${progressBar(s.watched_count, s.episode_count, tone(s))}
          <div class="poster-info">
            <div class="poster-title" title="${escapeHtml(s.title)}">${escapeHtml(s.title)}</div>
            <div class="poster-next">${escapeHtml(nextText(s))}</div>
          </div>
        </div>`).join("");
      for (const img of $$("img[data-poster]", postersEl)) {
        if (img.dataset.poster) posterImg(img, img.dataset.poster, true);
      }
    } else {
      $("tbody", table).innerHTML = list.map((s) => `
        <tr>
          <td class="col-status"><span class="dot ${s.watched_count >= s.episode_count ? "dot-ok" : isLong(s) ? "dot-warn" : ""}"></span></td>
          <td><a class="title-link" href="/shows/${s.show_id}/characters?user=${uid}">${escapeHtml(s.title)}</a></td>
          <td>${escapeHtml(s.last_watched_episode || "")}<span class="sub">${escapeHtml(s.last_watched_title || "")}</span></td>
          <td>${s.next_episode ? `${escapeHtml(s.next_episode)}<span class="sub">${escapeHtml(s.next_episode_title || "")}</span>` : '<span class="muted">Caught up</span>'}</td>
          <td class="num">${awayText(s)}</td>
          <td class="col-progress">${progressBar(s.watched_count, s.episode_count, tone(s))}</td>
          <td class="col-actions">${recapBtn(s, "icon-btn")}</td>
        </tr>`).join("");
    }
  }

  document.querySelector(".content").addEventListener("click", (e) => {
    const b = e.target.closest("[data-recap]");
    if (b) startRecap(b, { show_id: Number(b.dataset.recap), user_id: uid, length: getLength() });
  });
  render();
}

// ---- recap -----------------------------------------------------------------------------

async function pageRecap() {
  const id = $(".recap").dataset.recapId;
  const textEl = $("#recap-text");
  const statusEl = $("#recap-status");
  const labels = { queued: "Waiting in the queue", generating: "Writing", verifying: "Checking it against your episodes",
    done: "Done", failed: "Failed" };
  let recap;
  try { recap = await api(`/api/recaps/${id}`); }
  catch (err) { $(".status-text", statusEl).textContent = err.message; return; }

  $("#recap-show").textContent = recap.show_title || "";
  document.title = `${recap.show_title} · Recap`;
  api(`/api/shows/${recap.show_id}`).then((show) => {
    const img = $("#recap-poster");
    if (show.has_poster) { img.hidden = false; posterImg(img, show.id, true); }
  }).catch(() => {});
  const lengthName = { quick: "Quick", standard: "Standard", deep: "Deep" }[recap.length] || recap.length;
  $("#recap-labels").innerHTML =
    `<span class="label label-primary">Through ${escapeHtml(recap.boundary)}</span>` +
    (recap.boundary_title ? `<span class="label">${escapeHtml(recap.boundary_title)}</span>` : "") +
    `<span class="label">${escapeHtml(lengthName)}</span>` +
    (recap.model ? `<span class="label">${escapeHtml(recap.model)}</span>` : "");
  $("#chars-btn").href = `/shows/${recap.show_id}/characters?` +
    (recap.user_id ? `user=${recap.user_id}` : `until=${recap.boundary}`);

  for (const b of $$("#recap-length [data-length]")) {
    b.classList.toggle("selected", b.dataset.length === recap.length);
    b.addEventListener("click", async () => {
      if (b.dataset.length === recap.length) return;
      store("www.length", b.dataset.length);
      startRecap(b, { show_id: recap.show_id, user_id: recap.user_id, length: b.dataset.length, until: recap.boundary });
    });
  }

  const setStatus = (s) => {
    $(".status-text", statusEl).textContent = labels[s] || s;
    statusEl.className = `status ${s === "done" || s === "failed" ? s : ""}`;
  };

  let draft = "";
  const finish = (r) => {
    textEl.classList.remove("streaming");
    setStatus(r.status);
    if (r.status === "done") {
      textEl.innerHTML = renderMarkdown(r.text);
      setupListen(r.text);
    } else {
      textEl.innerHTML = "";
      const err = $("#recap-error");
      err.textContent = r.error || "Something went wrong writing this recap.";
      err.hidden = false;
    }
    const cov = r.coverage;
    if (cov && (cov.missing.length || cov.overview_only.length)) {
      const parts = [];
      if (cov.missing.length) {
        const shown = cov.missing.slice(0, 10).join(", ") + (cov.missing.length > 10 ? "…" : "");
        parts.push(`Built from ${cov.summarized} of the ${cov.watched} episodes you've seen; ${shown} aren't indexed yet. Index the show from the Library for a complete recap.`);
      }
      if (cov.overview_only.length) {
        parts.push(`No dialogue was available for ${cov.overview_only.join(", ")}, so those come from the episode synopsis.`);
      }
      const box = $("#recap-coverage");
      box.textContent = parts.join(" ");
      box.hidden = false;
    }
    const v = r.verification;
    if (v) {
      const box = $("#recap-verification");
      const removed = v.removed || [];
      let summary = removed.length
        ? `${plural(removed.length, "sentence")} removed for not matching your episodes`
        : "Every sentence was checked against your episodes";
      if (!v.llm_checked) summary += " (name check only)";
      $("summary", box).textContent = summary;
      $("ol", box).innerHTML = removed.map((x) =>
        `<li><s>${escapeHtml(x.sentence)}</s>${x.reason ? `<span class="why">${escapeHtml(x.reason)}</span>` : ""}</li>`).join("");
      box.hidden = false;
    }
  };

  if (recap.status === "done" || recap.status === "failed") { finish(recap); return; }
  setStatus(recap.status);
  textEl.classList.add("streaming");
  const es = new EventSource(`/api/recaps/${id}?stream=true`);
  es.addEventListener("status", (e) => setStatus(JSON.parse(e.data).status));
  es.addEventListener("draft", (e) => {
    const d = JSON.parse(e.data);
    draft = d.replace !== undefined ? d.replace : draft + d.delta;
    textEl.innerHTML = renderMarkdown(draft);
  });
  es.addEventListener("final", (e) => { es.close(); finish(JSON.parse(e.data)); refreshQueueBadge(); });
  es.addEventListener("error", (e) => {
    if (e.data) {
      es.close();
      setStatus("failed");
      $(".status-text", statusEl).textContent = JSON.parse(e.data).error;
    }
  });
}

function setupListen(text) {
  const btn = $("#listen-btn");
  if (!("speechSynthesis" in window) || !text) return;
  const label = $("span", btn);
  btn.hidden = false;
  btn.addEventListener("click", () => {
    if (speechSynthesis.speaking) { speechSynthesis.cancel(); label.textContent = "Read aloud"; return; }
    const u = new SpeechSynthesisUtterance(plainText(text));
    u.onend = () => { label.textContent = "Read aloud"; };
    speechSynthesis.speak(u);
    label.textContent = "Stop";
  });
}

// ---- characters ------------------------------------------------------------------------

async function pageCharacters() {
  const root = $("#chars-root");
  const showId = root.dataset.showId;
  const until = new URLSearchParams(location.search).get("until");
  const uid = until ? null : currentUserId();
  const status = $("#chars-status");
  const tbody = $("#char-table tbody");
  if (!until && !uid) { status.textContent = "Pick a viewer first."; return; }
  const query = until ? `until=${encodeURIComponent(until)}` : `user=${uid}`;
  api(`/api/shows/${showId}`).then((show) => {
    $("#chars-show").textContent = show.title;
    document.title = `${show.title} · Characters`;
    const img = $("#chars-poster");
    if (show.has_poster) { img.hidden = false; posterImg(img, show.id, true); }
  }).catch(() => {});
  let data;
  try { data = await api(`/api/shows/${showId}/characters?${query}`); }
  catch (err) { status.textContent = err.message; return; }
  $("#chars-labels").innerHTML = `<span class="label label-primary">As of ${escapeHtml(data.as_of)}</span>` +
    `<span class="label">${plural(data.characters.length, "character")}</span>`;
  status.textContent = data.characters.length ? "" : "No one here yet. Index this show from the Library first.";

  const statusLabel = (s) => {
    if (!s) return "";
    const cls = { dead: "label-danger", missing: "label-warning", "in custody": "label-warning", injured: "label-warning" }[s] || "";
    return `<span class="label ${cls}">${escapeHtml(s)}</span>`;
  };
  tbody.innerHTML = data.characters.map((c, i) => `
    <tr data-i="${i}" data-search="${escapeHtml([c.name, ...c.aliases].join(" ").toLowerCase())}">
      <td><span class="name">${escapeHtml(c.name)}</span>${c.aliases.length ? `<span class="sub">also ${escapeHtml(c.aliases.join(", "))}</span>` : ""}</td>
      <td class="muted">${escapeHtml(c.actor || "")}</td>
      <td>${escapeHtml(c.role || "")}</td>
      <td>${statusLabel(c.status)}</td>
      <td class="code">${escapeHtml(c.first_seen === c.last_seen ? c.first_seen : `${c.first_seen}–${c.last_seen}`)}</td>
      <td class="col-actions">
        <button class="icon-btn" type="button" data-notes title="Episode notes"${c.history.length ? "" : " disabled"}>${icon("list")}</button>
        <button class="icon-btn" type="button" data-ask title="Remind me who this is">${icon("help")}</button>
      </td>
    </tr>`).join("");

  const detailRow = (tr) => {
    let next = tr.nextElementSibling;
    if (!next || !next.classList.contains("detail-row")) {
      next = document.createElement("tr");
      next.className = "detail-row";
      next.innerHTML = '<td colspan="6"></td>';
      tr.after(next);
    }
    return $("td", next);
  };
  tbody.addEventListener("click", async (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    const tr = b.closest("tr");
    const c = data.characters[Number(tr.dataset.i)];
    const cell = detailRow(tr);
    if (b.hasAttribute("data-notes")) {
      cell.innerHTML = `<ul class="notes">${c.history.map((h) =>
        `<li><span class="code">${escapeHtml(h.episode)}</span><span>${escapeHtml([h.new_info, h.status && `Now ${h.status}.`].filter(Boolean).join(" "))}</span></li>`).join("")}</ul>`;
      return;
    }
    b.disabled = true;
    cell.innerHTML = '<p class="answer">One moment…</p>';
    try {
      const body = { character_id: c.character_id };
      if (until) body.until = until; else body.user = uid;
      $(".answer", cell).textContent = (await api(`/api/shows/${showId}/whois`, { method: "POST", body })).answer;
    } catch (err) { $(".answer", cell).textContent = err.message; }
    b.disabled = false;
  });

  $("#chars-search").addEventListener("input", (e) => {
    const q = e.target.value.trim().toLowerCase();
    for (const tr of $$("tr[data-i]", tbody)) {
      const hide = Boolean(q) && !tr.dataset.search.includes(q);
      tr.hidden = hide;
      const next = tr.nextElementSibling;
      if (next && next.classList.contains("detail-row")) next.hidden = hide;
    }
  });
}

// ---- up next ---------------------------------------------------------------------------

const REASON_ICONS = { related: "spark", creator: "spark", cast: "users", taste: "heart", household: "users",
  quality: "check", fresh: "plus", new: "plus", recap: "text", pace: "play", season: "list", finish: "clock",
  back: "clock", retry: "refresh", together: "users" };
const PART_NAMES = { taste: "Fits your taste", related: "Related to what you liked", quality: "Rating",
  household: "Popular here", fresh: "New on Plex" };
const MARKS = [["love", "heart", "Love it"], ["later", "bookmark", "Save for later"],
  ["seen", "eye", "Seen it already"], ["nope", "ban", "Not for me"]];

function hm(minutes) {
  if (!minutes) return "";
  if (minutes < 60) return `${minutes} min`;
  const h = Math.floor(minutes / 60), m = minutes % 60;
  return m ? `${h} h ${m} m` : `${h} h`;
}

function factLine(item) {
  const f = item.facts || {};
  return [
    f.seasons ? plural(f.seasons, "season") : null,
    f.runtime ? `${f.runtime} min episodes` : null,
    f.hours ? `${Math.round(f.hours)} h in all` : null,
    f.status, f.rating ? `★ ${f.rating}` : null, f.content_rating,
  ].filter(Boolean);
}

function artStyle(item) {
  const c = item.colors || [];
  return c.length >= 2 ? ` style="--c1:${escapeHtml(c[0])};--c2:${escapeHtml(c[2] || c[1])}"` : "";
}

function cardBadge(item) {
  const p = item.progress;
  if (p) {
    if (p.new_season) return `<span class="un-badge new">New season</span>`;
    if (p.all_new) return `<span class="un-badge new">${p.new_count} new</span>`;
    if (p.recap) return `<span class="un-badge recap">Recap</span>`;
    return `<span class="un-badge">${escapeHtml(p.next_code)}</span>`;
  }
  if (item.in_sonarr) return `<span class="un-badge">In Sonarr</span>`;
  if (item.prefs.includes("later")) return `<span class="un-badge match">On your list</span>`;
  return "";
}

function unCard(item) {
  const p = item.progress;
  const sub = p ? `${p.next_code}${p.next_title ? ` · ${p.next_title}` : ""}` : (item.reasons[0] || {}).text || factLine(item).slice(0, 2).join(" · ");
  const why = p ? (item.reasons[0] || {}).text || "" : (item.reasons[1] || {}).text || "";
  const saved = item.prefs.includes("later");
  return `
    <article class="un-card" data-key="${escapeHtml(item.key)}">
      <button class="un-poster" type="button" data-open aria-label="${escapeHtml(item.title)}">
        ${item.poster ? `<img alt="" loading="lazy" src="${escapeHtml(item.poster)}">` : ""}
        <span class="poster-fallback">${escapeHtml(item.title)}</span>
        ${cardBadge(item)}
        ${p ? `<span class="un-progress" style="--pct:${Math.round(100 * p.watched / Math.max(1, p.total))}%"></span>` : ""}
      </button>
      <div class="un-quick">
        ${item.play_url ? `<a class="icon-btn" href="${escapeHtml(item.play_url)}" target="_blank" rel="noopener" title="Play in Plex">${icon("play")}</a>` : ""}
        <button class="icon-btn${saved ? " on" : ""}" type="button" data-mark="later" title="${saved ? "On your list" : "Save for later"}">${icon("bookmark")}</button>
        <button class="icon-btn" type="button" data-mark="nope" title="Not for me">${icon("ban")}</button>
      </div>
      <div class="un-info">
        <div class="un-title" title="${escapeHtml(item.title)}">${escapeHtml(item.title)}</div>
        <div class="un-sub">${escapeHtml(sub)}</div>
        ${why ? `<div class="un-why">${escapeHtml(why)}</div>` : ""}
      </div>
    </article>`;
}

async function pageUpnext() {
  const status = $("#un-status");
  const lanesEl = $("#lanes");
  const hero = $("#tonight");
  const drawer = $("#drawer");
  const scrim = $("#drawer-scrim");
  const uid = currentUserId();
  if (!uid) {
    status.innerHTML = 'No viewers yet. Connect Plex and Tautulli in <a href="/settings">Settings</a>, then press Sync.';
    return;
  }
  const filters = {
    time: store("www.un.time") || "any",
    story: store("www.un.story") || "any",
    genre: store("www.un.genre") || "",
  };
  let withUser = Number(store("www.un.with")) || null;
  let data = null;
  let items = new Map();
  let heroIndex = 0;
  let query = "";

  initMenu("time", filters.time, (v) => { filters.time = store("www.un.time", v); load(); });
  initMenu("story", filters.story, (v) => { filters.story = store("www.un.story", v); load(); });
  if ($('.tb-menu[data-menu="with"]')) {
    const others = users.filter((u) => u.id !== uid);
    $('.tb-menu[data-menu="with"] .menu').innerHTML = '<li><button data-value="">Just me</button></li>' +
      others.map((u) => `<li><button data-value="${u.id}">${escapeHtml(u.display_name)}</button></li>`).join("");
    if (withUser && !others.some((u) => u.id === withUser)) withUser = null;
    initMenu("with", withUser ? String(withUser) : "", (v) => {
      withUser = Number(v) || null;
      store("www.un.with", withUser ? String(withUser) : null);
      load();
    });
  }
  onSearch((q) => { query = q; renderLanes(); });

  async function load() {
    const params = new URLSearchParams({ time: filters.time, story: filters.story, genre: filters.genre });
    if (withUser) params.set("with_user", withUser);
    lanesEl.classList.add("loading");
    try { data = await api(`/api/users/${uid}/upnext?${params}`); }
    catch (err) { status.textContent = `Couldn't work out what's next: ${err.message}`; lanesEl.classList.remove("loading"); return; }
    lanesEl.classList.remove("loading");
    items = new Map();
    for (const lane of data.lanes) for (const it of lane.items) if (!items.has(it.key)) items.set(it.key, it);
    if (data.tonight) for (const it of [data.tonight.pick, ...data.tonight.others]) if (!items.has(it.key)) items.set(it.key, it);
    genreMenu();
    status.hidden = Boolean(data.lanes.length);
    status.textContent = data.lanes.length ? "" : "Nothing to suggest yet. Watch something, then press Sync.";
    const st = data.status;
    const notes = [];
    if (st.with_details < st.shows * 0.95) notes.push(`Still reading show details (${st.with_details} of ${st.shows}); picks get better as it goes.`);
    else if (st.tmdb && st.with_tmdb < st.shows * 0.95) notes.push(`Reading TMDB details: ${st.with_tmdb} of ${st.shows} shows so far.`);
    if (!st.tmdb) notes.push("Add a TMDB key in Settings for related-show picks and the Not in your library row.");
    const active = [filters.time !== "any", filters.story !== "any", Boolean(filters.genre)].filter(Boolean).length;
    if (active) notes.push(`${plural(active, "filter")} on: they shape the new-show rows, not what you're already watching.`);
    $("#filter-note").hidden = !notes.length;
    $("#filter-note").textContent = notes.join(" ");
    heroIndex = 0;
    renderHero();
    renderTaste();
    renderLanes();
  }

  function genreMenu() {
    const list = $('.tb-menu[data-menu="genre"] .menu');
    list.innerHTML = '<li><button data-value="">Any genre</button></li>' + data.genres.map((g) =>
      `<li><button data-value="${escapeHtml(g.value)}">${escapeHtml(g.label)}</button></li>`).join("");
    for (const b of $$("button", list)) b.setAttribute("aria-checked", String(b.dataset.value === filters.genre));
  }
  initMenu("genre", filters.genre, (v) => { filters.genre = store("www.un.genre", v || null) || ""; load(); });

  const HERO_LABELS = { keep: "Pick up where you left off", new: "New episodes are waiting", start: "Something new" };

  function renderHero() {
    const t = data.tonight;
    hero.hidden = !t;
    if (!t) return;
    const all = [t.pick, ...t.others];
    const it = all[heroIndex % all.length];
    const p = it.progress;
    const nextLine = p
      ? `${p.next_code}${p.next_title ? ` · ${p.next_title}` : ""}${p.next_minutes ? ` · ${p.next_minutes} min` : ""}`
      : factLine(it).slice(0, 3).join(" · ");
    hero.innerHTML = `
      <div class="hero-bg"${artStyle(it)}>${it.art ? `<img alt="" src="${escapeHtml(it.art)}">` : ""}</div>
      <div class="hero-body">
        <div class="hero-poster">${it.poster ? `<img alt="" src="${escapeHtml(it.poster)}">` : ""}</div>
        <div class="hero-text">
          <div class="hero-eyebrow">${icon("spark")}Tonight · ${escapeHtml(HERO_LABELS[it.lane] || "")}</div>
          <h1 class="hero-title">${escapeHtml(it.title)}</h1>
          <div class="hero-line">${escapeHtml(nextLine)}</div>
          <ul class="hero-reasons">${it.reasons.slice(0, 3).map((r) => `<li>${icon(REASON_ICONS[r.kind] || "spark")}${escapeHtml(r.text)}</li>`).join("")}</ul>
          <div class="hero-actions">
            ${it.play_url ? `<a class="btn btn-primary" href="${escapeHtml(it.play_url)}" target="_blank" rel="noopener">${icon("play")}${p ? `Play ${escapeHtml(p.next_code)}` : "Open in Plex"}</a>` : ""}
            ${p && p.recap ? `<button class="btn" type="button" data-recap="${it.show_id}">${icon("text")}Recap first</button>` : ""}
            <button class="btn" type="button" data-open-key="${escapeHtml(it.key)}">${icon("list")}Details</button>
            ${all.length > 1 ? `<button class="btn btn-ghost" type="button" id="hero-next">${icon("refresh")}Something else</button>` : ""}
          </div>
        </div>
      </div>`;
    const next = $("#hero-next", hero);
    if (next) next.addEventListener("click", () => { heroIndex += 1; renderHero(); });
  }

  function renderTaste() {
    const el = $("#taste");
    const t = data.taste;
    el.hidden = !t.shows;
    if (!t.shows) return;
    const bits = [plural(t.shows, "show")];
    if (t.finish_rate !== null) bits.push(`finishes ${t.finish_rate}%`);
    $("#taste-line").textContent = `· ${bits.join(" · ")}`;
    const kinds = { g: "Genre", k: "Theme", c: "Creator", n: "Network", a: "Actor" };
    const top = t.tags.length ? t.tags[0].weight : 1;
    const stats = [
      ["Watched here", plural(t.shows, "show")],
      ["Finished", `${t.finished}${t.finish_rate !== null ? ` (${t.finish_rate}% of what you start)` : ""}`],
      ["Watching now", t.watching],
      ["Dropped early", `${t.dropped}${t.drops_genre ? `, mostly ${t.drops_genre}` : ""}`],
      ["Usual pace", t.pace ? `${t.pace} episodes a day when you're into something` : "—"],
    ];
    $(".taste-body", el).innerHTML = `
      <div class="taste-tags">${t.tags.map((g) => `<span class="taste-tag" style="--w:${(0.35 + 0.65 * g.weight / top).toFixed(2)}" title="${kinds[g.kind] || ""}"><small>${kinds[g.kind] || ""}</small>${escapeHtml(g.label)}</span>`).join("")}</div>
      <dl class="taste-stats">${stats.map(([k, v]) => `<dt>${k}</dt><dd>${escapeHtml(String(v))}</dd>`).join("")}</dl>
      <p class="hint">Built from what you finish, binge and drop. Love it / Not for me on any show tunes it.</p>`;
  }

  function renderLanes() {
    const match = (it) => !query || it.title.toLowerCase().includes(query);
    lanesEl.innerHTML = data.lanes.map((lane) => {
      const list = lane.items.filter(match);
      if (!list.length) return "";
      return `
        <section class="lane" data-lane="${lane.key}">
          <header class="lane-head">
            <h2>${escapeHtml(lane.title)} <span class="lane-count">${list.length}</span></h2>
            <span class="lane-sub">${escapeHtml(lane.subtitle)}</span>
            <span class="lane-nav">
              <button class="icon-btn" type="button" data-scroll="-1" aria-label="Back">${icon("chevron-left")}</button>
              <button class="icon-btn" type="button" data-scroll="1" aria-label="More">${icon("chevron-right")}</button>
            </span>
          </header>
          <div class="lane-row">${list.map(unCard).join("")}</div>
        </section>`;
    }).join("");
    for (const img of $$("img", lanesEl)) img.addEventListener("error", () => img.remove());
  }

  // ---- details drawer ----

  let open = null;
  function openDrawer(key, eyebrow = "") {
    const it = items.get(key);
    if (!it) return;
    open = key;
    const p = it.progress;
    const f = it.facts || {};
    const parts = it.parts ? Object.entries(it.parts).filter(([k]) => PART_NAMES[k]) : [];
    const marks = MARKS.map(([kind, ic, label]) => `<button class="mark${it.prefs.includes(kind) ? " on" : ""}" type="button" data-mark="${kind}">${icon(ic)}${label}</button>`).join("");
    const reasonList = it.reasons.length
      ? `<ul class="why">${it.reasons.map((r) => `<li>${icon(REASON_ICONS[r.kind] || "spark")}<span>${escapeHtml(r.text)}</span></li>`).join("")}</ul>` : "";
    const progressBlock = p ? `
      <div class="d-progress">
        ${progressBar(p.watched, p.total, p.recap ? "warning" : "")}
        <p>${p.last_code ? `Last watched ${escapeHtml(p.last_code)}, ${p.days_since === 0 ? "today" : plural(p.days_since, "day") + " ago"}. ` : ""}Next is <b>${escapeHtml(p.next_code)}</b>${p.next_title ? ` · ${escapeHtml(p.next_title)}` : ""}.
        ${p.left_in_season > 1 ? `${p.left_in_season} episodes (${hm(p.season_minutes)}) left in the season.` : ""}
        ${p.finish_days ? `At your pace, done in about ${plural(p.finish_days, "day")}.` : ""}</p>
      </div>` : "";
    $(".drawer-body", drawer).innerHTML = `
      <div class="d-head"${artStyle(it)}>
        ${it.art ? `<img class="d-art" alt="" src="${escapeHtml(it.art)}">` : ""}
        <div class="d-head-inner">
          ${it.poster ? `<img class="d-poster" alt="" src="${escapeHtml(it.poster)}">` : ""}
          <div>
            ${eyebrow ? `<div class="hero-eyebrow">${icon("shuffle")}${escapeHtml(eyebrow)}</div>` : ""}
            <h2 class="d-title">${escapeHtml(it.title)}${it.year ? ` <span class="muted">(${it.year})</span>` : ""}</h2>
            <div class="labels">${factLine(it).map((x) => `<span class="label">${escapeHtml(x)}</span>`).join("")}</div>
          </div>
        </div>
      </div>
      <div class="d-section d-actions">
        ${it.play_url ? `<a class="btn btn-primary" href="${escapeHtml(it.play_url)}" target="_blank" rel="noopener">${icon("play")}${p ? `Play ${escapeHtml(p.next_code)}` : "Open in Plex"}</a>` : ""}
        ${p ? `<button class="btn" type="button" data-recap="${it.show_id}">${icon("text")}Recap</button>
               <a class="btn" href="/shows/${it.show_id}/characters?user=${uid}">${icon("users")}Characters</a>` : ""}
        ${!it.in_library && data.can_add && !it.in_sonarr ? `<button class="btn btn-primary" type="button" data-sonarr="${it.tmdb_id}">${icon("plus")}Add to Sonarr</button>` : ""}
        ${!it.in_library && it.in_sonarr ? `<span class="label label-success">In Sonarr</span>` : ""}
        ${it.tmdb_id ? `<a class="btn btn-ghost" href="https://www.themoviedb.org/tv/${it.tmdb_id}" target="_blank" rel="noopener">${icon("external")}TMDB</a>` : ""}
      </div>
      ${progressBlock}
      ${it.tagline ? `<p class="d-tagline">${escapeHtml(it.tagline)}</p>` : ""}
      ${it.overview ? `<p class="d-overview">${escapeHtml(it.overview)}</p>` : ""}
      ${(it.warnings || []).map((w) => `<div class="alert alert-warning">${escapeHtml(w)}</div>`).join("")}
      ${reasonList ? `<div class="d-section"><h3>Why it's here</h3>${reasonList}</div>` : ""}
      ${parts.length ? `<div class="d-section"><h3>What scored it${it.match !== null && it.match !== undefined ? ` <span class="muted">· fits you better than ${Math.min(99, it.match)}% of your library</span>` : ""}</h3>
        <div class="parts">${parts.map(([k, v]) => `<div class="part"><span>${PART_NAMES[k]}</span><span class="part-bar"><span style="width:${Math.round(100 * Math.max(0, Math.min(1, v)))}%"></span></span></div>`).join("")}</div></div>` : ""}
      <div class="d-section"><h3>Why would I like it?</h3>
        <p class="pitch muted" id="pitch">A short, spoiler-free pitch written for you by the language model.</p>
        <button class="btn btn-small" type="button" id="pitch-btn">${icon("spark")}Write one</button></div>
      <div class="d-section"><h3>Your say</h3><div class="marks">${marks}</div></div>`;
    for (const img of $$("img", drawer)) img.addEventListener("error", () => img.remove());
    $("#pitch-btn", drawer).addEventListener("click", (e) => pitch(e.currentTarget, it));
    drawer.classList.add("open");
    drawer.setAttribute("aria-hidden", "false");
    scrim.hidden = false;
    drawer.scrollTop = 0;
    drawer.focus();
  }

  function closeDrawer() {
    open = null;
    drawer.classList.remove("open");
    drawer.setAttribute("aria-hidden", "true");
    scrim.hidden = true;
  }

  async function pitch(btn, it) {
    const out = $("#pitch", drawer);
    btn.disabled = true;
    out.classList.remove("muted");
    out.innerHTML = '<span class="spinner"></span> Thinking about what you like…';
    try {
      const res = await api(`/api/users/${uid}/pitch`, { method: "POST", body: { key: it.key } });
      out.textContent = res.text;
      btn.hidden = true;
    } catch (err) {
      out.textContent = `Couldn't write one: ${err.message}`;
      btn.disabled = false;
    }
  }

  async function mark(key, kind, btn) {
    const it = items.get(key);
    if (!it) return;
    const on = !it.prefs.includes(kind);
    if (btn) btn.disabled = true;
    try {
      const res = await api(`/api/users/${uid}/preferences`, { method: "PUT", body: { key, kind, on } });
      it.prefs = res.prefs;
      if (kind === "nope" && on) {
        for (const card of $$(`.un-card[data-key="${CSS.escape(key)}"]`)) card.classList.add("gone");
        if (open === key) closeDrawer();
      }
      if (open === key) openDrawer(key);
      for (const b of $$(`.un-card[data-key="${CSS.escape(key)}"] [data-mark="later"]`)) b.classList.toggle("on", it.prefs.includes("later"));
      clearTimeout(mark.reload);
      mark.reload = setTimeout(() => { if (!open) load(); }, 4000);  // marks retune the picks
    } catch (err) { alert(err.message); }
    if (btn) btn.disabled = false;
  }

  document.querySelector(".content").addEventListener("click", async (e) => {
    const t = e.target;
    const scroll = t.closest("[data-scroll]");
    if (scroll) {
      const row = $(".lane-row", scroll.closest(".lane"));
      row.scrollBy({ left: Number(scroll.dataset.scroll) * row.clientWidth * 0.9, behavior: "smooth" });
      return;
    }
    const recap = t.closest("[data-recap]");
    if (recap) { startRecap(recap, { show_id: Number(recap.dataset.recap), user_id: uid, length: getLength() }); return; }
    const sonarr = t.closest("[data-sonarr]");
    if (sonarr) {
      sonarr.disabled = true;
      try {
        const res = await api("/api/sonarr/add", { method: "POST", body: { tmdb_id: Number(sonarr.dataset.sonarr) } });
        sonarr.outerHTML = `<span class="label label-success">${res.added ? "Added to Sonarr" : "Already in Sonarr"}</span>`;
        const it = items.get(open);
        if (it) it.in_sonarr = true;
      } catch (err) { alert(`Sonarr: ${err.message}`); sonarr.disabled = false; }
      return;
    }
    const markBtn = t.closest("[data-mark]");
    if (markBtn) {
      const key = open && drawer.contains(markBtn) ? open : markBtn.closest("[data-key]").dataset.key;
      mark(key, markBtn.dataset.mark, markBtn);
      return;
    }
    const opener = t.closest("[data-open], [data-open-key]");
    if (opener) openDrawer(opener.dataset.openKey || opener.closest("[data-key]").dataset.key);
  });
  $("#drawer-close").addEventListener("click", closeDrawer);
  scrim.addEventListener("click", closeDrawer);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && open) closeDrawer(); });

  $("#surprise-btn").addEventListener("click", () => {
    const lane = data && (data.lanes.find((l) => l.key === "start") || data.lanes.find((l) => l.key === "retry"));
    if (!lane) return;
    const pool = lane.items.slice(0, 15);
    const weights = pool.map((it) => Math.max(0.01, it.score) ** 2);
    let r = Math.random() * weights.reduce((a, b) => a + b, 0);
    let pick = pool[0];
    for (let i = 0; i < pool.length; i++) { r -= weights[i]; if (r <= 0) { pick = pool[i]; break; } }
    openDrawer(pick.key, "Surprise pick");
  });
  const detailsBtn = $("#details-btn");
  if (detailsBtn) {
    detailsBtn.addEventListener("click", async () => {
      detailsBtn.disabled = true;
      detailsBtn.classList.add("busy");
      try { await api("/api/upnext/refresh", { method: "POST" }); refreshQueueBadge(); }
      catch (err) { alert(err.message); }
      setTimeout(() => { detailsBtn.disabled = false; detailsBtn.classList.remove("busy"); load(); }, 8000);
    });
  }
  await load();
}

// ---- library ---------------------------------------------------------------------------

async function pageLibrary() {
  const tbody = $("#show-table tbody");
  let filter = store("www.libfilter") || "watched";
  let shows = [];
  let query = "";

  const render = () => {
    const list = shows.filter((s) => !query || s.title.toLowerCase().includes(query));
    $("#shows-status").textContent = shows.length
      ? (list.length ? "" : "No shows match.")
      : "No shows yet. Press Sync to read your Plex library.";
    tbody.innerHTML = list.map((s) => {
      const st = s.stats;
      const done = st.episodes && st.summarized >= st.episodes && !st.outdated;
      const update = st.episodes && st.summarized >= st.episodes;
      return `
      <tr>
        <td><span class="title-link">${escapeHtml(s.title)}</span>${s.year ? ` <span class="muted">(${s.year})</span>` : ""}</td>
        <td class="col-progress">${progressBar(st.with_source, st.episodes)}</td>
        <td class="col-progress">${progressBar(st.summarized, st.episodes)}${st.outdated
          ? `<span class="sub" title="Written before your TMDB key or a newer prompt. Index redoes them.">${st.outdated} outdated</span>` : ""}</td>
        <td class="num">${st.rollups} / ${st.seasons}</td>
        <td class="col-actions">${done ? `<span class="label label-success">Indexed</span>`
          : `<button class="btn btn-small" type="button" data-index="${s.id}">${icon("index")}${update ? "Update" : "Index"}</button>`}</td>
      </tr>`;
    }).join("");
  };
  const load = async () => {
    try { shows = await api(`/api/shows?watched_only=${filter === "watched"}`); }
    catch (err) { $("#shows-status").textContent = err.message; return; }
    render();
  };

  initMenu("filter", filter, (v) => { filter = store("www.libfilter", v); load(); });
  onSearch((q) => { query = q; render(); });
  wireSync($("#sync-btn"), load);
  tbody.addEventListener("click", async (e) => {
    const b = e.target.closest("button[data-index]");
    if (!b) return;
    b.disabled = true;
    try {
      await api(`/api/shows/${b.dataset.index}/index`, { method: "POST", body: { scope: "all" } });
      b.innerHTML = `${icon("clock")}Queued`;
      refreshQueueBadge();
    } catch (err) { alert(err.message); b.disabled = false; }
  });
  await load();
  setInterval(load, 20000);
}

// ---- activity --------------------------------------------------------------------------

const JOB_NAMES = { sync_meta: "Show details", sync_library: "Library scan", sync_history: "Watch history sync", index_show: "Index show",
  index_episode: "Index episode", rollup_season: "Season summary", generate_recap: "Recap", backfill: "Nightly catch-up" };

async function pageActivity() {
  const tbody = $("#job-table tbody");
  const dot = { done: "dot-ok", failed: "dot-error", running: "dot-run", queued: "", cancelled: "" };
  const load = async () => {
    let data;
    try { data = await api("/api/jobs?limit=100"); } catch (err) { $("#jobs-status").textContent = err.message; return; }
    const c = data.counts;
    $("#job-counts").textContent = ["running", "queued", "failed"].filter((k) => c[k]).map((k) => `${c[k]} ${k}`).join(" · ");
    $("#jobs-status").textContent = data.jobs.length ? "" : "Nothing has run yet.";
    tbody.innerHTML = data.jobs.map((j) => {
      const detail = j.error ? j.error.split("\n")[0] : (j.message || "");
      return `
      <tr>
        <td class="col-status"><span class="dot ${dot[j.status] || ""}" title="${j.status}"></span></td>
        <td><span class="job-type">${escapeHtml(JOB_NAMES[j.type] || j.type)}</span>
          <span class="sub">${escapeHtml(j.subject || "")}${j.attempts > 1 ? ` · try ${j.attempts}` : ""}</span></td>
        <td><div class="job-detail" title="${escapeHtml(detail)}">${j.status === "queued" || j.status === "cancelled" ? `<span class="muted">${j.status}</span>` : escapeHtml(detail)}</div></td>
        <td class="hide-sm">${ago(j.created_at)}</td>
        <td class="num">${duration(j.timings && j.timings.seconds)}</td>
        <td class="col-actions">${j.status === "failed" ? `<button class="icon-btn" type="button" data-retry="${j.id}" title="Retry">${icon("refresh")}</button>` : ""}</td>
      </tr>`;
    }).join("");
    refreshQueueBadge();
  };
  tbody.addEventListener("click", async (e) => {
    const b = e.target.closest("button[data-retry]");
    if (!b) return;
    try { await api(`/api/jobs/${b.dataset.retry}/retry`, { method: "POST" }); load(); }
    catch (err) { alert(err.message); }
  });
  $("#refresh-btn").addEventListener("click", load);
  $("#clear-btn").addEventListener("click", async () => {
    if (!confirm("Cancel everything waiting in the queue? Jobs already running will finish.")) return;
    try { await api("/api/jobs/cancel", { method: "POST" }); load(); }
    catch (err) { alert(err.message); }
  });
  await load();
  setInterval(load, 3000);
}

// ---- system ----------------------------------------------------------------------------

async function pageSystem() {
  const tbody = $("#health-table tbody");
  const names = { database: "Database", plex: "Plex", tautulli: "Tautulli", llm: "Language model", sonarr: "Sonarr", ffmpeg: "ffmpeg",
    whisper: "Whisper", queue: "Queue" };
  const load = async () => {
    try {
      const h = await api("/api/health");
      tbody.innerHTML = Object.entries(h.checks).map(([name, c]) => `
        <tr><td class="col-status"><span class="dot ${c.ok ? "dot-ok" : "dot-error"}"></span></td>
        <td>${escapeHtml(names[name] || name)}</td><td>${escapeHtml(c.detail)}</td></tr>`).join("");
    } catch (err) { tbody.innerHTML = `<tr><td></td><td colspan="2">${escapeHtml(err.message)}</td></tr>`; }
  };
  $("#refresh-btn").addEventListener("click", load);
  await load();
}

// ---- settings --------------------------------------------------------------------------

const GROUP_NAMES = { Connections: "Servers", Model: "Language model", Sources: "Dialogue",
  Recaps: "Recaps", Notifications: "Notifications", Sonarr: "Sonarr", Security: "Security", Schedule: "Schedule" };
const FIELD_LABELS = {
  plex_url: ["Plex server", "e.g. https://192.168.1.100:32400"],
  plex_token: ["Plex token"],
  plex_libraries: ["Libraries", "comma-separated; blank means every TV library"],
  tautulli_url: ["Tautulli", "e.g. http://192.168.1.100:8181"],
  tautulli_api_key: ["Tautulli API key"],
  verify_ssl: ["Check TLS certificates", "turn off for Plex over https by IP"],
  media_path_map: ["Media path mapping", "plex path=path here; separate pairs with ;"],
  tmdb_api_key: ["TMDB API key", "synopses, cast, and Up Next's related shows"],
  public_url: ["This app's address", "used in notification links"],
  webhook_secret: ["Webhook secret", "optional ?token= for the webhook"],
  llm_base_url: ["Model endpoint", "Ollama or any OpenAI-compatible URL"],
  llm_api_key: ["API key", "hosted APIs only"],
  llm_model: ["Model"],
  llm_api: ["API style"],
  llm_context_tokens: ["Context window", "tokens"],
  llm_temperature: ["Temperature"],
  verify_model: ["Fact-check model", "blank uses the main model"],
  subtitle_languages: ["Subtitle languages", "in order of preference"],
  whisper_enabled: ["Transcribe with Whisper", "when an episode has no subtitles"],
  whisper_model: ["Whisper model"],
  whisper_device: ["Whisper device", "cuda or cpu"],
  allow_overview_only: ["Fall back to the synopsis", "when there's no dialogue at all"],
  watched_percent: ["Counts as watched at", "percent"],
  default_gap_days: ["Push a recap after", "days away"],
  recap_hide_title: ["Hide the show's title from the model"],
  recap_verify: ["Fact-check every recap"],
  recap_detail_episodes: ["Recent episodes in full detail"],
  recap_cache_hours: ["Reuse a recap for", "hours"],
  ntfy_url: ["ntfy topic URL"],
  ntfy_token: ["ntfy token"],
  discord_webhook_url: ["Discord webhook"],
  sonarr_url: ["Sonarr", "optional; e.g. http://192.168.1.100:8989, for Add to Sonarr in Up Next"],
  sonarr_api_key: ["Sonarr API key", "Sonarr → Settings → General"],
  sonarr_quality_profile: ["Quality profile", "name; blank uses the first"],
  sonarr_root_folder: ["Root folder", "path; blank uses the first"],
  sonarr_search: ["Search right away", "for episodes as soon as a show is added"],
  sync_interval_minutes: ["Check watch history every", "minutes"],
  library_scan_interval_minutes: ["Rescan the library every", "minutes"],
  admin_password: ["Admin password", "setting one turns login on"],
  plex_login: ["Sign in with Plex", "anyone with access to your Plex server; its owner is an admin"],
  summary_source: ["Episode summaries from"],
  auto_index: ["Index automatically", "when someone starts a show, and overnight; uses the GPU without asking"],
  backfill_hour: ["Overnight indexing at", "hour, 0–23; only with automatic indexing on"],
};

const CHOICES = {
  llm_api: [["auto", "Detect"], ["ollama", "Ollama"], ["openai", "OpenAI-compatible"]],
  summary_source: [["dialogue", "Dialogue, with the model (detailed, uses the GPU)"],
    ["synopsis", "TMDB/Plex synopsis and cast (fast, no GPU)"]],
};

const slug = (s) => s.toLowerCase().replace(/[^a-z0-9]+/g, "-");

async function pageSettings() {
  $("#webhook-url").textContent = `${location.origin}/webhooks/tautulli`;
  const groupsEl = $("#settings-groups");
  const msg = $("#settings-msg");
  let original = {};
  const render = (data) => {
    original = {};
    groupsEl.innerHTML = data.groups.map((g) => {
      const title = GROUP_NAMES[g.name] || g.name;
      return `<fieldset class="form-section" id="${slug(title)}"><legend>${escapeHtml(title)}</legend>
      ${g.fields.map((f) => {
        original[f.key] = f.value === null ? "" : String(f.value);
        const id = `set-${f.key}`;
        const [label, hint] = FIELD_LABELS[f.key] || [f.key];
        let input;
        if (f.type === "bool") {
          input = `<select id="${id}" name="${f.key}"><option value="true">Yes</option><option value="false">No</option></select>`;
        } else if (CHOICES[f.key]) {
          input = `<select id="${id}" name="${f.key}">` +
            CHOICES[f.key].map(([v, t]) => `<option value="${v}">${t}</option>`).join("") + "</select>";
        } else {
          const type = f.secret ? "password" : (f.type === "int" || f.type === "float") ? "number" : "text";
          const extra = f.type === "float" ? ' step="any"' : "";
          const list = (f.key === "llm_model" || f.key === "verify_model") ? ' list="model-list"' : "";
          input = `<input id="${id}" name="${f.key}" type="${type}"${extra}${list} autocomplete="off" spellcheck="false">`;
        }
        return `<div class="form-row">
          <label class="form-label" for="${id}">${escapeHtml(label)}${f.overridden ? '<span class="form-saved">saved</span>' : ""}</label>
          <div class="form-input">${input}
            <div class="form-help">${hint ? `${escapeHtml(hint)} · ` : ""}<span class="env">${f.key.toUpperCase()}</span></div>
          </div></div>`;
      }).join("")}</fieldset>`;
    }).join("") + '<datalist id="model-list"></datalist>';
    for (const [key, value] of Object.entries(original)) {
      const el = groupsEl.querySelector(`[name="${key}"]`);
      if (el) el.value = value;
    }
    api("/api/llm/models").then((m) => {
      $("#model-list").innerHTML = m.models.map((x) => `<option value="${escapeHtml(x)}">`).join("");
    }).catch(() => {});
    if (location.hash) { const target = $(location.hash); if (target) target.scrollIntoView(); }
  };
  try { render(await api("/api/settings")); }
  catch (err) { groupsEl.textContent = err.message; }

  $("#settings-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const changes = {};
    for (const el of $$("[name]", groupsEl)) {
      if (el.value !== original[el.name]) changes[el.name] = el.value === "" ? null : el.value;
    }
    if (!Object.keys(changes).length) { msg.textContent = "No changes"; return; }
    try {
      render(await api("/api/settings", { method: "PUT", body: changes }));
      msg.textContent = `Saved ${plural(Object.keys(changes).length, "change")}`;
    } catch (err) { msg.textContent = `Not saved: ${err.message}`; }
  });

  const tbody = $("#user-table tbody");
  if (!users.length) { tbody.innerHTML = '<tr><td colspan="5" class="muted">No viewers yet.</td></tr>'; return; }
  tbody.innerHTML = users.map((u) => `
    <tr data-user="${u.id}">
      <td>${escapeHtml(u.display_name)}</td>
      <td><input type="url" name="notify_target" placeholder="Defaults under Notifications" value="${escapeHtml(u.notify_target || "")}" aria-label="Push to"></td>
      <td class="num"><input type="number" name="gap_threshold_days" min="0" placeholder="${u.effective_gap_days}" value="${u.gap_threshold_days ?? ""}" aria-label="Days away"></td>
      <td><label class="check"><input type="checkbox" name="notifications_enabled" ${u.notifications_enabled ? "checked" : ""}> On</label></td>
      <td class="col-actions"><button class="btn btn-small" type="button">Save</button></td>
    </tr>`).join("");
  tbody.addEventListener("click", async (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    const row = b.closest("tr");
    const gap = $("[name=gap_threshold_days]", row).value;
    try {
      await api(`/api/users/${row.dataset.user}`, { method: "PUT", body: {
        notify_target: $("[name=notify_target]", row).value,
        gap_threshold_days: gap === "" ? null : Number(gap),
        notifications_enabled: $("[name=notifications_enabled]", row).checked } });
      b.textContent = "Saved";
      setTimeout(() => { b.textContent = "Save"; }, 2000);
    } catch (err) { alert(`Not saved: ${err.message}`); }
  });
}

// ---- login -----------------------------------------------------------------------------

function pageLogin() {
  const next = $(".login-card").dataset.next || "/";
  const msg = $("#login-msg");
  const plex = $("#plex-btn");
  if (plex) {
    plex.addEventListener("click", async () => {
      plex.disabled = true;
      try {
        const { url } = await api("/auth/plex/start", { method: "POST", body: { next } });
        location.href = url;
      } catch (err) { msg.textContent = err.message; plex.disabled = false; }
    });
  }
  $("#password-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = $("button[type=submit]", e.currentTarget);
    btn.disabled = true;
    msg.textContent = "";
    try {
      const res = await api("/auth/password", { method: "POST", body: { password: $("#password").value, next } });
      location.href = res.next;
    } catch (err) { msg.textContent = err.message === "wrong password" ? "Wrong password." : err.message; }
    btn.disabled = false;
  });
}

function initLogout() {
  const btn = $("#logout-btn");
  if (!btn) return;
  btn.addEventListener("click", async () => {
    try { await api("/auth/logout", { method: "POST" }); } catch (_) { /* leave anyway */ }
    location.href = "/login";
  });
}

// ---- boot ------------------------------------------------------------------------------

(async () => {
  if (document.body.dataset.page === "login") { pageLogin(); return; }
  initTheme();
  initLogout();
  initSidebar();
  initSearch();
  refreshQueueBadge();
  await initUsers();
  const pages = { home: pageHome, upnext: pageUpnext, recap: pageRecap, characters: pageCharacters, library: pageLibrary,
    activity: pageActivity, system: pageSystem, settings: pageSettings };
  const fn = pages[document.body.dataset.page];
  if (fn) await fn();
})();
