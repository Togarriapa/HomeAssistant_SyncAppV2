"use strict";
let csrf = "";
let requested = "";
const byId = id => document.getElementById(id);
const readable = value => (value || "").replaceAll("_", " ");
async function action(name, job) {
  try {
    const response = await fetch("action", {method:"POST",headers:{"Content-Type":"application/json","X-SyncApp-CSRF":csrf},body:JSON.stringify({action:name,...(job?{job}: {})})});
    const result = await response.json();
    if (!response.ok) throw new Error(readable(result.error));
    requested = result.id;
    byId("notice").textContent = "Action queued. SyncApp will process it safely in sequence.";
    await refresh();
  } catch(error) { byId("notice").textContent = `Action could not be queued: ${error.message}`; }
}
async function refresh() {
  try {
    const response = await fetch("status", {cache:"no-store"});
    if (!response.ok) throw new Error("Control panel unavailable");
    const s = await response.json(); csrf = s.csrf;
    byId("repository").textContent = s.repository || "Repo B is not configured yet";
    byId("state").textContent = s.initialized ? "Initialized" : "Setup required";
    const keys = s.keys || {}; const key = keys.pending || keys.active;
    byId("public-key").value = key?.public_key || "";
    byId("fingerprint").textContent = key ? `${keys.pending ? "Pending" : "Active"}: ${key.fingerprint}` : "";
    byId("active-key").textContent = keys.active ? `Active key: ${keys.active.fingerprint}` : "No active deploy key.";
    byId("previous-key").textContent = keys.previous ? `After confirming a successful sync, remove the previous deploy key from GitHub: ${keys.previous.fingerprint}` : "";
    byId("main-sha").textContent = s.main || "—";
    byId("candidate-sha").textContent = s.candidate || "—";
    for (const button of document.querySelectorAll("[data-action]")) {
      const a = button.dataset.action;
      button.disabled = Boolean(s.busy) ||
        (a === "initialize" && (s.initialized || !keys.active || !s.configured)) ||
        (a === "test" && (!key || !s.configured)) ||
        (a === "activate" && (!keys.pending || keys.test?.id !== keys.pending.id)) ||
        (a === "refresh" && !keys.active) || (a === "generate" && Boolean(keys.active));
    }
    if (s.action && (!requested || requested === s.action.id)) {
      byId("notice").textContent = s.action.error ? `Action stopped: ${readable(s.action.error)}. Check the setup or operation details, then retry.` : `Action ${readable(s.action.status)}: ${readable(s.action.name)}.`;
      if (s.action.status !== "running") requested = "";
    } else if (!requested) {
      byId("notice").textContent = s.initialized ? "Repo B is initialized. Review operation results below." : "Automatic sync is blocked until you finish setup and initialize Repo B.";
    }
    byId("jobs").replaceChildren();
    for (const job of (s.jobs || [])) {
      const row = document.createElement("tr");
      for (const value of [readable(job.kind), readable(job.status), `${readable(job.error || job.phase)} · attempt ${job.attempts}`]) {
        const cell = document.createElement("td"); cell.textContent = value; row.append(cell);
      }
      const cell = document.createElement("td");
      if (["blocked","retry"].includes(job.status)) {
        const retry = document.createElement("button"); retry.textContent = "Retry"; retry.className = "secondary";
        retry.disabled = Boolean(s.busy); retry.addEventListener("click", () => action("retry", job.id)); cell.append(retry);
      }
      row.append(cell); byId("jobs").append(row);
    }
  } catch (_) { byId("notice").textContent = "Connection interrupted. Waiting for SyncApp to become available…"; }
}
for (const button of document.querySelectorAll("[data-action]")) button.addEventListener("click", () => action(button.dataset.action));
byId("copy").addEventListener("click", async () => {
  try { await navigator.clipboard.writeText(byId("public-key").value); byId("notice").textContent = "Public key copied."; }
  catch (_) { byId("public-key").select(); byId("notice").textContent = "Select and copy the public key above."; }
});
refresh(); setInterval(refresh, 2500);
