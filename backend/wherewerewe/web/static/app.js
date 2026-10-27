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

function setTabs(group, value) {
  for (const b of group.querySelectorAll("button")) {
    b.setAttribute("aria-checked", String(b.dataset.length === value));
  }
}

function getLength() { return store("www.length") || "standard"; }

// ---- theme -----------------------------------------------------------------------------

const THEMES = ["auto", "dark", "light"];

function applyTheme(theme) {
  const root = document.documentElement;
  if (theme === "auto") delete root.dataset.theme;
  else root.dataset.theme = theme;
  store("www.theme", theme === "auto" ? null : theme);
  const btn = $("#theme-btn");
  btn.textContent = theme === "auto" ? "Auto" : theme === "dark" ? "Dark" : "Light";
  btn.title = `Theme: ${btn.textContent}. Click to change.`;
}

function initTheme() {
  const saved = store("www.theme");
  let current = THEMES.includes(saved) ? saved : "auto";
  applyTheme(current);
  $("#theme-btn").addEventListener("click", () => {
    current = THEMES[(THEMES.indexOf(current) + 1) % THEMES.length];
    applyTheme(current);
  });
}

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
    : "<option>Nobody yet</option>";
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

// ---- watching --------------------------------------------------------------------------

async function pageHome() {
  const tabs = $("#length-seg");
  setTabs(tabs, getLength());
  tabs.addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (b) { store("www.length", b.dataset.length); setTabs(tabs, b.dataset.length); }
  });
  const status = $("#home-status");
  const list = $("#show-grid");
  const uid = currentUserId();
  if (!uid) {
    status.innerHTML = 'No viewers yet. Connect Plex and Tautulli in <a href="/settings">Settings</a>, ' +
      'then sync from the <a href="/indexing">Library</a> page.';
    return;
  }
  const user = users.find((u) => u.id === uid);
  let shows;
  try { shows = await api(`/api/users/${uid}/shows`); }
  catch (err) { status.textContent = `Couldn't load shows: ${err.message}`; return; }
  status.textContent = shows.length ? "" : "Nothing in progress yet. Finish an episode of something and it will show up here.";

  const tpl = $("#show-card");
  for (const s of shows) {
    const row = tpl.content.firstElementChild.cloneNode(true);
    $(".queue-title", row).textContent = s.title;
    $(".poster-letter", row).textContent = (s.title || "?").replace(/^the\s+/i, "").charAt(0);
    if (s.has_poster) {
      const img = $("img", row);
      img.src = `/api/shows/${s.show_id}/poster`;
      img.addEventListener("error", () => img.removeAttribute("src"));
    }
    $(".from", row).textContent = s.last_watched_episode;
    if (s.next_episode) {
      $(".to", row).textContent = s.next_episode;
      $(".next-title", row).textContent = s.next_episode_title || "";
    } else {
      $(".arrow", row).remove();
      $(".to", row).remove();
      $(".next-title", row).textContent = "caught up";
    }
    $(".rail", row).innerHTML = (s.seasons || []).map((x) =>
      `<span style="--n:${x.total};--p:${Math.round((100 * x.watched) / x.total)}%"><i></i></span>`).join("");
    const current = (s.seasons || []).find((x) => x.watched < x.total) || (s.seasons || []).slice(-1)[0];
    $(".queue-count", row).textContent =
      `${s.watched_count} of ${plural(s.episode_count, "episode")}` + (current ? ` · season ${current.season}` : "");

    const away = $(".away", row);
    if (s.days_since === null || s.days_since === undefined) away.remove();
    else if (s.days_since < 1) { $(".away-n", row).textContent = "—"; $(".away-unit", row).textContent = "today"; }
    else {
      $(".away-n", row).textContent = s.days_since;
      $(".away-unit", row).textContent = s.days_since === 1 ? "day away" : "days away";
      if (user && s.days_since >= user.effective_gap_days) away.classList.add("long");
    }

    $(".chars-link", row).href = `/shows/${s.show_id}/characters?user=${uid}`;
    const btn = $(".recap-btn", row);
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
    list.appendChild(row);
  }
}

// ---- recap -----------------------------------------------------------------------------

async function pageRecap() {
  const id = $(".recap").dataset.recapId;
  const textEl = $("#recap-text");
  const statusEl = $("#recap-status");
  const labels = { queued: "In the queue", generating: "Writing", verifying: "Checking it against your episodes",
    done: "Done", failed: "Failed" };
  let recap;
  try { recap = await api(`/api/recaps/${id}`); }
  catch (err) { $(".status-text", statusEl).textContent = err.message; statusEl.className = "status failed"; return; }

  $("#recap-show").textContent = recap.show_title || "";
  document.title = `${recap.show_title} · Previously on`;
  $("#recap-boundary").innerHTML = `Everything through <span class="code">${escapeHtml(recap.boundary)}</span>` +
    (recap.boundary_title ? ` · <em>${escapeHtml(recap.boundary_title)}</em>` : "");
  $("#chars-btn").href = `/shows/${recap.show_id}/characters?` +
    (recap.user_id ? `user=${recap.user_id}` : `until=${recap.boundary}`);
  const tabs = $("#recap-length");
  setTabs(tabs, recap.length);
  tabs.addEventListener("click", async (e) => {
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
    $(".status-text", statusEl).textContent = labels[s] || s;
    statusEl.className = `status ${s === "done" || s === "failed" ? s : "working"}`;
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
        parts.push(`Built from ${cov.summarized} of the ${cov.watched} episodes you've seen. Still waiting on ${shown}.`);
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
        ? `${plural(removed.length, "sentence")} cut for not matching your episodes`
        : "Every sentence was checked against your episodes";
      if (!v.llm_checked) summary += " (name check only; the model check didn't run)";
      $("summary", box).textContent = summary;
      $("ol", box).innerHTML = removed.map((x) =>
        `<li><s>${escapeHtml(x.sentence)}</s>${x.reason ? `<span class="why">${escapeHtml(x.reason)}</span>` : ""}</li>`).join("");
      if (!removed.length) box.removeAttribute("open");
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
  btn.hidden = false;
  btn.addEventListener("click", () => {
    if (speechSynthesis.speaking) { speechSynthesis.cancel(); btn.textContent = "Read aloud"; return; }
    const u = new SpeechSynthesisUtterance(plainText(text));
    u.onend = () => { btn.textContent = "Read aloud"; };
    speechSynthesis.speak(u);
    btn.textContent = "Stop reading";
  });
}

// ---- characters ------------------------------------------------------------------------

async function pageCharacters() {
  const showId = $(".page-title").dataset.showId;
  const until = new URLSearchParams(location.search).get("until");
  const uid = until ? null : currentUserId();
  const status = $("#chars-status");
  const list = $("#char-list");
  if (!until && !uid) { status.textContent = "Pick a viewer first."; return; }
  const query = until ? `until=${encodeURIComponent(until)}` : `user=${uid}`;
  try {
    const show = await api(`/api/shows/${showId}`);
    $("#chars-show").textContent = show.title;
    document.title = `${show.title} · Who's who`;
  } catch (_) { /* the title is cosmetic */ }
  let data;
  try { data = await api(`/api/shows/${showId}/characters?${query}`); }
  catch (err) { status.textContent = err.message; return; }
  $("#chars-asof").innerHTML = `As of <span class="code">${escapeHtml(data.as_of)}</span>. ` +
    "Nothing from later episodes, including names you haven't learned yet.";
  status.textContent = data.characters.length ? "" : "No one here yet. This show hasn't been indexed.";
  const tpl = $("#char-card");
  const rows = data.characters.map((c) => {
    const row = tpl.content.firstElementChild.cloneNode(true);
    $(".person-name", row).textContent = c.name;
    $(".person-seen", row).textContent = c.first_seen === c.last_seen ? c.first_seen : `${c.first_seen}–${c.last_seen}`;
    $(".person-aka", row).textContent = c.aliases.length ? `also ${c.aliases.join(", ")}` : "";
    $(".person-role", row).textContent = c.role || "";
    $(".person-status", row).textContent = c.status ? `Status: ${c.status}` : "";
    const hist = $(".person-history", row);
    if (c.history.length) {
      $("ol", hist).innerHTML = c.history.map((h) =>
        `<li><span class="code">${escapeHtml(h.episode)}</span><span>${escapeHtml(h.new_info || h.status || "")}</span></li>`).join("");
    } else hist.remove();
    const ask = $(".ask-btn", row);
    const answer = $(".person-answer", row);
    ask.addEventListener("click", async () => {
      ask.disabled = true;
      answer.hidden = false;
      answer.textContent = "One moment…";
      try {
        const body = { character_id: c.character_id };
        if (until) body.until = until; else body.user = uid;
        answer.textContent = (await api(`/api/shows/${showId}/whois`, { method: "POST", body })).answer;
      } catch (err) { answer.textContent = err.message; }
      ask.disabled = false;
    });
    row.dataset.search = [c.name, ...c.aliases].join(" ").toLowerCase();
    list.appendChild(row);
    return row;
  });
  $("#chars-search").addEventListener("input", (e) => {
    const q = e.target.value.trim().toLowerCase();
    for (const row of rows) row.hidden = Boolean(q) && !row.dataset.search.includes(q);
  });
}

// ---- library ---------------------------------------------------------------------------

async function pageIndexing() {
  const tbody = $("#show-table tbody");
  const watchedOnly = $("#watched-only");
  const jobBody = $("#job-table tbody");
  const meter = (n, d) =>
    `<span class="meter"><b><i style="width:${d ? Math.round((100 * n) / d) : 0}%"></i></b>${n}/${d}</span>`;

  const loadJobs = async () => {
    let data;
    try { data = await api("/api/jobs?limit=40"); } catch (_) { return; }
    $("#job-counts").textContent = Object.entries(data.counts).map(([k, v]) => `${v} ${k}`).join(" · ");
    jobBody.innerHTML = data.jobs.map((j) => `
      <tr>
        <td class="num">${j.id}</td>
        <td>${escapeHtml(j.type.replace(/_/g, " "))}<span class="job-payload">${escapeHtml(JSON.stringify(j.payload))}</span></td>
        <td><span class="state state-${j.status}">${j.status}</span>${j.attempts > 1 ? ` <span class="job-payload">try ${j.attempts}</span>` : ""}</td>
        <td><div class="job-detail">${escapeHtml(j.error ? j.error.split("\n")[0] : (j.message || ""))}</div></td>
        <td>${j.status === "failed" ? `<button class="btn btn-small" data-retry="${j.id}">Retry</button>` : ""}</td>
      </tr>`).join("");
  };

  const loadShows = async () => {
    let shows;
    try { shows = await api(`/api/shows?watched_only=${watchedOnly.checked}`); }
    catch (err) { $("#shows-status").textContent = err.message; return; }
    $("#shows-status").textContent = shows.length ? "" : "No shows yet. Run a sync, or import a folder from the command line.";
    tbody.innerHTML = shows.map((s) => {
      const done = s.stats.episodes && s.stats.summarized >= s.stats.episodes;
      return `
      <tr>
        <td class="title-cell">${escapeHtml(s.title)}${s.year ? ` <span class="year">${s.year}</span>` : ""}</td>
        <td>${meter(s.stats.with_source, s.stats.episodes)}</td>
        <td>${meter(s.stats.summarized, s.stats.episodes)}</td>
        <td class="num">${s.stats.rollups}/${s.stats.seasons}</td>
        <td>${done ? "" : `<button class="btn btn-small" data-index="${s.id}">Index</button>`}</td>
      </tr>`;
    }).join("");
  };

  tbody.addEventListener("click", async (e) => {
    const b = e.target.closest("button[data-index]");
    if (!b) return;
    b.disabled = true;
    try {
      await api(`/api/shows/${b.dataset.index}/index`, { method: "POST", body: { scope: "all" } });
      b.textContent = "Queued";
      loadJobs();
    } catch (err) { alert(err.message); b.disabled = false; }
  });
  jobBody.addEventListener("click", async (e) => {
    const b = e.target.closest("button[data-retry]");
    if (!b) return;
    try { await api(`/api/jobs/${b.dataset.retry}/retry`, { method: "POST" }); loadJobs(); }
    catch (err) { alert(err.message); }
  });
  watchedOnly.addEventListener("change", loadShows);
  $("#sync-btn").addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    btn.disabled = true;
    try { await api("/api/sync", { method: "POST", body: {} }); btn.textContent = "Sync queued"; loadJobs(); }
    catch (err) { alert(err.message); btn.disabled = false; }
  });
  await Promise.all([loadShows(), loadJobs()]);
  setInterval(loadJobs, 3000);
  setInterval(loadShows, 15000);
}

// ---- settings --------------------------------------------------------------------------

const GROUP_NAMES = { Connections: "Servers", Model: "Language model", Sources: "Dialogue",
  Recaps: "Recaps", Notifications: "Notifications", Schedule: "Schedule" };
const FIELD_LABELS = {
  plex_url: ["Plex server", "e.g. https://192.168.1.100:32400"],
  plex_token: ["Plex token"],
  plex_libraries: ["Libraries", "comma-separated; blank means every TV library"],
  tautulli_url: ["Tautulli", "e.g. http://192.168.1.100:8181"],
  tautulli_api_key: ["Tautulli API key"],
  verify_ssl: ["Check TLS certificates", "turn off for Plex over https by IP"],
  media_path_map: ["Media path mapping", "plex path=path here; separate pairs with ;"],
  tmdb_api_key: ["TMDB API key", "optional synopses"],
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
  sync_interval_minutes: ["Check watch history every", "minutes"],
  library_scan_interval_minutes: ["Rescan the library every", "minutes"],
  backfill_hour: ["Overnight indexing at", "hour, 0–23; -1 turns it off"],
};

async function pageSettings() {
  $("#webhook-url").textContent = `${location.origin}/webhooks/tautulli`;
  api("/api/health").then((h) => {
    $("#health").innerHTML = Object.entries(h.checks).map(([name, c]) =>
      `<li><span class="mark ${c.ok ? "ok" : ""}"></span><span class="name">${escapeHtml(name)}</span>` +
      `<span class="detail">${escapeHtml(c.detail)}</span></li>`).join("");
  }).catch((err) => { $("#health").innerHTML = `<li>${escapeHtml(err.message)}</li>`; });

  const groupsEl = $("#settings-groups");
  let original = {};
  const render = (data) => {
    original = {};
    groupsEl.innerHTML = data.groups.map((g) => `
      <fieldset><legend>${escapeHtml(GROUP_NAMES[g.name] || g.name)}</legend>
      ${g.fields.map((f) => {
        original[f.key] = f.value === null ? "" : String(f.value);
        const id = `set-${f.key}`;
        const [label, hint] = FIELD_LABELS[f.key] || [f.key];
        const env = f.key.toUpperCase();
        const labelHtml = `<label class="field-label" for="${id}">${escapeHtml(label)}` +
          `${f.overridden ? '<span class="saved">saved here</span>' : ""}` +
          `<small>${env}${hint ? ` · ${escapeHtml(hint)}` : ""}</small></label>`;
        let input;
        if (f.type === "bool") {
          input = `<select class="field-input" id="${id}" name="${f.key}"><option value="true">Yes</option><option value="false">No</option></select>`;
        } else if (f.key === "llm_api") {
          input = `<select class="field-input" id="${id}" name="${f.key}">` +
            [["auto", "Detect"], ["ollama", "Ollama"], ["openai", "OpenAI-compatible"]]
              .map(([v, t]) => `<option value="${v}">${t}</option>`).join("") + "</select>";
        } else {
          const type = f.secret ? "password" : (f.type === "int" || f.type === "float") ? "number" : "text";
          const extra = f.type === "float" ? ' step="any"' : "";
          const list = (f.key === "llm_model" || f.key === "verify_model") ? ' list="model-list"' : "";
          input = `<input id="${id}" name="${f.key}" type="${type}"${extra}${list} autocomplete="off" spellcheck="false">`;
        }
        return `<div class="field">${labelHtml}${input}</div>`;
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
    if (!Object.keys(changes).length) { msg.textContent = "Nothing to save."; return; }
    try {
      render(await api("/api/settings", { method: "PUT", body: changes }));
      msg.textContent = `Saved ${plural(Object.keys(changes).length, "change")}.`;
    } catch (err) { msg.textContent = `Not saved: ${err.message}`; }
  });

  const usersEl = $("#user-settings");
  if (!users.length) { usersEl.innerHTML = '<p class="subtle">No viewers yet.</p>'; return; }
  usersEl.innerHTML = users.map((u) => `
    <div class="user-row" data-user="${u.id}">
      <p class="user-name">${escapeHtml(u.display_name)}</p>
      <div class="user-fields">
        <input type="url" name="notify_target" placeholder="ntfy topic or Discord webhook" value="${escapeHtml(u.notify_target || "")}" aria-label="Notification target">
        <input type="number" name="gap_threshold_days" min="0" placeholder="${u.effective_gap_days} days" value="${u.gap_threshold_days ?? ""}" aria-label="Days away before a push">
        <label class="check"><input type="checkbox" name="notifications_enabled" ${u.notifications_enabled ? "checked" : ""}> Push</label>
        <button class="btn btn-small" type="button">Save</button>
      </div>
      <p class="user-note">Gets a recap after ${plural(u.effective_gap_days, "day")} away.</p>
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
      $(".user-note", row).textContent = `Saved. Gets a recap after ${plural(u.effective_gap_days, "day")} away.`;
    } catch (err) { $(".user-note", row).textContent = `Not saved: ${err.message}`; }
  });
}

// ---- boot ------------------------------------------------------------------------------

(async () => {
  initTheme();
  await initUsers();
  const pages = { home: pageHome, recap: pageRecap, characters: pageCharacters,
    indexing: pageIndexing, settings: pageSettings };
  const fn = pages[document.body.dataset.page];
  if (fn) await fn();
})();
