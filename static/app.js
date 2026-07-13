"use strict";

const $ = (id) => document.getElementById(id);

const state = {
  results: new Map(), // video_id -> {title, filename, markdown}
  activeId: null,
  running: false,
};

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function postJSON(url, body) {
  const resp = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  return resp.json();
}

function cardEl(videoId) {
  return document.querySelector(`.video-card[data-id="${videoId}"]`);
}

function renderCard(video) {
  const div = document.createElement("div");
  div.className = "video-card";
  div.dataset.id = video.id;
  div.innerHTML = `
    <div class="vc-title">${escapeHtml(video.title || video.url)}</div>
    <div class="vc-row">
      <span class="chip waiting">waiting</span>
    </div>
    <div class="vc-error hidden"></div>`;
  div.addEventListener("click", () => {
    if (state.results.has(video.id)) showPreview(video.id);
  });
  return div;
}

function setCardStatus(videoId, status, message) {
  const card = cardEl(videoId);
  if (!card) return;
  const chip = card.querySelector(".chip");
  chip.className = `chip ${status}`;
  chip.innerHTML =
    status === "converting" ? `<span class="spinner"></span>converting` : status;
  card.classList.toggle("done", status === "done");
  const err = card.querySelector(".vc-error");
  if (status === "error" && message) {
    err.textContent = message;
    err.classList.remove("hidden");
  } else {
    err.classList.add("hidden");
  }
}

function setCardTitle(videoId, title) {
  const card = cardEl(videoId);
  if (card) card.querySelector(".vc-title").textContent = title;
}

function escapeHtml(s) {
  const d = document.createElement("div");
  d.textContent = s || "";
  return d.innerHTML;
}

function showPreview(videoId) {
  const r = state.results.get(videoId);
  if (!r) return;
  state.activeId = videoId;
  $("preview").textContent = r.markdown;
  $("previewTitle").textContent = r.filename;
  $("copyBtn").disabled = false;
  $("downloadBtn").disabled = false;
  document
    .querySelectorAll(".video-card")
    .forEach((c) => c.classList.toggle("active", c.dataset.id === videoId));
}

async function startConversion() {
  if (state.running) return;
  const lines = $("urls").value.split("\n").map((s) => s.trim()).filter(Boolean);
  if (!lines.length) return;

  state.running = true;
  $("convertBtn").disabled = true;
  $("globalStatus").textContent = "Expanding URLs…";
  $("expandErrors").classList.add("hidden");

  let expanded;
  try {
    expanded = await postJSON("/api/expand", { urls: lines });
  } catch (e) {
    $("globalStatus").textContent = "Could not reach the server.";
    state.running = false;
    $("convertBtn").disabled = false;
    return;
  }

  if (expanded.errors.length) {
    $("expandErrors").textContent = expanded.errors.join("\n");
    $("expandErrors").classList.remove("hidden");
  }

  const videos = expanded.videos;
  if (!videos.length) {
    $("globalStatus").textContent = "No videos found in the input.";
    state.running = false;
    $("convertBtn").disabled = false;
    return;
  }

  $("workspace").classList.remove("hidden");
  const list = $("videoList");
  const fresh = videos.filter((v) => !cardEl(v.id));
  fresh.forEach((v) => list.appendChild(renderCard(v)));

  let done = 0;
  for (let i = 0; i < videos.length; i++) {
    const v = videos[i];
    if (state.results.has(v.id)) { done++; continue; } // already converted this session
    $("globalStatus").textContent = `Converting ${i + 1} of ${videos.length}…`;
    setCardStatus(v.id, "converting");
    try {
      const r = await postJSON("/api/convert", { url: v.url });
      if (r.ok) {
        state.results.set(v.id, r);
        setCardTitle(v.id, r.title);
        setCardStatus(v.id, "done");
        done++;
        showPreview(v.id);
      } else {
        setCardStatus(v.id, "error", r.error_message);
      }
    } catch (e) {
      setCardStatus(v.id, "error", "Request failed — is the server still running?");
    }
    if (i < videos.length - 1) await sleep(1000);
  }

  $("globalStatus").textContent =
    `Done — ${done} of ${videos.length} converted. Saved to output/ 🐈`;
  $("zipBtn").disabled = state.results.size < 1;
  state.running = false;
  $("convertBtn").disabled = false;
}

function copyMarkdown() {
  const r = state.results.get(state.activeId);
  if (!r) return;
  navigator.clipboard.writeText(r.markdown).then(() => {
    $("copyBtn").textContent = "Copied!";
    setTimeout(() => ($("copyBtn").textContent = "Copy"), 1200);
  });
}

function downloadBlob(blob, filename) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 5000);
}

function downloadOne() {
  const r = state.results.get(state.activeId);
  if (!r) return;
  downloadBlob(new Blob([r.markdown], { type: "text/markdown" }), r.filename);
}

async function downloadAll() {
  const filenames = [...state.results.values()].map((r) => r.filename);
  if (!filenames.length) return;
  const resp = await fetch("/api/download_zip", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ filenames }),
  });
  if (!resp.ok) return;
  downloadBlob(await resp.blob(), "transcripts.zip");
}

$("convertBtn").addEventListener("click", startConversion);
$("copyBtn").addEventListener("click", copyMarkdown);
$("downloadBtn").addEventListener("click", downloadOne);
$("zipBtn").addEventListener("click", downloadAll);
$("urls").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) startConversion();
});
