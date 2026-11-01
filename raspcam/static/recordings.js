"use strict";
// Recordings page: plays 1-minute segments as one continuous day, with a
// 24 h track, a 1 h zoom track, and 5-minute thumbnails.
//
// Two <video> elements take turns: one plays while the other preloads the
// next segment, so playback crosses segment boundaries without a pause.

const $ = id => document.getElementById(id);
const DAY = 86400, HOUR = 3600, GAP = 5; // seconds; shorter gaps count as continuous

const ICON_PLAY = '<svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>';
const ICON_PAUSE = '<svg viewBox="0 0 24 24"><path d="M6 5h4v14H6zm8 0h4v14h-4z"/></svg>';

const st = {
  days: [],        // newest first
  day: null,       // "YYYY-MM-DD"
  dayStart: 0,     // epoch seconds of local midnight
  segs: [],        // {name, start, end, duration, size}, oldest first
  ranges: [],      // merged recorded spans [{start, end}]
  cur: -1,         // index of the segment on the front player
  rate: 1,
  wantPlay: false,
  hour: -1,        // hour currently drawn on the hour track
};
let front = $("va"), back = $("vb");

// ---------- formatting ----------
const pad = n => String(n).padStart(2, "0");
const at = ts => new Date(ts * 1000);
const hms = ts => { const d = at(ts); return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`; };
const hm = ts => hms(ts).slice(0, 5);
const dmy = ts => { const d = at(ts); return `${pad(d.getDate())}/${pad(d.getMonth() + 1)}/${d.getFullYear()}`; };
const weekday = new Intl.DateTimeFormat("vi-VN", {weekday: "long"});
const midnight = day => { const [y, m, d] = day.split("-").map(Number); return new Date(y, m - 1, d).getTime() / 1000; };
const thumbUrl = seg => "/thumbs/" + seg.name.replace(/\.mp4$/, ".jpg");
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

function dayTitle(day) {
  const w = weekday.format(at(midnight(day)));
  return `${w[0].toUpperCase()}${w.slice(1)}, ${dmy(midnight(day))}`;
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

async function loadDay(day, startAt) {
  st.day = day;
  st.dayStart = midnight(day);
  st.segs = await api("/api/segments?day=" + encodeURIComponent(day));
  st.ranges = mergeRanges(st.segs);
  st.cur = -1;
  st.hour = -1;
  back.removeAttribute("src");
  back.dataset.name = "";
  drawDay();
  if (!st.segs.length) { render(); return; }
  const last = st.segs[st.segs.length - 1];
  seekTo(startAt ?? Math.max(st.segs[0].start, last.end - 300), {quiet: true});
}

// Pick up segments recorded since the page loaded (or removed by retention).
async function refreshDay() {
  if (!st.day) return;
  const curName = st.cur >= 0 ? st.segs[st.cur].name : null;
  const segs = await api("/api/segments?day=" + encodeURIComponent(st.day));
  if (segs.length === st.segs.length && segs.at(-1)?.name === st.segs.at(-1)?.name) return;
  st.segs = segs;
  st.ranges = mergeRanges(segs);
  st.cur = curName ? segs.findIndex(s => s.name === curName) : -1;
  st.hour = -1;
  drawDay();
  prepareNext();
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
    const gap = st.segs[next].start - st.segs[st.cur].end;
    if (gap > GAP) toast(`Bỏ qua khoảng không ghi ${hm(st.segs[st.cur].end)}–${hm(st.segs[next].start)}.`);
    show(next, 0);
  } else {
    st.wantPlay = false;
    render();
    // the newest day keeps growing: try to continue once the next minute is saved
    if (st.day === st.days[0]) setTimeout(async () => {
      const before = st.segs.length;
      await refreshDay();
      if (st.segs.length > before) { st.wantPlay = true; show(before, 0); }
    }, 5000);
  }
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

function togglePlay() {
  if (st.cur < 0) return;
  st.wantPlay = front.paused;
  if (st.wantPlay) front.play().catch(() => { st.wantPlay = false; render(); });
  else front.pause();
  render();
}

function setRate(r) {
  st.rate = r;
  for (const v of [front, back]) v.defaultPlaybackRate = v.playbackRate = r;
  document.querySelectorAll("[data-rate]").forEach(b => b.setAttribute("aria-pressed", String(+b.dataset.rate === r)));
}

// ---------- drawing ----------
const pct = (t, from, span) => `${clamp((t - from) / span, 0, 1) * 100}%`;

function drawRanges(track, from, span) {
  track.querySelectorAll(".range").forEach(el => el.remove());
  for (const r of st.ranges) {
    if (r.end <= from || r.start >= from + span) continue;
    const el = document.createElement("div");
    el.className = "range";
    el.style.left = pct(r.start, from, span);
    el.style.width = `calc(${pct(r.end, from, span)} - ${pct(r.start, from, span)})`;
    track.prepend(el);
  }
  if (!track.querySelector(".playhead")) {
    const ph = document.createElement("div");
    ph.className = "playhead";
    track.append(ph);
  }
}

function drawDay() {
  const idx = st.days.indexOf(st.day);
  $("day-label").textContent = dayTitle(st.day);
  $("prev-day").disabled = idx < 0 || idx >= st.days.length - 1;
  $("next-day").disabled = idx <= 0;
  const recorded = st.ranges.reduce((sum, r) => sum + (r.end - r.start), 0);
  $("day-summary").textContent = st.segs.length
    ? `Đã ghi ${Math.floor(recorded / HOUR)} giờ ${pad(Math.round(recorded % HOUR / 60))} phút, từ ${hm(st.segs[0].start)} đến ${hm(st.segs.at(-1).end)}`
    : "Không có bản ghi trong ngày này";
  drawRanges($("day-track"), st.dayStart, DAY);
}

function drawHour(hour) {
  st.hour = hour;
  const from = st.dayStart + hour * HOUR;
  $("hour-title").textContent = `${pad(hour)}:00 – ${pad((hour + 1) % 24)}:00`;
  drawRanges($("hour-track"), from, HOUR);
  const box = $("thumbs");
  box.textContent = "";
  for (let k = 0; k < 12; k++) {
    const mark = from + k * 300;
    const {i, exact} = locate(mark);
    const seg = i >= 0 && (exact || st.segs[i].start < mark + 300) ? st.segs[i] : null;
    const b = document.createElement("button");
    b.className = "thumb";
    b.dataset.from = mark;
    b.dataset.to = mark + 300;
    if (seg) {
      const img = new Image();
      img.loading = "lazy";
      img.alt = "";
      img.src = thumbUrl(seg);
      img.onerror = () => img.replaceWith(Object.assign(document.createElement("i"), {className: "blank"}));
      b.append(img);
      b.onclick = () => seekTo(Math.max(mark, seg.start));
      b.setAttribute("aria-label", `Xem từ ${hm(Math.max(mark, seg.start))}`);
    } else {
      b.append(Object.assign(document.createElement("i"), {className: "blank"}));
      b.disabled = true;
      b.setAttribute("aria-label", `Không có bản ghi ${hm(mark)}`);
    }
    b.append(Object.assign(document.createElement("span"), {textContent: hm(mark)}));
    box.append(b);
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
  const play = $("play");
  play.innerHTML = playing ? ICON_PAUSE : ICON_PLAY;
  play.setAttribute("aria-label", playing ? "Dừng" : "Phát");

  const hour = clamp(Math.floor((t - st.dayStart) / HOUR), 0, 23);
  if (hour !== st.hour) drawHour(hour);
  $("day-track").querySelector(".playhead").style.left = pct(t, st.dayStart, DAY);
  $("hour-track").querySelector(".playhead").style.left = pct(t, st.dayStart + hour * HOUR, HOUR);
  for (const track of [$("day-track"), $("hour-track")]) track.setAttribute("aria-valuetext", hms(t));
  document.querySelectorAll(".thumb").forEach(b =>
    b.setAttribute("aria-current", String(t >= +b.dataset.from && t < +b.dataset.to)));

  const dl = $("download");
  if (st.cur >= 0) {
    const seg = st.segs[st.cur];
    dl.href = `/media/${seg.name}?download=1`;
    $("download-label").textContent = `Tải đoạn ${hm(seg.start)}–${hm(seg.end)}`;
    dl.hidden = false;
  } else {
    dl.hidden = true;
  }

  if (Date.now() - lastHashUpdate > 1000) saveHash();
}

// ---------- track interaction: hover/drag shows a preview, release seeks ----------
function attachTrack(track, range) {
  const tip = document.createElement("div");
  tip.className = "tip";
  tip.hidden = true;
  const img = new Image();
  img.alt = "";
  const label = document.createElement("div");
  tip.append(img, label);
  track.append(tip);
  let dragging = false;

  const timeAt = e => {
    const box = track.getBoundingClientRect();
    const [from, span] = range();
    return from + clamp((e.clientX - box.left) / box.width, 0, 0.99999) * span;
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
}

attachTrack($("day-track"), () => [st.dayStart, DAY]);
attachTrack($("hour-track"), () => [st.dayStart + st.hour * HOUR, HOUR]);

// ---------- controls ----------
$("play").addEventListener("click", togglePlay);
document.querySelectorAll("[data-seek]").forEach(b => b.addEventListener("click", () => seekTo(now() + +b.dataset.seek)));
document.querySelectorAll("[data-rate]").forEach(b => b.addEventListener("click", () => setRate(+b.dataset.rate)));
$("prev-day").addEventListener("click", () => {
  const i = st.days.indexOf(st.day);
  if (i < st.days.length - 1) loadDay(st.days[i + 1], null);
});
$("next-day").addEventListener("click", () => {
  const i = st.days.indexOf(st.day);
  if (i > 0) loadDay(st.days[i - 1], null);
});
document.addEventListener("keydown", e => {
  if (e.target.closest("input, select, textarea") || e.ctrlKey || e.metaKey || e.altKey) return;
  if (e.key === " " && !e.target.closest("button, a")) { e.preventDefault(); togglePlay(); }
  else if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
    e.preventDefault();
    const step = (e.shiftKey ? 60 : 10) * (e.key === "ArrowLeft" ? -1 : 1);
    seekTo(now() + step);
  }
});

// ---------- start ----------
(async function init() {
  st.days = await api("/api/days");
  if (!st.days.length) { $("empty").hidden = false; return; }
  $("player-ui").hidden = false;
  front.hidden = false;
  const m = /^#(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2}):(\d{2})$/.exec(location.hash);
  if (m && st.days.includes(m[1])) {
    await loadDay(m[1], midnight(m[1]) + (+m[2]) * HOUR + (+m[3]) * 60 + (+m[4]));
  } else {
    await loadDay(st.days[0], null);
  }
  setInterval(() => { if (st.day === st.days[0]) refreshDay(); }, 30000);
})();
