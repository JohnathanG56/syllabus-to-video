// Plain JS, no build step. Polls the API every 2 s while work is in progress.
const STEPS = ["script", "narration", "animation code", "sandboxed render", "assembly"];
// index of the step currently in progress for each topic state
const ACTIVE = { queued: 0, scripted: 1, narrated: 2, coded: 3, rendering: 3, repairing: 3, assembled: 4, done: 5 };
const LABEL = {
  queued: "queued", scripted: "script ready", narrated: "narrated", coded: "code written",
  rendering: "rendering", repairing: "self-repairing", assembled: "muxing", done: "done", failed: "failed",
};
const POLL_MS = 2000;

const $ = (sel, root = document) => root.querySelector(sel);
let current = null;
let timer = null;

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch (_) { /* not json */ }
    throw new Error(msg);
  }
  return r.json();
}

// ------------------------------------------------------------------ upload
const fileInput = $("#file");
const dz = $("#dropzone");
fileInput.addEventListener("change", () => {
  $("#dz-label").textContent = fileInput.files[0] ? fileInput.files[0].name : "Drop a PDF or text file";
});
["dragenter", "dragover"].forEach((e) => dz.addEventListener(e, (ev) => { ev.preventDefault(); dz.classList.add("drag"); }));
["dragleave", "drop"].forEach((e) => dz.addEventListener(e, (ev) => { ev.preventDefault(); dz.classList.remove("drag"); }));
dz.addEventListener("drop", (ev) => {
  if (ev.dataTransfer.files.length) {
    fileInput.files = ev.dataTransfer.files;
    fileInput.dispatchEvent(new Event("change"));
  }
});

$("#upload-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const msg = $("#form-msg");
  const fd = new FormData();
  if (fileInput.files[0]) fd.append("file", fileInput.files[0]);
  else if ($("#text").value.trim()) fd.append("text", $("#text").value);
  else { msg.textContent = "Choose a file or paste some text."; msg.className = "form-msg error"; return; }
  $("#submit").disabled = true;
  msg.className = "form-msg"; msg.textContent = "Uploading…";
  try {
    const res = await api("/syllabi", { method: "POST", body: fd });
    msg.textContent = "Queued. Extracting topics…";
    fileInput.value = ""; $("#text").value = ""; $("#dz-label").textContent = "Drop a PDF or text file";
    location.hash = `#/syllabi/${res.id}`;
    loadRecent();
  } catch (e) {
    msg.className = "form-msg error"; msg.textContent = e.message;
  } finally {
    $("#submit").disabled = false;
  }
});

// ------------------------------------------------------------------ recent list
async function loadRecent() {
  try {
    const items = await api("/syllabi");
    const ul = $("#recent");
    ul.innerHTML = "";
    if (!items.length) { ul.innerHTML = '<li class="muted">Nothing yet.</li>'; return; }
    for (const s of items) {
      const li = document.createElement("li");
      const a = document.createElement("a");
      a.href = `#/syllabi/${s.id}`;
      a.className = current === s.id ? "active" : "";
      const t = document.createElement("span");
      t.className = "rtitle";
      t.textContent = s.title || s.filename || `Syllabus #${s.id}`;
      const st = document.createElement("span");
      st.className = "muted";
      st.textContent = s.status;
      a.append(t, st);
      li.append(a);
      ul.append(li);
    }
  } catch (_) { /* API down: keep old list */ }
}

// ------------------------------------------------------------------ syllabus view
function topicCard(t, existing) {
  const el = existing || $("#topic-tpl").content.firstElementChild.cloneNode(true);
  el.dataset.id = t.id;
  $(".num", el).textContent = t.position;
  $(".title", el).textContent = t.title;
  const pill = $(".pill", el);
  pill.textContent = LABEL[t.status] || t.status;
  pill.className = "pill " + (t.status === "done" ? "done" : t.status === "failed" ? "failed"
    : t.status === "repairing" ? "repairing" : t.status === "queued" ? "" : "active");

  const stepper = $(".stepper", el);
  stepper.innerHTML = "";
  const active = ACTIVE[t.status] ?? 0;
  STEPS.forEach((s, i) => {
    const li = document.createElement("li");
    li.title = s;
    if (t.status === "failed") li.className = "bad";
    else if (i < active) li.className = "on";
    else if (i === active) li.className = "now";
    stepper.append(li);
  });

  const media = $(".media", el);
  if (t.video_url) {
    if (!media.querySelector("video")) {
      media.innerHTML = "";
      const v = document.createElement("video");
      v.controls = true; v.preload = "metadata"; v.src = `${t.video_url}#t=4`;  // skip the blank first frame
      if (t.captions_url) {
        const tr = document.createElement("track");
        tr.kind = "captions"; tr.srclang = "en"; tr.label = "English"; tr.src = t.captions_url;
        v.append(tr);
      }
      media.append(v);
    }
  } else {
    const msg = t.status === "failed" ? "No video" : t.status === "repairing"
      ? `Render failed; the model is fixing its code (attempt ${t.attempts + 1})…`
      : `Working: ${LABEL[t.status] || t.status}…`;
    media.innerHTML = `<div class="placeholder"></div>`;
    $(".placeholder", media).textContent = msg;
  }

  const ul = $(".objectives ul", el);
  if (!ul.children.length) for (const o of t.objectives) { const li = document.createElement("li"); li.textContent = o; ul.append(li); }

  const note = $(".note", el);
  if (t.error) { note.textContent = t.error; note.className = "note error"; }
  else if (t.warning) { note.textContent = t.warning; note.className = "note warn"; }
  else if (t.attempts > 1 && t.status === "done") { note.textContent = `Rendered after ${t.attempts - 1} automatic repair(s).`; note.className = "note"; }
  else { note.textContent = ""; note.className = "note"; }
  return el;
}

function render(s) {
  const root = $("#content");
  if (!root.querySelector(".syl-view") || root.dataset.id !== String(s.id)) {
    root.dataset.id = s.id;
    root.innerHTML = `<div class="syl-view">
      <div class="syl-head"><h1 class="syl-title"></h1><span class="progress"></span></div>
      <div class="bar"><span></span></div><div class="banner-slot"></div><div class="topics"></div></div>`;
  }
  $(".syl-title", root).textContent = s.title || s.filename || `Syllabus #${s.id}`;
  const p = s.progress;
  $(".progress", root).textContent = s.status === "ready"
    ? `${p.done}/${p.total} lessons ready${p.failed ? ` · ${p.failed} failed` : ""}`
    : s.status === "failed" ? "failed" : "extracting topics…";
  $(".bar > span", root).style.width = p.total ? `${(100 * (p.done + p.failed)) / p.total}%` : "4%";
  const slot = $(".banner-slot", root);
  slot.innerHTML = "";
  if (s.error) { const b = document.createElement("div"); b.className = "banner error"; b.textContent = s.error; slot.append(b); }
  else if (!s.topics.length) { const b = document.createElement("div"); b.className = "banner"; b.textContent = "Reading the syllabus and ordering topics…"; slot.append(b); }

  const grid = $(".topics", root);
  for (const t of s.topics) {
    const existing = grid.querySelector(`.topic[data-id="${t.id}"]`);
    const card = topicCard(t, existing);
    if (!existing) grid.append(card);
  }
}

async function show(id) {
  current = id;
  clearTimeout(timer);
  try {
    const s = await api(`/syllabi/${id}`);
    render(s);
    if (!s.progress.finished) timer = setTimeout(() => show(id), POLL_MS);
    else loadRecent();
  } catch (e) {
    $("#content").innerHTML = `<div class="banner error"></div>`;
    $("#content .banner").textContent = e.message;
  }
}

function route() {
  const m = location.hash.match(/^#\/syllabi\/(\d+)/);
  if (m) show(Number(m[1]));
  loadRecent();
}
window.addEventListener("hashchange", route);
route();
