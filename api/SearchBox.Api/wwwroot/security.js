// Security page: frames the public Looker Studio report, if one is configured
// (SearchBox__DashboardUrl on the server, returned by /api/v1/stats).
"use strict";

// Only a Looker Studio *embed* URL may be framed; anything else is ignored.
const EMBED = /^https:\/\/lookerstudio\.google\.com\/embed\/reporting\/[A-Za-z0-9_-]+(\/page\/[A-Za-z0-9_-]+)?$/;

async function init() {
  const note = document.getElementById("dash-missing");
  const frame = document.getElementById("dash-frame");
  try {
    const r = await fetch("/api/v1/stats", { headers: { Accept: "application/json" } });
    const s = await r.json();
    const url = (s.dashboard_url || "").trim();
    if (EMBED.test(url)) {
      frame.src = url;
      frame.classList.remove("is-hidden");
      note.classList.add("is-hidden");
    } else {
      note.textContent = "The dashboard isn't published yet.";
    }
  } catch {
    note.textContent = "Couldn't load the dashboard settings. The search still works.";
  }
}

init();
