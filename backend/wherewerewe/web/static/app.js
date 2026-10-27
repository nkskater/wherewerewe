"use strict";

// ---- helpers ---------------------------------------------------------------------------

const $ = (sel, root = document) => root.querySelector(sel);

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
    localStorage.setItem(key, value);
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
    const flush = () => {
      if (para.length) { out.push(`<p>${inline(para.join(" "))}</p>`); para = []; }
      if (list.length) { out.push(`<ul>${list.map((i) => `<li>${inline(i)}</li>`).join("")}</ul>`); list = []; }
    };
    for (const line of lines) {
      const h = line.match(/^#{1,6}\s+(.*)$/);
      const li = line.match(/^(?:[-*•]|\d+[.)])\s+(.*)$/);
      if (h) { flush(); out.push(`<h2>${inline(h[1])}</h2>`); }
      else if (li) { if (para.length) { out.push(`<p>${inline(para.join(" "))}</p>`); para = []; } list.push(li[1]); }
      else { if (list.length) flush(); para.push(line); }
    }
    flush();
  }
  return out.join("\n");
}

function plainText(markdown) {
  return String(markdown || "").replace(/^#+\s*/gm, "").replace(/^[-*•]\s+/gm, "").replace(/\*/g, "");
}

function daysAgo(days) {
  if (days === null || days === undefined) return "";
  if (days <= 0) return "today";
  if (days === 1) return "yesterday";
  return `${days} days ago`;
}

function setSeg(seg, value) {
  for (const b of seg.querySelectorAll("button")) {
    b.setAttribute("aria-checked", String(b.dataset.length === value));
  }
}

function getLength() { return store("www.length") || "standard"; }

// ---- viewer picker ---------------------------------------------------------------------

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
    : "<option>No viewers yet</option>";
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

// ---- pages -----------------------------------------------------------------------------

async function pageHome() {
  const seg = $("#length-seg");
  setSeg(seg, getLength());
  seg.addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (b) { store("www.length", b.dataset.length); setSeg(seg, b.dataset.length); }
  });
  const status = $("#home-status");
  const grid = $("#show-grid");
  const uid = currentUserId();
  if (!uid) {
    status.innerHTML = 'No viewers yet. Connect Plex and Tautulli on the <a href="/settings">Settings</a> page, then run a sync from <a href="/indexing">Indexing</a>.';
    return;
  }
  let shows;
  try { shows = await api(`/api/users/${uid}/shows`); }
  catch (err) { status.textContent = `Couldn't load shows: ${err.message}`; return; }
  status.textContent = shows.length ? "" : "Nothing in progress for this viewer yet.";
  status.hidden = !shows.length ? false : true;
  const tpl = $("#show-card");
  for (const s of shows) {
    const card = tpl.content.firstElementChild.cloneNode(true);
    $(".show-title", card).textContent = s.title;
    $(".poster-fallback", card).textContent = (s.title || "?").charAt(0);
    if (s.has_poster) {
      const img = $("img", card);
      img.src = `/api/shows/${s.show_id}/poster`;
      img.addEventListener("error", () => img.removeAttribute("src"));
    }
    $(".show-pos", card).textContent =
      `Watched through ${s.last_watched_episode}` + (s.days_since !== null ? ` · ${daysAgo(s.days_since)}` : "");
    $(".show-next", card).textContent = s.next_episode
      ? `Up next: ${s.next_episode}${s.next_episode_title ? ` · ${s.next_episode_title}` : ""}`
      : "All caught up";
    $(".chars-link", card).href = `/shows/${s.show_id}/characters?user=${uid}`;
    const btn = $(".recap-btn", card);
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      btn.textContent = "Starting…";
      try {
        const recap = await api("/api/recaps", { method: "POST",
          body: { show_id: s.show_id, user_id: uid, length: getLength() } });
        location.href = `/recaps/${recap.id}`;
      } catch (err) {
        btn.disabled = false;
        btn.textContent = "Recap";
        alert(`Couldn't start a recap: ${err.message}`);
      }
    });
    grid.appendChild(card);
  }
}

async function pageRecap() {
  const root = $(".recap");
  const id = root.dataset.recapId;
  const textEl = $("#recap-text");
  const statusEl = $("#recap-status");
  const labels = { queued: "Queued…", generating: "Writing…", verifying: "Checking against your episodes…",
    done: "Ready", failed: "Failed" };
  let recap;
  try { recap = await api(`/api/recaps/${id}`); }
  catch (err) { statusEl.textContent = err.message; return; }

  $("#recap-show").textContent = recap.show_title || "";
  document.title = `${recap.show_title} · Recap`;
  $("#recap-boundary").textContent =
    `Through ${recap.boundary}${recap.boundary_title ? ` · “${recap.boundary_title}”` : ""}`;
  $("#chars-btn").href = `/shows/${recap.show_id}/characters?` +
    (recap.user_id ? `user=${recap.user_id}` : `until=${recap.boundary}`);
  const seg = $("#recap-length");
  setSeg(seg, recap.length);
  seg.addEventListener("click", async (e) => {
    const b = e.target.closest("button");
    if (!b || b.dataset.length === recap.length) return;
    store("www.length", b.dataset.length);
    try {
      const next = await api("/api/recaps", { method: "POST", body: {
        show_id: recap.show_id, user_id: recap.user_id, length: b.dataset.length, until: recap.boundary } });
      location.href = `/recaps/${next.id}`;
    } catch (err) { alert(err.message); }
  });

  const setStatus = (s) => {
    statusEl.textContent = labels[s] || s;
    statusEl.className = `status-pill ${s}`;
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
      err.textContent = r.error || "Recap failed.";
      err.hidden = false;
    }
    const cov = r.coverage;
    if (cov && (cov.missing.length || cov.overview_only.length)) {
      const parts = [];
      if (cov.missing.length) {
        parts.push(`Based on ${cov.summarized} of ${cov.watched} episodes you've watched; ` +
          `not summarized yet: ${cov.missing.slice(0, 12).join(", ")}${cov.missing.length > 12 ? "…" : ""}.`);
      }
      if (cov.overview_only.length) {
        parts.push(`No dialogue was available for ${cov.overview_only.join(", ")}; those use the episode synopsis.`);
      }
      const box = $("#recap-coverage");
      box.textContent = parts.join(" ");
      box.hidden = false;
    }
    const v = r.verification;
    if (v) {
      const box = $("#recap-verification");
      const removed = v.removed || [];
      $("summary", box).textContent = removed.length
        ? `Verification removed ${removed.length} of ${v.checked} sentences`
        : `Verified: all ${v.checked} sentences are supported by your episodes`;
      if (!v.llm_checked) $("summary", box).textContent += " (model check unavailable; name check only)";
      $("ul", box).innerHTML = removed.map((x) =>
        `<li><s>${escapeHtml(x.sentence)}</s><br><span>${escapeHtml(x.reason)}</span></li>`).join("");
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
  es.addEventListener("final", (e) => { es.close(); finish(JSON.parse(e.data)); });
  es.addEventListener("error", (e) => {
    if (e.data) { es.close(); setStatus("failed"); statusEl.textContent = JSON.parse(e.data).error; }
  });
}

function setupListen(text) {
  const btn = $("#listen-btn");
  if (!("speechSynthesis" in window) || !text) return;
  btn.hidden = false;
  btn.addEventListener("click", () => {
    if (speechSynthesis.speaking) { speechSynthesis.cancel(); btn.textContent = "▶ Listen"; return; }
    const u = new SpeechSynthesisUtterance(plainText(text));
    u.rate = 1.0;
    u.onend = () => { btn.textContent = "▶ Listen"; };
    speechSynthesis.speak(u);
    btn.textContent = "■ Stop";
  });
}

async function pageCharacters() {
  const showId = $(".page-head").dataset.showId;
  const params = new URLSearchParams(location.search);
  const until = params.get("until");
  const uid = until ? null : currentUserId();
  const status = $("#chars-status");
  const list = $("#char-list");
  const query = until ? `until=${encodeURIComponent(until)}` : `user=${uid}`;
  try {
    const show = await api(`/api/shows/${showId}`);
    $("#chars-title").textContent = `${show.title}: characters`;
    document.title = `${show.title} · Characters`;
  } catch (_) { /* title is cosmetic */ }
  let data;
  try { data = await api(`/api/shows/${showId}/characters?${query}`); }
  catch (err) { status.textContent = err.message; return; }
  $("#chars-asof").textContent = `As of ${data.as_of}. Nothing from later episodes is shown.`;
  status.textContent = data.characters.length ? "" : "No characters yet. Index this show first.";
  const tpl = $("#char-card");
  const cards = data.characters.map((c) => {
    const card = tpl.content.firstElementChild.cloneNode(true);
    $(".char-name", card).textContent = c.name;
    $(".char-seen", card).textContent = c.first_seen === c.last_seen
      ? `in ${c.first_seen}` : `${c.first_seen}–${c.last_seen}`;
    $(".char-aliases", card).textContent = c.aliases.length ? `Also: ${c.aliases.join(", ")}` : "";
    $(".char-role", card).textContent = c.role || "";
    $(".char-status", card).textContent = c.status ? `Last seen: ${c.status}` : "";
    const hist = $(".char-history", card);
    if (c.history.length) {
      $("ul", hist).innerHTML = c.history.map((h) =>
        `<li><b>${escapeHtml(h.episode)}</b> ${escapeHtml(h.new_info || h.status || "")}</li>`).join("");
    } else hist.remove();
    const ask = $(".ask-btn", card);
    const answer = $(".char-answer", card);
    ask.addEventListener("click", async () => {
      ask.disabled = true;
      answer.hidden = false;
      answer.textContent = "Thinking…";
      try {
        const body = { character_id: c.character_id };
        if (until) body.until = until; else body.user = uid;
        const r = await api(`/api/shows/${showId}/whois`, { method: "POST", body });
        answer.textContent = r.answer;
      } catch (err) { answer.textContent = err.message; }
      ask.disabled = false;
    });
    card.dataset.search = [c.name, ...c.aliases].join(" ").toLowerCase();
    list.appendChild(card);
    return card;
  });
  $("#chars-search").addEventListener("input", (e) => {
    const q = e.target.value.trim().toLowerCase();
    for (const card of cards) card.hidden = q && !card.dataset.search.includes(q);
  });
}

async function pageIndexing() {
  const tbody = $("#show-table tbody");
  const watchedOnly = $("#watched-only");
  const loadShows = async () => {
    let shows;
    try { shows = await api(`/api/shows?watched_only=${watchedOnly.checked}`); }
    catch (err) { $("#shows-status").textContent = err.message; return; }
    $("#shows-status").textContent = shows.length ? "" : "No shows yet. Run a sync, or import a folder with the CLI.";
    const bar = (n, d) => `<div class="bar"><span style="width:${d ? Math.round((100 * n) / d) : 0}%"></span></div>`;
    tbody.innerHTML = shows.map((s) => `
      <tr>
        <td>${escapeHtml(s.title)}${s.year ? ` <span class="muted">(${s.year})</span>` : ""}</td>
        <td>${s.stats.with_source}/${s.stats.episodes}${bar(s.stats.with_source, s.stats.episodes)}</td>
        <td>${s.stats.summarized}/${s.stats.episodes}${bar(s.stats.summarized, s.stats.episodes)}</td>
        <td>${s.stats.rollups}/${s.stats.seasons}</td>
        <td><button class="btn ghost" data-index="${s.id}" ${s.stats.summarized >= s.stats.episodes && s.stats.episodes ? "disabled" : ""}>Index the rest</button></td>
      </tr>`).join("");
  };
  tbody.addEventListener("click", async (e) => {
    const b = e.target.closest("button[data-index]");
    if (!b) return;
    b.disabled = true;
    try { await api(`/api/shows/${b.dataset.index}/index`, { method: "POST", body: { scope: "all" } }); b.textContent = "Queued"; loadJobs(); }
    catch (err) { alert(err.message); b.disabled = false; }
  });
  watchedOnly.addEventListener("change", loadShows);
  $("#sync-btn").addEventListener("click", async (e) => {
    e.target.disabled = true;
    try { await api("/api/sync", { method: "POST", body: {} }); loadJobs(); }
    catch (err) { alert(err.message); }
    e.target.disabled = false;
  });

  const jobBody = $("#job-table tbody");
  const loadJobs = async () => {
    let data;
    try { data = await api("/api/jobs?limit=40"); } catch (_) { return; }
    $("#job-counts").textContent = Object.entries(data.counts).map(([k, v]) => `${v} ${k}`).join(" · ");
    jobBody.innerHTML = data.jobs.map((j) => `
      <tr>
        <td>${j.id}</td>
        <td>${escapeHtml(j.type)}<div class="muted small">${escapeHtml(JSON.stringify(j.payload))}</div></td>
        <td class="st-${j.status}">${j.status}${j.attempts > 1 ? ` <span class="muted">(try ${j.attempts})</span>` : ""}</td>
        <td><div class="job-detail">${escapeHtml(j.error ? j.error.split("\n")[0] : (j.message || ""))}</div></td>
        <td>${j.status === "failed" ? `<button class="btn ghost" data-retry="${j.id}">Retry</button>` : ""}</td>
      </tr>`).join("");
  };
  jobBody.addEventListener("click", async (e) => {
    const b = e.target.closest("button[data-retry]");
    if (!b) return;
    try { await api(`/api/jobs/${b.dataset.retry}/retry`, { method: "POST" }); loadJobs(); }
    catch (err) { alert(err.message); }
  });
  await Promise.all([loadShows(), loadJobs()]);
  setInterval(loadJobs, 3000);
  setInterval(loadShows, 15000);
}

async function pageSettings() {
  $("#webhook-url").textContent = `${location.origin}/webhooks/tautulli`;
  api("/api/health").then((h) => {
    $("#health").innerHTML = Object.entries(h.checks).map(([name, c]) =>
      `<li><span class="dot ${c.ok ? "ok" : ""}"></span><span><b>${escapeHtml(name)}</b> ${escapeHtml(c.detail)}</span></li>`).join("");
  }).catch((err) => { $("#health").innerHTML = `<li>${escapeHtml(err.message)}</li>`; });

  const groupsEl = $("#settings-groups");
  let original = {};
  const render = (data) => {
    original = {};
    groupsEl.innerHTML = data.groups.map((g) => `
      <fieldset><legend>${escapeHtml(g.name)}</legend>
      ${g.fields.map((f) => {
        original[f.key] = f.value === null ? "" : String(f.value);
        const id = `set-${f.key}`;
        const label = `<label for="${id}">${escapeHtml(f.key.toUpperCase())}${f.overridden ? '<span class="tag">saved here</span>' : ""}</label>`;
        let input;
        if (f.type === "bool") {
          input = `<select id="${id}" name="${f.key}"><option value="true">true</option><option value="false">false</option></select>`;
        } else if (f.key === "llm_api") {
          input = `<select id="${id}" name="${f.key}">${["auto", "openai", "ollama"].map((o) => `<option>${o}</option>`).join("")}</select>`;
        } else {
          const type = f.secret ? "password" : (f.type === "int" || f.type === "float") ? "number" : "text";
          const extra = f.type === "float" ? ' step="any"' : "";
          const list = (f.key === "llm_model" || f.key === "verify_model") ? ' list="model-list"' : "";
          input = `<input id="${id}" name="${f.key}" type="${type}"${extra}${list} autocomplete="off">`;
        }
        return `<div class="field">${label}${input}</div>`;
      }).join("")}
      </fieldset>`).join("") + '<datalist id="model-list"></datalist>';
    for (const [key, value] of Object.entries(original)) {
      const el = groupsEl.querySelector(`[name="${key}"]`);
      if (el) el.value = value;
    }
    api("/api/llm/models").then((m) => {
      $("#model-list").innerHTML = m.models.map((x) => `<option value="${escapeHtml(x)}">`).join("");
    }).catch(() => {});
  };
  try { render(await api("/api/settings")); }
  catch (err) { groupsEl.textContent = err.message; }

  $("#settings-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const changes = {};
    for (const el of groupsEl.querySelectorAll("[name]")) {
      if (el.value !== original[el.name]) changes[el.name] = el.value === "" ? null : el.value;
    }
    const msg = $("#settings-msg");
    if (!Object.keys(changes).length) { msg.textContent = "Nothing changed."; return; }
    try {
      render(await api("/api/settings", { method: "PUT", body: changes }));
      msg.textContent = `Saved ${Object.keys(changes).length} setting(s).`;
    } catch (err) { msg.textContent = `Not saved: ${err.message}`; }
  });

  const usersEl = $("#user-settings");
  if (!users.length) { usersEl.innerHTML = '<p class="muted">No viewers yet.</p>'; return; }
  usersEl.innerHTML = users.map((u) => `
    <div class="user-row" data-user="${u.id}">
      <b>${escapeHtml(u.display_name)}</b>
      <div class="inline">
        <input type="url" name="notify_target" placeholder="ntfy topic or Discord webhook URL" value="${escapeHtml(u.notify_target || "")}">
        <input type="number" name="gap_threshold_days" min="0" placeholder="Gap days" title="Days away before a recap is pushed" value="${u.gap_threshold_days ?? ""}">
        <label class="check"><input type="checkbox" name="notifications_enabled" ${u.notifications_enabled ? "checked" : ""}> Notify</label>
        <button class="btn ghost" type="button">Save</button>
      </div>
      <span class="muted small">Recap pushed after ${u.effective_gap_days} days away</span>
    </div>`).join("");
  usersEl.addEventListener("click", async (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    const row = b.closest(".user-row");
    const gap = $("[name=gap_threshold_days]", row).value;
    try {
      const u = await api(`/api/users/${row.dataset.user}`, { method: "PUT", body: {
        notify_target: $("[name=notify_target]", row).value,
        gap_threshold_days: gap === "" ? null : Number(gap),
        notifications_enabled: $("[name=notifications_enabled]", row).checked } });
      $(".small", row).textContent = `Saved. Recap pushed after ${u.effective_gap_days} days away`;
    } catch (err) { $(".small", row).textContent = `Not saved: ${err.message}`; }
  });
}

// ---- boot ------------------------------------------------------------------------------

(async () => {
  await initUsers();
  const pages = { home: pageHome, recap: pageRecap, characters: pageCharacters,
    indexing: pageIndexing, settings: pageSettings };
  const fn = pages[document.body.dataset.page];
  if (fn) await fn();
})();
