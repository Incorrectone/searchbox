// SearchBox UI. Plain JavaScript, no framework.
//
// Safety rule for this whole file: text from the API (titles, snippets, AI
// answers) is ALWAYS inserted with textContent / createTextNode, never
// innerHTML. Crawled pages are untrusted, so this is what stops a blog post
// from injecting HTML or script into our page. The Content-Security-Policy
// header is a second layer.
"use strict";

const $ = (id) => document.getElementById(id);
const HL_START = "\u0002", HL_END = "\u0003"; // markers around matched words (see SearchRepository.cs)

const state = { q: "", tab: "web", page: 1 };

// ---------- small DOM helpers ----------
function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else node.setAttribute(k, v);
  }
  for (const c of children) node.append(c);
  return node;
}

function safeHref(url) {
  // Only http(s) links, so a stored "javascript:" URL can never become clickable.
  try {
    const u = new URL(url);
    return u.protocol === "http:" || u.protocol === "https:" ? u.href : "#";
  } catch { return "#"; }
}

function showMessage(text, isError = false) {
  const m = $("message");
  m.textContent = text;
  m.classList.toggle("is-error", isError);
  m.classList.remove("is-hidden");
}
function hideMessage() { $("message").classList.add("is-hidden"); }

function fmtDate(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return isNaN(d) ? "" : d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

function fmtNumber(n) { return Number(n || 0).toLocaleString(); }

async function getJson(url) {
  const res = await fetch(url, { headers: { Accept: "application/json" } });
  let body = null;
  try { body = await res.json(); } catch { /* not JSON */ }
  return { ok: res.ok, status: res.status, body, retryAfter: res.headers.get("Retry-After") };
}

// ---------- URL state (so results pages can be bookmarked and Back works) ----------
function readUrl() {
  const p = new URLSearchParams(location.search);
  state.q = (p.get("q") || "").slice(0, 300);
  state.tab = p.get("tab") === "images" ? "images" : "web";
  state.page = Math.min(Math.max(parseInt(p.get("page") || "1", 10) || 1, 1), 10);
}
function writeUrl(push) {
  const p = new URLSearchParams();
  if (state.q) p.set("q", state.q);
  if (state.tab !== "web") p.set("tab", state.tab);
  if (state.page > 1) p.set("page", String(state.page));
  const url = "/" + (p.toString() ? "?" + p : "");
  (push ? history.pushState : history.replaceState).call(history, null, "", url);
}

// ---------- stats gadget + status bar ----------
async function loadStats() {
  const r = await getJson("/api/v1/stats").catch(() => null);
  if (!r || !r.ok || !r.body) {
    $("index-status").textContent = "Index status unavailable";
    $("index-status").classList.add("is-down");
    return;
  }
  const s = r.body;
  $("st-docs").textContent = fmtNumber(s.doc_count);
  $("st-domains").textContent = fmtNumber(s.domain_count);
  $("st-images").textContent = fmtNumber(s.image_count);
  $("st-crawl").textContent = s.last_crawl_at ? fmtDate(s.last_crawl_at) : "not yet";
  $("st-version").textContent = String(s.index_version);
  $("st-model").textContent = s.embedding_model;
  $("build").textContent = `Index build v${s.index_version}`;
  $("index-status").textContent = `Index online: ${fmtNumber(s.doc_count)} posts from ${fmtNumber(s.domain_count)} blogs`;
  $("ai-models").textContent = s.summary_enabled
    ? "Models: " + s.summary_models.join(", then ")
    : "AI summary is switched off on this server.";
  $("ai-btn").disabled = !s.summary_enabled;
  if (s.dashboard_url) {
    // The report is framed on our own page, so visitors stay on the site.
    $("dash-link").href = "/security.html";
    $("dash-link").removeAttribute("target");
    $("dash-link-wrap").classList.remove("is-hidden");
  }
}

// ---------- search ----------
function setMode(hasQuery) {
  $("hero").classList.toggle("is-home", !hasQuery);
  $("layout").classList.toggle("is-home", !hasQuery);
  $("resultsbar").classList.toggle("is-hidden", !hasQuery);
  document.querySelectorAll(".tab[data-tab]").forEach((t) =>
    t.classList.toggle("is-active", t.dataset.tab === state.tab));
  document.querySelectorAll('input[name="tab"]').forEach((r) => { r.checked = r.value === state.tab; });
}

function clearResults() {
  $("results").replaceChildren();
  $("imagegrid").replaceChildren();
  $("pager").replaceChildren();
}

async function runSearch() {
  $("q").value = state.q;
  $("q2").value = state.q;
  setMode(!!state.q);
  clearResults();
  hideMessage();
  if (!state.q) { document.title = "SearchBox: the independent web, searched"; return; }
  document.title = `${state.q} - SearchBox`;

  const endpoint = state.tab === "images" ? "/api/v1/images" : "/api/v1/search";
  const params = new URLSearchParams({ q: state.q, page: String(state.page) });
  $("result-count").textContent = "Searching…";
  $("result-meta").textContent = "";

  const r = await getJson(`${endpoint}?${params}`).catch(() => null);
  if (!r) return showMessage("Could not reach the server. Please try again.", true);
  if (r.status === 429) {
    $("result-count").textContent = "";
    return showMessage(`Whoa, that's a lot of searches. Please wait ${r.retryAfter || "a few"} seconds and try again (rate limit).`, true);
  }
  if (!r.ok) {
    $("result-count").textContent = "";
    return showMessage(r.body?.error ? `Error: ${r.body.error}` : `The search node returned an error (${r.status}).`, true);
  }

  const d = r.body;
  const first = (d.page - 1) * d.page_size + 1;
  const last = first + d.results.length - 1;
  const countText = d.results.length
    ? [`Results `, el("b", { text: `${first} - ${last}` }), ` of `, el("b", { text: fmtNumber(d.total_candidates) }),
       ` candidates for `, el("b", { text: `"${d.query}"` }), ` (${(d.took_ms / 1000).toFixed(2)} seconds)`]
    : [`No results for `, el("b", { text: `"${d.query}"` })];
  $("result-count").replaceChildren(...countText);
  $("result-meta").textContent = d.cached ? "served from cache" : "fresh from the index";

  if (!d.results.length) {
    showMessage("No posts matched. Try different words, or fewer of them. The index only contains personal blogs.");
    return;
  }
  if (state.tab === "images") renderImages(d.results);
  else renderResults(d.results);
  renderPager(d.total_candidates, d.page_size);
}

function snippetNodes(snippet) {
  // "...the \u0002rust\u0003 compiler..." -> text, <b>rust</b>, text
  const out = [];
  let bold = false;
  for (const part of snippet.split(/([\u0002\u0003])/)) {
    if (part === HL_START) { bold = true; continue; }
    if (part === HL_END) { bold = false; continue; }
    if (part) out.push(bold ? el("b", { text: part }) : document.createTextNode(part));
  }
  return out;
}

function displayUrl(url) {
  try {
    const u = new URL(url);
    const s = u.host + u.pathname;
    return s.length > 80 ? s.slice(0, 77) + "…" : s;
  } catch { return url; }
}

function renderResults(results) {
  const list = $("results");
  for (const r of results) {
    const href = safeHref(r.url);
    const text = el("div", { class: "result-text" },
      el("h3", {}, el("a", { href, rel: "noopener", text: r.title })),
      el("div", { class: "url", text: displayUrl(r.url) }),
      el("p", { class: "snippet" },
        ...(r.published_at ? [el("span", { class: "date", text: fmtDate(r.published_at) + " ... " })] : []),
        ...snippetNodes(r.snippet || "")));

    const why = el("div", { class: "why" });
    if (r.match.semantic_rank) why.append(el("span", { class: "tag tag-s", title: "found by meaning", text: "S" }), ` meaning #${r.match.semantic_rank}`);
    if (r.match.keyword_rank) why.append(el("span", { class: "tag tag-k", title: "found by keywords", text: "K" }), ` keywords #${r.match.keyword_rank}`);
    why.append(` · score ${r.match.score.toFixed(4)}`);
    text.append(why);

    const item = el("li", { class: "result" }, text);
    if (r.thumb) {
      const img = el("img", { class: "thumb", src: r.thumb.url, alt: r.thumb.alt || "", loading: "lazy", width: "96", height: "72" });
      img.addEventListener("error", () => img.remove());
      item.append(el("a", { href, rel: "noopener", tabindex: "-1", "aria-hidden": "true" }, img));
    }
    list.append(item);
  }
}

function renderImages(results) {
  const grid = $("imagegrid");
  for (const r of results) {
    if (!r.thumb) continue;
    const href = safeHref(r.url);
    const img = el("img", { src: r.thumb.url, alt: r.thumb.alt || r.title, loading: "lazy" });
    img.addEventListener("error", () => card.remove());
    const card = el("a", { class: "imgcard", href, rel: "noopener" },
      img, el("div", { class: "t", text: r.title }), el("div", { class: "h", text: r.host }));
    grid.append(card);
  }
}

function renderPager(total, pageSize) {
  const pages = Math.min(10, Math.ceil(total / pageSize));
  if (pages <= 1) return;
  const pager = $("pager");
  const mark = el("span", { class: "wordmark" }, "Search", el("span", { text: "Box" }));
  pager.append(mark);
  const link = (p, label) => {
    const a = el("a", { href: "#", text: label });
    a.addEventListener("click", (e) => { e.preventDefault(); go({ page: p }); });
    return a;
  };
  if (state.page > 1) pager.append(link(state.page - 1, "<< Prev"), " ");
  for (let p = 1; p <= pages; p++) {
    pager.append(p === state.page ? el("b", { text: String(p) }) : link(p, String(p)), " ");
  }
  if (state.page < pages) pager.append(link(state.page + 1, "Next >>"));
}

// ---------- AI summary ----------
async function summarize() {
  const q = $("q").value.trim();
  if (!q) { $("q").focus(); return; }
  if (q !== state.q || state.tab !== "web") await go({ q, tab: "web", page: 1 });

  const panel = $("ai-panel");
  panel.classList.remove("is-hidden");
  $("ai-sub").textContent = "Reading the top results…";
  $("ai-body").replaceChildren(el("span", { class: "ai-loading", text: "Summarizing" }));
  $("ai-sources").replaceChildren();
  $("ai-btn").disabled = true;

  const r = await getJson(`/api/v1/summary?${new URLSearchParams({ q })}`).catch(() => null);
  $("ai-btn").disabled = false;
  if (!r || !r.ok) {
    const why = r?.status === 429 ? `Too many summaries; wait ${r.retryAfter || "a few"} seconds.`
      : "The AI summary isn't available right now (free model quota may be used up). Search results are unaffected.";
    $("ai-sub").textContent = "";
    $("ai-body").replaceChildren(document.createTextNode(why));
    return;
  }
  renderSummary(r.body);
}

function renderSummary(s) {
  $("ai-sub").textContent = `✓ ${s.model} · ${(s.took_ms / 1000).toFixed(2)} s${s.cached ? " · cached" : ""} · answer built only from the results below`;
  const byN = new Map(s.sources.map((c) => [c.n, c]));
  const body = $("ai-body");
  body.replaceChildren();
  // Split on [n]; only numbers that match a real source become links.
  for (const part of s.answer.split(/(\[\d+\])/)) {
    const m = part.match(/^\[(\d+)\]$/);
    const src = m && byN.get(Number(m[1]));
    if (src) body.append(el("a", { class: "cite", href: safeHref(src.url), rel: "noopener", title: src.title, text: part }));
    else body.append(document.createTextNode(part));
  }
  const sources = $("ai-sources");
  sources.replaceChildren(el("b", { text: "Sources:" }));
  for (const c of s.sources) sources.append(el("a", { href: safeHref(c.url), rel: "noopener", text: `[${c.n}] ${c.title}` }));
}

// ---------- navigation ----------
async function go(changes) {
  Object.assign(state, changes);
  writeUrl(true);
  if (changes.q !== undefined) $("ai-panel").classList.add("is-hidden");
  await runSearch();
  window.scrollTo(0, 0);
}

function onSubmit(input) {
  return (e) => {
    e.preventDefault();
    const q = input.value.trim();
    if (!q) return;
    const tab = document.querySelector('input[name="tab"]:checked')?.value || state.tab;
    go({ q, tab, page: 1 });
  };
}

document.addEventListener("DOMContentLoaded", () => {
  $("searchform").addEventListener("submit", onSubmit($("q")));
  $("footerform").addEventListener("submit", onSubmit($("q2")));
  $("ai-btn").addEventListener("click", summarize);
  $("ai-hide").addEventListener("click", () => $("ai-panel").classList.add("is-hidden"));
  document.querySelectorAll(".tab[data-tab]").forEach((t) => t.addEventListener("click", (e) => {
    e.preventDefault();
    go({ tab: t.dataset.tab, page: 1 });
  }));
  document.querySelectorAll('input[name="tab"]').forEach((r) => r.addEventListener("change", () => {
    if (state.q) go({ tab: r.value, page: 1 });
  }));
  window.addEventListener("popstate", () => { readUrl(); runSearch(); });

  readUrl();
  writeUrl(false);
  loadStats();
  runSearch();
  if (!state.q) $("q").focus();
});
