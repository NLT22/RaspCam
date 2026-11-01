"use strict";
// Recordings page: one player, one timeline (24 h or the current hour), and a
// list of motion activity. 1-minute segments play back as one continuous day.
//
// Two <video> elements take turns: one plays while the other preloads the
// next segment, so playback crosses segment boundaries without a pause.

const $ = id => document.getElementById(id);
const DAY = 86400, HOUR = 3600;
const GAP = 5;        // seconds; shorter recording gaps count as continuous
const MERGE = 60;     // motion events closer than this form one activity
const LEAD = 3;       // start playback this many seconds before an activity
const RATES = [1, 2, 4, 8, 16];

const ICON_PLAY = '<svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>';
const ICON_PAUSE = '<svg viewBox="0 0 24 24"><path d="M6 5h4v14H6zm8 0h4v14h-4z"/></svg>';

const st = {
  days: [],          // newest first
  day: null,         // "YYYY-MM-DD"
  dayStart: 0,       // epoch seconds of local midnight
  segs: [],          // {name, start, end, duration, size}, oldest first
  ranges: [],        // merged recorded spans [{start, end}]
  events: [],        // motion events {id, start, end|null, peak}, oldest first
  groups: [],        // activities: merged events, oldest first
  motionEnabled: false,
  cur: -1,           // index of the segment on the front player
  rate: 1,
  wantPlay: false,
  zoom: "day",       // "day" | "hour"
  view: [0, DAY],    // [from, length] currently drawn on the track
};
let front = $("va"), back = $("vb");

// ---------- formatting ----------
const pad = n => String(n).padStart(2, "0");
const at = ts => new Date(ts * 1000);
const hms = ts => { const d = at(ts); return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`; };
const hm = ts => hms(ts).slice(0, 5);
const dmy = ts => { const d = at(ts); return `${pad(d.getDate())}/${pad(d.getMonth() + 1)}/${d.getFullYear()}`; };
const isoDay = d => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const weekday = new Intl.DateTimeFormat("vi-VN", {weekday: "long"});
const midnight = day => { const [y, m, d] = day.split("-").map(Number); return new Date(y, m - 1, d).getTime() / 1000; };
const thumbUrl = seg => "/thumbs/" + seg.name.replace(/\.mp4$/, ".jpg");
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
const nowTs = () => Date.now() / 1000;

function dayTitle(day) {
  const today = new Date();
  const yesterday = new Date(today.getFullYear(), today.getMonth(), today.getDate() - 1);
  const d = at(midnight(day));
  const short = `${pad(d.getDate())}/${pad(d.getMonth() + 1)}`;
  if (day === isoDay(today)) return `Hôm nay, ${short}`;
  if (day === isoDay(yesterday)) return `Hôm qua, ${short}`;
  const w = weekday.format(d);
  return `${w[0].toUpperCase()}${w.slice(1)}, ${short}`;
}

function duration(secs) {
  secs = Math.max(1, Math.round(secs));
  if (secs < 60) return `${secs} giây`;
  const m = Math.floor(secs / 60), s = secs % 60;
  return s ? `${m} phút ${s} giây` : `${m} phút`;
}

async function api(url) {
  const r = await fetch(url);
  if (r.status === 401) { location.href = "/login"; throw new Error("not logged in"); }
  if (!r.ok) throw new Error(`${url}: ${r.status}`);
  return r.json();
}

let toastTimer = 0;
function toast(text) {
  const el = $("toast");
  el.textContent = text;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 3500);
}

// ---------- data ----------
function mergeRanges(segs) {
  const out = [];
  for (const s of segs) {
    const last = out[out.length - 1];
    if (last && s.start - last.end <= GAP) last.end = Math.max(last.end, s.end);
    else out.push({start: s.start, end: s.end});
  }
  return out;
}

// Motion events close together are one "activity" (someone walking around).
function groupEvents(events) {
  const groups = [];
  for (const e of events) {
    const end = e.end ?? nowTs();
    const g = groups[groups.length - 1];
    if (g && e.start - g.end <= MERGE) {
      g.end = Math.max(g.end, end);
      g.open = g.open || e.end === null;
      if (e.peak > g.best.peak) g.best = e;
    } else {
      groups.push({start: e.start, end, open: e.end === null, best: e});
    }
  }
  return groups;
}

// Segment containing ts ({i, exact: true}), else the first one after it.
function locate(ts) {
  const s = st.segs;
  let lo = 0, hi = s.length - 1, found = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (s[mid].start <= ts) { found = mid; lo = mid + 1; } else hi = mid - 1;
  }
  if (found >= 0 && ts < s[found].end) return {i: found, exact: true};
  return {i: found + 1 < s.length ? found + 1 : -1, exact: false};
}

function now() {
  if (st.cur < 0) return st.dayStart;
  return st.segs[st.cur].start + (front.currentTime || 0);
}

async function fetchDay(day) {
  const q = "?day=" + encodeURIComponent(day);
  return Promise.all([api("/api/segments" + q), api("/api/events" + q)]);
}

async function loadDay(day, startAt) {
  st.day = day;
  st.dayStart = midnight(day);
  [st.segs, st.events] = await fetchDay(day);
  st.ranges = mergeRanges(st.segs);
  st.groups = groupEvents(st.events);
  st.cur = -1;
  back.removeAttribute("src");
  back.dataset.name = "";
  drawSide();
  drawTrack();
  if (!st.segs.length) { render(); return; }
  if (startAt == null) {
    // open on the latest activity that is already on disk, else the last few minutes
    const latest = [...st.groups].reverse().find(g => g.start < st.segs.at(-1).end);
    startAt = latest ? latest.start - LEAD : Math.max(st.segs[0].start, st.segs.at(-1).end - 300);
  }
  seekTo(startAt, {quiet: true});
}

// Pick up segments and events recorded since the page loaded.
async function refreshDay() {
  if (!st.day) return;
  const curName = st.cur >= 0 ? st.segs[st.cur].name : null;
  const [segs, events] = await fetchDay(st.day);
  const eventsChanged = JSON.stringify(events) !== JSON.stringify(st.events);
  const segsChanged = segs.length !== st.segs.length || segs.at(-1)?.name !== st.segs.at(-1)?.name;
  if (!eventsChanged && !segsChanged) return;
  st.events = events;
  st.groups = groupEvents(events);
  if (segsChanged) {
    st.segs = segs;
    st.ranges = mergeRanges(segs);
    st.cur = curName ? segs.findIndex(s => s.name === curName) : -1;
    prepareNext();
  }
  drawSide();
  drawTrack();
  render();
}

// ---------- playback ----------
function load(video, i) {
  video.dataset.name = st.segs[i].name;
  video.src = "/media/" + st.segs[i].name;
  video.defaultPlaybackRate = video.playbackRate = st.rate;
  video.load();
}

function prepareNext() {
  const n = st.cur + 1;
  if (n > 0 && n < st.segs.length && back.dataset.name !== st.segs[n].name) load(back, n);
}

function show(i, offset) {
  const name = st.segs[i].name;
  if (front.dataset.name !== name) {
    if (back.dataset.name === name) {
      front.pause();
      [front, back] = [back, front];
      front.hidden = false;
      back.hidden = true;
    } else {
      load(front, i);
    }
  }
  st.cur = i;
  const apply = () => {
    front.playbackRate = st.rate;
    if (Math.abs(front.currentTime - offset) > 0.25) front.currentTime = offset;
    if (st.wantPlay) front.play().catch(() => { st.wantPlay = false; render(); });
    else front.pause();
  };
  if (front.readyState >= 1) apply(); else front.addEventListener("loadedmetadata", apply, {once: true});
  prepareNext();
  render();
}

function seekTo(ts, {quiet = false} = {}) {
  if (!st.segs.length) return;
  ts = clamp(ts, st.dayStart, st.dayStart + DAY - 1);
  const {i, exact} = locate(ts);
  if (i < 0) {
    const last = st.segs.length - 1;
    if (!quiet) toast(`Chưa có bản ghi sau ${hm(st.segs[last].end)}.`);
    show(last, Math.max(0, st.segs[last].duration - 1));
    return;
  }
  if (!exact && !quiet) toast(`Không có bản ghi lúc ${hm(ts)}. Chuyển tới ${hm(st.segs[i].start)}.`);
  show(i, exact ? ts - st.segs[i].start : 0);
}

function onEnded(e) {
  if (e.target !== front) return;
  const next = st.cur + 1;
  if (next < st.segs.length) {
    if (st.segs[next].start - st.segs[st.cur].end > GAP) {
      toast(`Bỏ qua khoảng không ghi ${hm(st.segs[st.cur].end)}–${hm(st.segs[next].start)}.`);
    }
    show(next, 0);
    return;
  }
  st.wantPlay = false;
  render();
  // today keeps growing: continue once the next minute has been saved
  if (st.day === st.days[0]) setTimeout(async () => {
    const before = st.segs.length;
    await refreshDay();
    if (st.segs.length > before) { st.wantPlay = true; show(before, 0); }
  }, 5000);
}

function onError(e) {
  if (e.target !== front || st.cur < 0) return;
  toast(`Không phát được đoạn ${hm(st.segs[st.cur].start)}. Chuyển sang đoạn sau.`);
  if (st.cur + 1 < st.segs.length) show(st.cur + 1, 0);
}

for (const v of [$("va"), $("vb")]) {
  v.addEventListener("ended", onEnded);
  v.addEventListener("error", onError);
  v.addEventListener("timeupdate", e => { if (e.target === front) render(); });
  v.addEventListener("play", e => { if (e.target === front) render(); });
  v.addEventListener("pause", e => { if (e.target === front) render(); });
  v.addEventListener("seeked", e => { if (e.target === front) { render(); saveHash(); } });
}

function play(on) {
  if (st.cur < 0) return;
  st.wantPlay = on;
  if (on) front.play().catch(() => { st.wantPlay = false; render(); });
  else front.pause();
  render();
}

function cycleRate() {
  st.rate = RATES[(RATES.indexOf(st.rate) + 1) % RATES.length];
  for (const v of [front, back]) v.defaultPlaybackRate = v.playbackRate = st.rate;
  render();
}

function playActivity(g) {
  st.wantPlay = true;
  seekTo(g.start - LEAD);
}

function jumpActivity(dir) {
  const t = now();
  // like a music player's "previous": restart the current activity if we are
  // more than a few seconds into it, otherwise go to the one before
  const g = dir < 0 ? [...st.groups].reverse().find(g => g.start - LEAD < t - 5)
                    : st.groups.find(g => g.start - LEAD > t + 1);
  if (g) playActivity(g);
  else toast(dir < 0 ? "Không có chuyển động nào trước đó trong ngày." : "Không có chuyển động nào sau đó trong ngày.");
}

// ---------- drawing ----------
const pct = (t, from, length) => `${clamp((t - from) / length, 0, 1) * 100}%`;

function bar(cls, start, end, [from, length]) {
  const el = document.createElement("div");
  el.className = cls;
  el.style.left = pct(start, from, length);
  el.style.width = `calc(${pct(end, from, length)} - ${pct(start, from, length)})`;
  return el;
}

function currentView() {
  if (st.zoom === "day") return [st.dayStart, DAY];
  const hour = clamp(Math.floor((now() - st.dayStart) / HOUR), 0, 23);
  return [st.dayStart + hour * HOUR, HOUR];
}

// Recorded ranges underneath, motion on top, playhead above both.
function drawTrack() {
  const track = $("track");
  const view = st.view = currentView();
  const [from, length] = view;
  track.querySelectorAll(".range, .event").forEach(el => el.remove());
  let playhead = track.querySelector(".playhead");
  if (!playhead) {
    playhead = Object.assign(document.createElement("div"), {className: "playhead"});
    track.append(playhead);
  }
  for (const r of st.ranges) {
    if (r.end > from && r.start < from + length) track.prepend(bar("range", r.start, r.end, view));
  }
  for (const e of st.events) {
    const end = e.end ?? nowTs();
    if (end > from && e.start < from + length) track.insertBefore(bar("event", e.start, end, view), playhead);
  }
  const ticks = $("ticks");
  ticks.textContent = "";
  const marks = st.zoom === "day"
    ? [0, 3, 6, 9, 12, 15, 18, 21, 24].map(h => [h / 24, `${h}h`, h % 6 !== 0])
    : [0, 10, 20, 30, 40, 50, 60].map(m => [m / 60, hm(from + m * 60), m % 30 !== 0]);
  for (const [x, label, minor] of marks) {
    const s = Object.assign(document.createElement("span"), {textContent: label});
    s.style.left = `${x * 100}%`;
    if (minor) s.className = "minor";
    ticks.append(s);
  }
}

function drawSide() {
  const idx = st.days.indexOf(st.day);
  $("day-label").textContent = dayTitle(st.day);
  $("prev-day").disabled = idx < 0 || idx >= st.days.length - 1;
  $("next-day").disabled = idx <= 0;

  const n = st.groups.length;
  $("summary").textContent = !st.motionEnabled && !n ? "Phát hiện chuyển động đang tắt"
    : n ? `${n} lần có chuyển động` : "";
  $("side-empty").hidden = n > 0 || !st.segs.length;
  $("side-empty-text").textContent = st.motionEnabled
    ? `${st.day === st.days[0] ? "Hôm nay" : "Ngày này"} không có chuyển động nào.`
    : "Bật lại bằng recording.mode: both trong config.yaml.";

  const list = $("activity");
  list.textContent = "";
  for (const g of [...st.groups].reverse()) {        // newest first
    const b = document.createElement("button");
    b.className = "act";
    b.dataset.start = g.start;
    b.dataset.end = g.end;
    const img = new Image();
    img.loading = "lazy";
    img.alt = "";
    img.src = `/events/${g.best.id}.jpg`;
    img.onerror = () => img.replaceWith(Object.assign(document.createElement("i"), {className: "blank"}));
    const sameMinute = hm(g.start) === hm(g.end);
    const text = document.createElement("div");
    text.append(
      Object.assign(document.createElement("strong"),
        {textContent: sameMinute ? hm(g.start) : `${hm(g.start)} – ${hm(g.end)}`}),
      Object.assign(document.createElement("span"),
        {textContent: g.open ? "Đang diễn ra" : duration(g.end - g.start)}));
    b.append(img, text);
    b.onclick = () => playActivity(g);
    const li = document.createElement("li");
    li.append(b);
    list.append(li);
  }
}

// Keep the position in the URL so a reload or a shared link opens the same moment.
let lastHashUpdate = 0;
function saveHash() {
  if (st.cur < 0) return;
  lastHashUpdate = Date.now();
  history.replaceState(null, "", `#${st.day}T${hms(now())}`);
}

function render() {
  const t = now();
  const playing = st.cur >= 0 && !front.paused;
  $("osd").textContent = st.cur >= 0 ? `${dmy(t)} ${hms(t)}` : "";
  const playBtn = $("play");
  playBtn.innerHTML = playing ? ICON_PAUSE : ICON_PLAY;
  playBtn.setAttribute("aria-label", playing ? "Dừng" : "Phát");
  $("rate").textContent = `${st.rate}x`;

  if (st.zoom === "hour" && currentView()[0] !== st.view[0]) drawTrack();
  const [from, length] = st.view;
  $("track").querySelector(".playhead").style.left = pct(t, from, length);
  $("track").setAttribute("aria-valuetext", hms(t));

  document.querySelectorAll(".act").forEach(b =>
    // +1 s slack: the video may land a frame before the exact seek target
    b.setAttribute("aria-current", String(t >= +b.dataset.start - LEAD - 1 && t <= +b.dataset.end + LEAD)));

  const dl = $("download");
  dl.hidden = st.cur < 0;
  if (st.cur >= 0) {
    const seg = st.segs[st.cur];
    dl.href = `/media/${seg.name}?download=1`;
    dl.title = `Tải phút này (${hm(seg.start)}–${hm(seg.end)})`;
  }
  if (Date.now() - lastHashUpdate > 1000) saveHash();
}

// ---------- track: hover/drag shows a preview, release seeks ----------
(function attachTrack() {
  const track = $("track");
  const tip = Object.assign(document.createElement("div"), {className: "tip", hidden: true});
  const img = Object.assign(new Image(), {alt: ""});
  const label = document.createElement("div");
  tip.append(img, label);
  track.append(tip);
  let dragging = false;

  const timeAt = e => {
    const box = track.getBoundingClientRect();
    const [from, length] = st.view;
    return from + clamp((e.clientX - box.left) / box.width, 0, 0.99999) * length;
  };
  const preview = e => {
    const t = timeAt(e);
    const {i, exact} = locate(t);
    tip.hidden = false;
    tip.style.left = `${clamp(e.clientX - track.getBoundingClientRect().left, 84, track.clientWidth - 84)}px`;
    label.textContent = exact ? hms(t) : `${hm(t)}: không ghi`;
    img.hidden = !exact;
    if (exact && img.dataset.src !== thumbUrl(st.segs[i])) img.src = img.dataset.src = thumbUrl(st.segs[i]);
  };
  track.addEventListener("pointermove", e => { if (e.pointerType === "mouse" || dragging) preview(e); });
  track.addEventListener("pointerleave", () => { if (!dragging) tip.hidden = true; });
  track.addEventListener("pointerdown", e => {
    dragging = true;
    track.setPointerCapture(e.pointerId);
    preview(e);
  });
  track.addEventListener("pointerup", e => {
    if (!dragging) return;
    dragging = false;
    if (e.pointerType !== "mouse") tip.hidden = true;
    seekTo(timeAt(e));
  });
  track.addEventListener("pointercancel", () => { dragging = false; tip.hidden = true; });
})();

// ---------- controls ----------
$("play").addEventListener("click", () => play(front.paused));
$("rate").addEventListener("click", cycleRate);
document.querySelectorAll("[data-seek]").forEach(b =>
  b.addEventListener("click", () => seekTo(now() + +b.dataset.seek)));
document.querySelectorAll("[data-zoom]").forEach(b => b.addEventListener("click", () => {
  st.zoom = b.dataset.zoom;
  document.querySelectorAll("[data-zoom]").forEach(x => x.setAttribute("aria-pressed", String(x === b)));
  drawTrack();
  render();
}));
$("prev-day").addEventListener("click", () => {
  const i = st.days.indexOf(st.day);
  if (i < st.days.length - 1) loadDay(st.days[i + 1], null);
});
$("next-day").addEventListener("click", () => {
  const i = st.days.indexOf(st.day);
  if (i > 0) loadDay(st.days[i - 1], null);
});
$("from-start").addEventListener("click", () => { st.wantPlay = true; seekTo(st.dayStart); });
document.addEventListener("keydown", e => {
  if (e.target.closest("input, select, textarea") || e.ctrlKey || e.metaKey || e.altKey) return;
  if (e.key === " " && !e.target.closest("button, a")) { e.preventDefault(); play(front.paused); }
  else if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
    e.preventDefault();
    seekTo(now() + (e.shiftKey ? 60 : 10) * (e.key === "ArrowLeft" ? -1 : 1));
  }
  else if (e.key === ",") jumpActivity(-1);
  else if (e.key === ".") jumpActivity(1);
});

// ---------- start ----------
(async function init() {
  const [days, status] = await Promise.all([api("/api/days"), api("/api/status")]);
  st.days = days;
  st.motionEnabled = status.motion?.enabled ?? false;
  if (!st.days.length) { $("empty").hidden = false; return; }
  for (const id of ["controls", "timeline", "side"]) $(id).hidden = false;
  front.hidden = false;
  const m = /^#(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2}):(\d{2})$/.exec(location.hash);
  if (m && st.days.includes(m[1])) {
    await loadDay(m[1], midnight(m[1]) + (+m[2]) * HOUR + (+m[3]) * 60 + (+m[4]));
  } else {
    await loadDay(st.days[0], null);
  }
  setInterval(() => { if (st.day === st.days[0]) refreshDay(); }, 30000);
})();
