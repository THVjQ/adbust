// adbust web: talks to the phone straight from the browser over WebUSB.
// Flow: plug in -> read every app -> offer to remove the ones other people
// already removed -> watch what opens -> remove the culprits -> report them.

import { Adb, AdbDaemonTransport } from "@yume-chan/adb";
import {
  AdbDaemonWebUsbDevice,
  AdbDaemonWebUsbDeviceManager,
} from "@yume-chan/adb-daemon-webusb";
import AdbWebCredentialStore from "@yume-chan/adb-credential-web";
import { ReadableStream } from "@yume-chan/stream-extra";

const HELPER = "/data/local/tmp/adbust-applist.dex";
const PKG_RE = /^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+$/;

// Activity class fragments that belong to ad SDKs (same list as adbust.py).
const AD_MARKERS = [
  "adactivity", "interstitial", "admob", "gms.ads", "facebook.ads",
  "audiencenetwork", "applovin", "unity3d.ads", "ironsource", "mbridge",
  "mintegral", "vungle", "chartboost", "inmobi", "startapp", "pangle",
  "openadsdk", "fyber", "adcolony", "appodeal", "yandex.mobile.ads",
  "bigosdk", "tapjoy", "adsdk", "fullscreenad", "rewardedad",
];
const OVERLAY_TYPES = new Set([
  "APPLICATION_OVERLAY", "SYSTEM_ALERT", "SYSTEM_OVERLAY", "PHONE",
  "PRIORITY_PHONE", "SYSTEM_ERROR",
]);
// Never listed as "opened": the phone's own chrome.
const IGNORE_OPENED = new Set(["com.android.systemui", "android"]);

const $ = (s) => document.querySelector(s);
const el = (tag, props = {}, ...kids) => {
  const n = Object.assign(document.createElement(tag), props);
  for (const k of kids) n.append(k);
  return n;
};

const state = {
  adb: null,
  device: null,
  apps: new Map(),   // pkg -> {pkg,label,system,enabled,launcher,installer,installed}
  home: "",
  bad: new Map(),    // pkg -> shared database entry
  good: new Map(),   // pkg -> entry in the shared "sorted good" list
  sortMark: new Map(), // pkg -> good? : unsaved ticks in the sorter
  phone: "",           // hash of the connected phone's serial, for counting
  opened: new Map(), // pkg -> {pkg,kind,activity,first,last,count}
  picked: new Set(),
  busy: false,
  watcher: null,
  running: [],             // apps running in the background right now
  keepStopped: new Set(),  // pkgs to re-kill while connected
  guard: null,             // interval that re-stops kept apps
  runTimer: null,          // interval that refreshes the running list
  view: "main",            // "main" or "adv" (full-page Advanced)
  log: [],                 // recent adb commands + replies, for the console
};

// ------------------------------------------------------------ platform

function platform() {
  const ua = navigator.userAgent.toLowerCase();
  const p = (navigator.userAgentData?.platform || navigator.platform || "")
    .toLowerCase();
  if (/iphone|ipad|ipod|android/.test(ua)) return "mobile";
  if (p.includes("mac") || ua.includes("macintosh")) return "mac";
  if (p.includes("win")) return "windows";
  if (p.includes("linux") || ua.includes("linux") || ua.includes("cros"))
    return "linux";
  return "other";
}

function clientId() {
  try {
    let id = localStorage.getItem("adbust-client");
    if (!id) {
      id = crypto.randomUUID();
      localStorage.setItem("adbust-client", id);
    }
    return id;
  } catch {
    return crypto.randomUUID();
  }
}

// ------------------------------------------------------------ status

function status(text, kind = "info") {
  const s = $("#status");
  s.className = `status ${kind}`;
  s.textContent = text;
}

function showStep(name) {
  for (const n of document.querySelectorAll("[data-step]"))
    n.hidden = !n.dataset.step.split(" ").includes(name);
}

// ------------------------------------------------------------ adb helpers

function logCmd(cmd, out, err) {
  // One-line, trimmed reply so a huge dumpsys/base64 doesn't run away.
  const o = (out || "").replace(/\s+/g, " ").trim().slice(0, 300);
  const last = state.log[state.log.length - 1];
  if (last && last.cmd === cmd && last.err === !!err) {
    // Same command firing again (the 5s poll, the keep-stopped watchdog):
    // collapse into the one entry with a counter instead of flooding.
    last.out = o; last.t = Date.now(); last.n++;
  } else {
    state.log.push({ t: Date.now(), cmd, out: o, err: !!err, n: 1 });
    if (state.log.length > 200) state.log.shift();
  }
  if (state.view === "adv") renderLog();
}

const sh = async (cmd) => {
  try {
    const out = await state.adb.subprocess.noneProtocol.spawnWaitText(cmd);
    logCmd(cmd, out, false);
    return out;
  } catch (e) {
    logCmd(cmd, String(e?.message || e), true);
    throw e;
  }
};

async function pushHelper() {
  const res = await fetch("adbust-applist.dex", { cache: "no-cache" });
  if (!res.ok) throw new Error("could not download the app-list helper");
  const bytes = new Uint8Array(await res.arrayBuffer());
  const sync = await state.adb.sync();
  try {
    await sync.write({
      filename: HELPER,
      file: new ReadableStream({
        start(c) { c.enqueue(bytes); c.close(); },
      }),
      permission: 0o644,
    });
  } finally {
    await sync.dispose();
  }
}

function parseAppList(text) {
  const apps = new Map();
  for (const line of text.split("\n")) {
    const f = line.replace(/\r$/, "").split("\t");
    if (f.length < 7 || !PKG_RE.test(f[0])) continue;
    apps.set(f[0], {
      pkg: f[0],
      label: f[1] && f[1] !== f[0] ? f[1] : "",
      system: f[2] === "1",
      enabled: f[3] === "1",
      launcher: f[4] === "1",
      installer: f[5],
      installed: Number(f[6]) || 0,
    });
  }
  return apps;
}

async function readApps() {
  try {
    await pushHelper();
    const out = await sh(`CLASSPATH=${HELPER} app_process / AppList; ` +
                         `rm -f ${HELPER}`);
    const apps = parseAppList(out);
    if (apps.size) return apps;
  } catch (e) {
    console.warn("helper failed, falling back to pm", e);
  }
  // No names this way, but still a complete list.
  const apps = new Map();
  for (const [flag, system] of [["-3", false], ["-s", true]]) {
    const out = await sh(`pm list packages ${flag}`);
    for (const m of out.matchAll(/^package:(\S+)/gm))
      apps.set(m[1], { pkg: m[1], label: "", system, enabled: true,
                       launcher: false, installer: "", installed: 0 });
  }
  return apps;
}

// Fetch launcher icons for the given packages and stash a data URL on each
// app record. Done in chunks because each icon is a base64 PNG. Returns true
// if any new icon was added, so the caller knows to re-render.
async function loadIcons(pkgs, onProgress) {
  const want = [...new Set(pkgs)]
    .filter((p) => state.apps.get(p) && !state.apps.get(p).icon);
  if (!want.length) return false;
  let got = false;
  try {
    await pushHelper();
    // Small batches: each icon is a base64 PNG, and one huge shell reply is
    // easy to truncate. Smaller replies also let icons show up as they land.
    for (let i = 0; i < want.length; i += 6) {
      const chunk = want.slice(i, i + 6);           // pkg names are [\w.]+, safe unquoted
      let any = false;
      const out = await sh(`CLASSPATH=${HELPER} app_process / AppList --icons ` +
                           chunk.join(" "));
      for (const line of out.split("\n")) {
        const t = line.indexOf("\t");
        if (t < 0) continue;
        const pkg = line.slice(0, t), b64 = line.slice(t + 1).replace(/\r$/, "");
        const a = state.apps.get(pkg);
        if (a && b64) { a.icon = "data:image/png;base64," + b64; got = any = true; }
      }
      if (any && onProgress) onProgress();
    }
    await sh(`rm -f ${HELPER}`);
  } catch (e) {
    console.warn("icon fetch failed", e);
  }
  return got;
}

async function readHome() {
  const out = await sh("cmd package resolve-activity --brief " +
    "-a android.intent.action.MAIN -c android.intent.category.HOME");
  const m = out.trim().split("\n").pop().match(/^([\w.]+)\//);
  return m ? m[1] : "";
}

function nameOf(pkg) {
  return state.apps.get(pkg)?.label || state.bad.get(pkg)?.label || pkg;
}

// ------------------------------------------------------------ running apps

// Packages with a live process right now. The activity manager knows the
// full process name of each running app; `ps` only knows the kernel "comm"
// name, which is truncated to 15 chars, so long package names (e.g. YouTube
// Music) never matched. Parse ProcessRecord lines, fall back to ps with a
// prefix match for the truncation.
async function readRunning() {
  const procs = new Set();
  let out = "";
  try { out = await sh("dumpsys activity processes"); } catch { /* fall back */ }
  // ProcessRecord{hash pid:procname/uid}; procname is the full package.
  const re = /ProcessRecord\{[0-9a-f]+ \d+:([A-Za-z0-9_.]+)[:/]/g;
  let m;
  while ((m = re.exec(out))) {
    const name = m[1];
    if (PKG_RE.test(name) && state.apps.has(name)) procs.add(name);
  }
  if (procs.size) return procs;

  // Fallback: ps. comm is truncated, so match installed packages by prefix.
  try { out = await sh("ps -A -o NAME"); } catch { out = ""; }
  const installed = [...state.apps.keys()];
  for (const raw of out.split("\n")) {
    let name = raw.trim();
    if (!name || name === "NAME") continue;
    name = name.split(":")[0];
    if (state.apps.has(name)) { procs.add(name); continue; }
    if (name.length >= 15) {                 // truncated comm — match by prefix
      const hit = installed.find((p) => p.startsWith(name));
      if (hit) procs.add(hit);
    }
  }
  return procs;
}

async function refreshRunning() {
  if (!state.adb) return;
  try {
    const procs = await readRunning();
    // Show what's running now, plus anything we're keeping stopped so it can
    // still be un-ticked even while it's successfully held down.
    const pkgs = new Set([...procs, ...state.keepStopped]);
    state.running = [...pkgs].map((p) => ({
      ...(state.apps.get(p) || { pkg: p, label: nameOf(p), system: false }),
      live: procs.has(p),
    })).filter((a) => !a.system)   // user apps only
      .sort((a, b) => (a.label || a.pkg).localeCompare(b.label || b.pkg));
  } catch (e) { console.warn("running list failed", e); }
  renderRunning();
}

async function stopApp(pkg) {
  try { await sh(`am force-stop ${pkg}`); }
  catch (e) { console.warn("force-stop failed", e); }
  await refreshRunning();
}

// Re-kill every kept package on a timer, but only while connected.
let guardBusy = false;
async function guardTick() {
  if (guardBusy || !state.adb) return;
  guardBusy = true;
  try {
    for (const pkg of state.keepStopped) {
      try { await sh(`am force-stop ${pkg}`); } catch { /* keep going */ }
    }
  } finally { guardBusy = false; }
}

function ensureGuard() {
  if (state.keepStopped.size && state.adb) {
    if (!state.guard) state.guard = setInterval(guardTick, 2000);
  } else if (state.guard) {
    clearInterval(state.guard);
    state.guard = null;
  }
  renderRunning();
}

function startRunning() {
  refreshRunning();
  if (state.runTimer) clearInterval(state.runTimer);
  state.runTimer = setInterval(refreshRunning, 5000);
}

function stopRunning() {
  if (state.guard) { clearInterval(state.guard); state.guard = null; }
  if (state.runTimer) { clearInterval(state.runTimer); state.runTimer = null; }
  state.keepStopped.clear();
  state.running = [];
}

async function uninstall(pkg) {
  const app = state.apps.get(pkg);
  if (!app || app.system || !PKG_RE.test(pkg))
    return { ok: false, msg: "system app, not removed" };
  await sh(`am force-stop ${pkg}`);
  let out = await sh(`pm uninstall ${pkg}`);
  if (!/Success/.test(out) && !/DEVICE_POLICY/.test(out))
    out = await sh(`pm uninstall --user 0 ${pkg}`);
  if (/Success/.test(out)) return { ok: true, msg: "removed" };
  if (/DEVICE_POLICY/.test(out)) {
    await sh("am start -a android.settings.APPLICATION_DETAILS_SETTINGS " +
             `-d package:${pkg}`);
    return { ok: false, admin: true,
             msg: "is a device admin. App info is open on the phone: turn " +
                  "off its admin access there, then remove it again" };
  }
  return { ok: false, msg: out.trim().split("\n").pop() || "failed" };
}

// ------------------------------------------------------------ shared database

async function loadBad() {
  try {
    const res = await fetch("api/badapps", { cache: "no-store" });
    const doc = await res.json();
    state.bad = new Map(doc.apps.map((a) => [a.pkg, a]));
    $("#dbcount").textContent = `${state.bad.size} app${state.bad.size === 1
      ? "" : "s"} in the shared database`;
  } catch {
    $("#dbcount").textContent = "shared database unreachable";
  }
}

async function report(items) {
  if (!items.length) return;
  try {
    await fetch("api/removed", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ client: clientId(), apps: items }),
    });
  } catch (e) {
    console.warn("report failed", e);
  }
  track("remove");
}

// Count one use of the site (visit, connect, remove, protect). Counts only,
// never what's on the phone; the server keeps no IP for these.
function track(event) {
  fetch("api/usage", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ client: clientId(), event, phone: state.phone }),
  }).catch(() => {});
}

// Lets the server count phones without ever seeing a serial number.
async function phoneId(serial) {
  try {
    const buf = await crypto.subtle.digest("SHA-256",
      new TextEncoder().encode("adbust-phone:" + serial));
    return [...new Uint8Array(buf).slice(0, 8)]
      .map((b) => b.toString(16).padStart(2, "0")).join("");
  } catch {
    return "";
  }
}

// "added 20 Oct" from the database's UTC timestamp, in the viewer's time.
function addedOn(b) {
  const d = new Date(b.added_at || b.added);
  return isNaN(d) ? "" : "added " + d.toLocaleDateString([],
    { day: "numeric", month: "short", year: "numeric" });
}

// ------------------------------------------------------------ connect

const manager = AdbDaemonWebUsbDeviceManager.BROWSER;
const credentials = new AdbWebCredentialStore("adbust");

const withTimeout = (p, ms, msg) => Promise.race([p, new Promise((_, rej) =>
  setTimeout(() => rej(new Error(msg)), ms))]);

// Returns true once the phone is connected and read.
async function connect(device) {
  if (state.busy || state.adb) return false;
  state.busy = true;
  showStep("work");
  try {
    status("Connecting to the phone…");
    let conn;
    try {
      // A USB entry left behind by a stuck phone never answers; don't hang on it.
      conn = await withTimeout(device.connect(), 10000,
        "The phone isn't answering over USB. Unplug it and plug it back in.");
    } catch (e) {
      if (e instanceof AdbDaemonWebUsbDevice.DeviceBusyError ||
          /claim|busy|access/i.test(String(e?.message))) {
        throw new Error("Another program is using the phone's USB " +
          "connection. Close adb, Android Studio, scrcpy, Smart Switch or " +
          "Android File Transfer, unplug the phone and plug it back in.");
      }
      throw e;
    }
    status("Look at the phone and tap Allow (tick \"Always allow\").", "ask");
    const transport = await AdbDaemonTransport.authenticate({
      serial: device.serial, connection: conn, credentialStore: credentials,
    });
    state.adb = new Adb(transport);
    state.device = device;
    state.phone = await phoneId(device.serial);
    state.adb.disconnected.then(onDisconnected, onDisconnected);

    const props = await sh("getprop ro.product.manufacturer; getprop " +
      "ro.product.model; getprop ro.build.version.release");
    const [maker, model, release] = props.trim().split(/\r?\n/);
    $("#phone").textContent =
      `${cap(maker)} ${model} · Android ${release} · ${device.serial}`;

    status("Reading every app on the phone…");
    const [apps, home] = await Promise.all([readApps(), readHome(),
                                            loadBad()]);
    state.apps = apps;
    state.home = home;
    state.view = "main";
    renderApps();
    showStep("main");
    track("connect");
    // Icons load in the background, appearing as each small batch lands.
    loadIcons([...apps.values()].filter((a) => a.launcher || !a.system)
      .map((a) => a.pkg), () => { renderApps(); renderOpened(); });
    const known = [...apps.values()]
      .filter((a) => !a.system && state.bad.has(a.pkg));
    if (known.length) askKnown(known);
    else status("No apps from the shared database. Watching for popups — " +
                "leave the phone on the home screen.", "ok");
    startWatch();
    startRunning();
    return true;
  } catch (e) {
    console.error(e);
    status(e?.message || String(e), "error");
    showStep("connect");
    stopRunning();
    try { await state.adb?.close(); } catch {}
    state.adb = null;
    return false;
  } finally {
    state.busy = false;
  }
}

function onDisconnected() {
  if (!state.adb) return;
  state.adb = null;
  state.watcher = null;
  stopRunning();
  renderRunning();
  status("Phone unplugged. Plug it back in to carry on.", "error");
}

const cap = (s) => (s ? s[0].toUpperCase() + s.slice(1) : "");

async function autoConnect() {
  if (!manager || state.adb) return;
  // The same phone can show up twice (a stale entry plus the live one), so
  // try each until one answers.
  for (const d of await manager.getDevices()) {
    if (await connect(d)) return;
  }
}

// ------------------------------------------------------------ known bad prompt

function askKnown(known) {
  const list = $("#known-list");
  list.replaceChildren();
  for (const a of known) {
    const b = state.bad.get(a.pkg);
    const box = el("input", { type: "checkbox", checked: true, value: a.pkg });
    list.append(el("label", { className: "row" }, box,
      el("span", { className: "name" }, nameOf(a.pkg)),
      el("span", { className: "pkg" }, a.pkg),
      el("span", { className: "tag bad" },
         `removed by ${b.reports} ${b.reports === 1 ? "person" : "people"}`),
      el("span", { className: "tag" }, addedOn(b))));
  }
  $("#known-title").textContent = `${known.length} app${known.length > 1 ?
    "s" : ""} on this phone ${known.length > 1 ? "were" : "was"} removed ` +
    "from other people's phones";
  $("#known").showModal();
  status("Choose which known-bad apps to remove.", "ask");
}

async function removeKnown() {
  const pkgs = [...document.querySelectorAll("#known-list input:checked")]
    .map((b) => b.value);
  $("#known").close();
  await removeMany(pkgs, "database");
}

// ------------------------------------------------------------ live watch

function parseFocus(text) {
  for (const key of ["mCurrentFocus=", "mFocusedApp="]) {
    const i = text.indexOf(key);
    if (i < 0) continue;
    const m = text.slice(i).match(/([A-Za-z][\w.]+)\/([\w.$]+)/);
    if (m && m[1] !== "null") return { pkg: m[1], activity: m[2] };
  }
  return null;
}

function parseOverlays(text) {
  const owners = new Set();
  for (const block of ("\n" + text).split(/\n\s*Window #\d+ /).slice(1)) {
    const ty = block.match(/\bty=([A-Z_]+)/);
    if (!ty || !OVERLAY_TYPES.has(ty[1])) continue;
    if (/isVisible=false|mViewVisibility=0x[48]\b|isOnScreen=false|mHasSurface=false/
        .test(block)) continue;
    const pm = block.match(/\bpackage=([A-Za-z][\w.]+)/) ||
               block.match(/^Window\{\w+ u\d+ ([A-Za-z][\w.]+)/);
    if (pm) owners.add(pm[1]);
  }
  return owners;
}

const RANK = { opened: 0, OVERLAY: 1, AD: 2 };

function noteOpened(pkg, kind, activity) {
  if (!state.apps.has(pkg) || pkg === state.home || IGNORE_OPENED.has(pkg))
    return;
  const now = new Date();
  let o = state.opened.get(pkg);
  if (!o) {
    o = { pkg, kind, activity, first: now, last: now, count: 0 };
    state.opened.set(pkg, o);
    // Ads and overlays are what this is for, so pick those straight away.
    if (kind !== "opened" && !state.apps.get(pkg).system)
      state.picked.add(pkg);
  }
  if (RANK[kind] > RANK[o.kind]) {
    o.kind = kind;
    if (!state.apps.get(pkg).system) state.picked.add(pkg);
  }
  if (activity) o.activity = activity;
  o.last = now;
  o.count += 1;
  renderOpened();
}

async function startWatch() {
  // One long-lived shell loop on the phone streams the foreground window,
  // and every few ticks the windows drawn over other apps.
  const script =
    "i=0; while true; do " +
    "echo \"@F $(dumpsys window | grep -E 'mCurrentFocus=|mFocusedApp=' | tr '\\n' ' ')\"; " +
    "if [ $((i % 4)) -eq 0 ]; then echo @O; dumpsys window windows | " +
    "grep -E 'Window #|package=|ty=|isVisible=|mViewVisibility=|mHasSurface=|isOnScreen='; " +
    "echo @E; fi; i=$((i+1)); sleep 0.7; done";
  const proc = await state.adb.subprocess.noneProtocol.spawn(script);
  state.watcher = proc;
  const reader = proc.output.pipeThrough(new TextDecoderStream()).getReader();
  let buf = "", overlay = null, lastPkg = "";
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done || state.watcher !== proc) break;
      buf += value;
      let nl;
      while ((nl = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, nl);
        buf = buf.slice(nl + 1);
        if (line.startsWith("@F ")) {
          const f = parseFocus(line);
          if (f && f.pkg !== lastPkg) {
            lastPkg = f.pkg;
            const ad = AD_MARKERS.some((m) => f.activity.toLowerCase().includes(m));
            noteOpened(f.pkg, ad ? "AD" : "opened", f.activity);
          }
        } else if (line === "@O") {
          overlay = [];
        } else if (line === "@E") {
          for (const p of parseOverlays(overlay.join("\n")))
            if (!state.opened.get(p) || state.opened.get(p).kind !== "OVERLAY")
              noteOpened(p, "OVERLAY", "");
          overlay = null;
        } else if (overlay) {
          overlay.push(line);
        }
      }
    }
  } catch (e) {
    console.warn("watch stopped", e);
  }
}

// ------------------------------------------------------------ removing

async function removeMany(pkgs, how) {
  pkgs = pkgs.filter((p) => state.apps.has(p) && !state.apps.get(p).system);
  if (!pkgs.length || state.busy) return;
  state.busy = true;
  renderBar();
  const removed = [], failed = [];
  for (const [i, pkg] of pkgs.entries()) {
    status(`Removing ${i + 1} of ${pkgs.length}: ${nameOf(pkg)}…`);
    const r = await uninstall(pkg).catch((e) => ({ ok: false, msg: e.message }));
    if (r.ok) {
      // Every removal is reported. Ones that weren't real adware get
      // marked good in the sorter (Ctrl+Shift+E), which drops them off.
      removed.push({ pkg, label: nameOf(pkg),
                     how: state.opened.has(pkg) ? "popup" : how });
      state.apps.delete(pkg);
      state.picked.delete(pkg);
      const o = state.opened.get(pkg);
      if (o) o.removed = true;
    } else {
      failed.push(`${nameOf(pkg)} ${r.msg}`);
    }
  }
  // Everything removed goes into the shared database.
  await report(removed);
  await loadBad();
  state.busy = false;
  renderApps();
  renderOpened();
  const ok = `Removed ${removed.length} app${removed.length === 1 ? "" : "s"}.`;
  if (failed.length) status(`${ok} Not removed: ${failed.join("; ")}`, "error");
  else status(`${ok} Still watching — anything else that pops up appears ` +
              "below.", "ok");
}

// ------------------------------------------------------------ rendering

function fmtTime(d) {
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit",
                                    second: "2-digit" });
}

function appRow(a, extra = []) {
  const bad = state.bad.get(a.pkg);
  const box = el("input", { type: "checkbox", disabled: a.system,
                            checked: state.picked.has(a.pkg) });
  box.addEventListener("change", () => {
    box.checked ? state.picked.add(a.pkg) : state.picked.delete(a.pkg);
    renderOpened();
    renderApps();
  });
  const tags = [];
  if (a.system) tags.push(el("span", { className: "tag sys" }, "system"));
  if (bad) tags.push(el("span", { className: "tag bad", title: addedOn(bad) },
                        `removed by ${bad.reports}`));
  if (!a.launcher && !a.system) tags.push(el("span", { className: "tag" },
                                             "no icon"));
  const ico = a.icon
    ? el("img", { className: "ico", src: a.icon, alt: "", loading: "lazy",
                  width: 22, height: 22 })
    : el("span", { className: "ico ph" });
  return el("label", { className: `row${a.system ? " locked" : ""}` }, box, ico,
    el("span", { className: "name" }, a.label || a.pkg),
    el("span", { className: "pkg" }, a.pkg), ...tags, ...extra);
}

function renderApps() {
  const all = [...state.apps.values()].sort((x, y) =>
    (x.label || x.pkg).localeCompare(y.label || y.pkg));
  const user = all.filter((a) => !a.system);
  const sys = all.filter((a) => a.system);
  $("#user-count").textContent = user.length;
  $("#sys-count").textContent = sys.length;
  $("#user-list").replaceChildren(...user.map((a) => appRow(a)));
  // System apps are listed for reference only; there are hundreds, so they
  // are only built when the section is opened.
  const sl = $("#sys-list");
  sl.dataset.pending = "1";
  if ($("#sys").open) buildSys();
  renderBar();
}

function buildSys() {
  const sl = $("#sys-list");
  if (sl.dataset.pending !== "1") return;
  sl.dataset.pending = "";
  const sys = [...state.apps.values()].filter((a) => a.system)
    .sort((x, y) => (x.label || x.pkg).localeCompare(y.label || y.pkg));
  sl.replaceChildren(...sys.map((a) => appRow(a)));
}

const openedIsSystem = (o) => !!(state.apps.get(o.pkg) || {}).system;

function openedRow(o) {
  const a = state.apps.get(o.pkg) ||
    { pkg: o.pkg, label: nameOf(o.pkg), system: false, launcher: true };
  const kind = el("span", { className: `kind ${o.kind}` },
    o.kind === "AD" ? "AD" : o.kind === "OVERLAY" ? "draws over" : "opened");
  const when = el("span", { className: "when" },
    `${fmtTime(o.last)}${o.count > 1 ? ` · ${o.count}×` : ""}`);
  const row = appRow(a, [when]);
  row.prepend(kind);
  if (o.removed) {
    row.classList.add("gone");
    row.querySelector("input").disabled = true;
    row.querySelector("input").checked = false;
    row.append(el("span", { className: "tag ok" }, "removed"));
  }
  return row;
}

function renderOpened() {
  const rows = [...state.opened.values()].sort((a, b) =>
    RANK[b.kind] - RANK[a.kind] || b.last - a.last);
  $("#opened-count").textContent = rows.filter((o) => !o.removed).length;
  $("#opened-empty").hidden = rows.length > 0;

  // Group user apps above system apps; each group keeps the rank/recency sort.
  const user = rows.filter((o) => !openedIsSystem(o));
  const sys = rows.filter((o) => openedIsSystem(o));
  const out = [];
  if (user.length) {
    out.push(el("div", { className: "grouphead" }, "User apps"));
    out.push(...user.map(openedRow));
  }
  if (sys.length) {
    out.push(el("div", { className: "grouphead" },
      "System apps — part of the phone, can't be removed"));
    out.push(...sys.map(openedRow));
  }
  $("#opened-list").replaceChildren(...out);

  // Pull icons for anything that popped up but hasn't got one yet.
  const missing = rows.map((o) => o.pkg)
    .filter((p) => state.apps.get(p) && !state.apps.get(p).icon);
  if (missing.length && state.adb && !state.iconBusy) {
    state.iconBusy = true;
    loadIcons(missing, () => { renderOpened(); renderApps(); })
      .finally(() => { state.iconBusy = false; });
  }

  // Select-all only applies to removable user apps that are still present.
  const pick = user.filter((o) => !o.removed);
  const allSel = pick.length > 0 && pick.every((o) => state.picked.has(o.pkg));
  const sa = $("#opened-selectall");
  if (sa) {
    sa.hidden = pick.length === 0;
    sa.textContent = allSel ? "Clear selection" : "Select all";
  }
  renderBar();
}

function iconEl(a) {
  return a.icon
    ? el("img", { className: "ico", src: a.icon, alt: "", width: 22, height: 22 })
    : el("span", { className: "ico ph" });
}

function renderRunning() {
  const list = state.running;
  $("#running-count").textContent = list.length;
  $("#running-empty").hidden = list.length > 0;
  $("#running-list").replaceChildren(...list.map((a) => {
    const stop = el("button", { className: "small danger", type: "button" },
                    a.live ? "Stop" : "Stopped");
    stop.disabled = !a.live;
    stop.addEventListener("click", (e) => { e.preventDefault(); stopApp(a.pkg); });

    const box = el("input", { type: "checkbox",
                              checked: state.keepStopped.has(a.pkg) });
    box.addEventListener("change", () => {
      box.checked ? state.keepStopped.add(a.pkg) : state.keepStopped.delete(a.pkg);
      ensureGuard();
    });
    const keep = el("label", { className: "keep" }, box, "keep stopped");

    const kids = [iconEl(a), el("span", { className: "name" }, a.label || a.pkg),
      el("span", { className: "pkg" }, a.pkg)];
    if (a.system) kids.push(el("span", { className: "tag sys" }, "system"));
    if (!a.live) kids.push(el("span", { className: "run-state" }, "held stopped"));
    kids.push(keep, stop);
    return el("div", { className: `row${a.live ? "" : " dim"}` }, ...kids);
  }));
}

function renderBar() {
  for (const p of state.picked) if (!state.apps.has(p)) state.picked.delete(p);
  const n = state.picked.size;
  const btn = $("#remove");
  btn.disabled = n === 0 || state.busy;
  btn.textContent = n ? `Uninstall ${n} selected app${n > 1 ? "s" : ""}`
                      : "Tick apps to uninstall";
  if (state.view === "adv") {               // full-page Advanced owns the screen
    $("#bar").hidden = true;
    $("#advanced").hidden = true;
    return;
  }
  $("#bar").hidden = !state.adb && n === 0;
  $("#done").hidden = !state.adb;
  $("#advanced").hidden = !state.adb;
}

// ------------------------------------------------------------ hardening

// Apps the per-app guards target: non-system apps that are ticked, were
// caught popping up / drawing over the screen, or are in the shared database.
function suspects() {
  const s = new Set();
  const add = (p) => {
    const a = state.apps.get(p);
    if (a && !a.system) s.add(p);
  };
  for (const p of state.picked) add(p);
  for (const [p, o] of state.opened)
    if (o.kind === "AD" || o.kind === "OVERLAY") add(p);
  for (const p of state.bad.keys()) add(p);
  return [...s];
}

const HARDEN_OPS = {
  overlay:    { op: "SYSTEM_ALERT_WINDOW",      label: "Block pop-ups" },
  install:    { op: "REQUEST_INSTALL_PACKAGES", label: "Block installing apps" },
  fullscreen: { op: "USE_FULL_SCREEN_INTENT",   label: "Block full-screen ads" },
  settings:   { op: "WRITE_SETTINGS",           label: "Block settings changes" },
};

async function applyHarden(opts) {
  const sus = suspects();
  const out = [];
  for (const key of Object.keys(HARDEN_OPS)) {
    if (!opts.includes(key)) continue;
    const { op, label } = HARDEN_OPS[key];
    let ok = 0;
    for (const pkg of sus) {
      try {
        await sh(`appops set ${pkg} ${op} deny`);
        const got = await sh(`appops get ${pkg} ${op}`);
        if (/deny|ignore/i.test(got)) ok++;
      } catch { /* some phones reject some ops */ }
    }
    out.push(`${ok === sus.length ? "✓" : "⚠"} ` +
             `${label}: ${ok}/${sus.length} app${sus.length === 1 ? "" : "s"}`);
  }
  if (opts.includes("chrome")) {
    let res;
    try { res = await sh("pm clear com.android.chrome 2>&1"); }
    catch (e) { res = String(e?.message || e); }
    out.push(/Success/.test(res)
      ? "✓ Chrome wiped. Opening it again starts fresh — sign back in to get " +
        "synced passwords and bookmarks back."
      : `⚠ Chrome wipe failed: ${res.trim().split("\n").pop() || "no reply"}`);
  }
  // Ad settings live in the Google account and Play services, not anywhere
  // adb can write, so these open the right screen and say what to tap. One
  // button each, because opening both at once would bury the first screen.
  for (const key of Object.keys(AD_STEPS)) {
    if (opts.includes(key)) out.push(adStepRow(AD_STEPS[key]));
  }
  return out;
}

const AD_STEPS = {
  adsoff: {
    label: "Personalised ads",
    tap: "on the phone, sign in if asked, then turn Personalised ads off",
    cmds: ["am start -a android.intent.action.VIEW " +
           "-d https://myadcenter.google.com/ 2>&1"],
  },
  adid: {
    label: "Advertising ID",
    tap: "on the phone, then tap Delete advertising ID and confirm",
    cmds: ["am start -a com.google.android.gms.settings.ADS_PRIVACY 2>&1",
           "am start -a android.settings.PRIVACY_SETTINGS 2>&1"],
  },
};

function adStepRow({ label, tap, cmds }) {
  const note = el("span", {}, ` — press Open, ${tap}.`);
  const btn = el("button", { className: "small", textContent: "Open" });
  btn.addEventListener("click", async () => {
    for (const cmd of cmds) {
      let res;
      try { res = await sh(cmd); } catch { continue; }
      if (!/Error|Exception/.test(res)) {
        btn.textContent = "Open again";
        if (cmd !== cmds[0])
          note.textContent = " — opened Privacy settings instead. Tap Ads " +
                             "(on Samsung: Other privacy settings → Ads), " +
                             "then Delete advertising ID.";
        return;
      }
    }
    note.textContent = " — couldn't open it. On the phone go to Settings → " +
                       "Google → Ads.";
  });
  return el("span", { className: "adstep" }, `• ${label} `, btn, note);
}

function openHarden() {
  $("#harden-n").textContent = suspects().length;
  $("#harden-result").hidden = true;
  for (const b of document.querySelectorAll("#harden input[type=checkbox]"))
    b.checked = false;
  $("#harden-apply").disabled = false;
  $("#harden-apply").textContent = "Apply";
  $("#harden").showModal();
}

// ------------------------------------------------------------ sorter

// Hidden behind Ctrl+Shift+E. Goes through the shared database, not the
// phone: every entry is bad until someone ticks it good. Good apps leave the
// bad list on the server, so no phone flags or pre-ticks them any more.

async function loadGood() {
  try {
    const doc = await (await fetch("api/goodapps", { cache: "no-store" })).json();
    state.good = new Map(doc.apps.map((a) => [a.pkg, a]));
  } catch {
    state.good = new Map();
  }
}

// Ticked = good. Unsaved changes override what the server says.
const isGood = (pkg) => state.sortMark.has(pkg) ? state.sortMark.get(pkg)
                                                : state.good.has(pkg);

function sortEntries() {
  const showGood = $("#sort-showgood").checked;
  const all = [...state.bad.values()];
  if (showGood) all.push(...state.good.values());
  return all.sort((x, y) => (x.label || x.pkg).localeCompare(y.label || y.pkg));
}

function renderSorter() {
  const q = $("#sort-find").value.trim().toLowerCase();
  const shown = sortEntries().filter((a) => !q ||
    (a.label || "").toLowerCase().includes(q) || a.pkg.includes(q));
  $("#sort-n").textContent = state.bad.size;
  $("#sort-goodn").textContent = state.good.size;
  const list = $("#sort-list");
  const top = list.scrollTop;  // ticking re-renders; don't jump to the top
  list.replaceChildren(...(shown.length ? shown.map(sortRow)
    : [el("p", { className: "hint" }, q ? "Nothing matches."
        : "The shared database is empty.")]));
  list.scrollTop = top;
  const n = state.sortMark.size;
  $("#sort-save").disabled = !n;
  $("#sort-save").textContent = n ? `Save ${n} change${n === 1 ? "" : "s"}`
                                  : "Save";
}

function sortRow(a) {
  const good = isGood(a.pkg);
  const box = el("input", { type: "checkbox", checked: good });
  const app = state.apps.get(a.pkg);
  const row = el("label", { className: `row${good ? " good" : ""}` }, box,
    app ? iconEl(app) : el("span", { className: "ico ph" }),
    el("span", { className: "name" }, a.label || a.pkg),
    el("span", { className: "pkg" }, a.pkg),
    el("span", { className: "tag" },
       `removed by ${a.reports} ${a.reports === 1 ? "person" : "people"}`),
    el("span", { className: "tag" }, addedOn(a)));
  if (app) row.append(el("span", { className: "tag bad" }, "on this phone"));
  row.append(el("a", { className: "tag", target: "_blank", rel: "noopener",
                       href: "https://play.google.com/store/apps/details?id=" +
                             encodeURIComponent(a.pkg) }, "Play Store ↗"));
  box.addEventListener("change", () => {
    if (box.checked === state.good.has(a.pkg)) state.sortMark.delete(a.pkg);
    else state.sortMark.set(a.pkg, box.checked);
    renderSorter();
  });
  return row;
}

async function saveSort() {
  const btn = $("#sort-save");
  const items = [...state.sortMark].map(([pkg, good]) =>
    ({ pkg, good, label: state.bad.get(pkg)?.label || "" }));
  btn.disabled = true;
  btn.textContent = "Saving…";
  let accepted = 0;
  try {
    for (let i = 0; i < items.length; i += 100) {
      const res = await fetch("api/good", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ client: clientId(),
                               apps: items.slice(i, i + 100) }),
      });
      accepted += (await res.json()).accepted || 0;
    }
    track("sort");
    const good = items.filter((x) => x.good).length;
    $("#sort-result").textContent = `Saved: ${good} marked good, ` +
      `${items.length - good} back on the bad list.` +
      (accepted < items.length ? " Some hit the daily limit." : "");
  } catch {
    $("#sort-result").textContent = "Couldn't reach the shared database.";
  }
  state.sortMark.clear();
  await Promise.all([loadGood(), loadBad()]);
  renderSorter();
  if (state.adb) renderApps();
}

async function loadStats() {
  const box = $("#sort-stats");
  try {
    const midnight = new Date();
    midnight.setHours(0, 0, 0, 0);
    const today = midnight.toISOString().slice(0, 19) + "Z";
    const s = await (await fetch(`api/stats?today=${today}`,
                                 { cache: "no-store" })).json();
    const day = (x) => new Date(x).toLocaleDateString([],
      { day: "numeric", month: "short", year: "numeric" });
    const d = day(s.since);
    box.replaceChildren(
      el("b", {}, String(s.visits_today)), " visits today · ",
      el("b", {}, String(s.visits)), " visits by ",
      el("b", {}, String(s.visitors)),
      ` visitor${s.visitors === 1 ? "" : "s"} since ${day(s.visits_since)}`,
      el("br"),
      el("b", {}, String(s.phones_cleaned)),
      ` phone${s.phones_cleaned === 1 ? "" : "s"} cleaned · `,
      el("b", {}, String(s.phones)),
      ` connected (since ${d}) · `,
      el("b", {}, String(s.removals)), " removals reported in total");
  } catch {
    box.textContent = "Counter unavailable.";
  }
}

async function toggleSorter() {
  const card = $("#sorter");
  card.hidden = !card.hidden;
  if (card.hidden) return;
  $("#sort-result").textContent = "";
  state.sortMark.clear();
  await Promise.all([loadGood(), loadBad(), loadStats()]);
  renderSorter();
  card.scrollIntoView({ behavior: "smooth" });
}

// ------------------------------------------------------------ advanced

// Strip one package's components out of a secure settings list (accessibility
// services, notification listeners), the way the desktop tool does.
async function revokeSecure(key, pkg) {
  try {
    const raw = (await sh(`settings get secure ${key}`)).trim();
    const cur = (raw && raw !== "null") ? raw : "";
    const kept = cur.split(":").filter((c) => c && !c.startsWith(pkg + "/"));
    const next = kept.join(":");
    if (next === cur) return;
    if (next) await sh(`settings put secure ${key} ${next}`);
    else await sh(`settings delete secure ${key}`);
  } catch (e) { console.warn("revoke failed", e); }
}

async function advReview() {
  const box = $("#adv-review");
  box.replaceChildren(el("p", { className: "hint" }, "Reading…"));
  const secList = async (key) => {
    const raw = (await sh(`settings get secure ${key}`)).trim();
    if (!raw || raw === "null") return [];
    return [...new Set(raw.split(":").filter((c) => c.includes("/")))];
  };
  let acc = [], notif = [], admins = [], batt = [];
  try {
    acc = await secList("enabled_accessibility_services");
    notif = await secList("enabled_notification_listeners");
    const adOut = await sh("dumpsys device_policy");
    const seen = new Set();
    for (const m of adOut.matchAll(/ComponentInfo\{([A-Za-z][\w.]+)\/([\w.$]+)\}/g))
      if (!seen.has(m[1])) { seen.add(m[1]); admins.push(`${m[1]}/${m[2]}`); }
    const bOut = await sh("dumpsys deviceidle whitelist");
    const bs = new Set();
    for (const line of bOut.split("\n")) {
      const m = line.match(/,([A-Za-z][\w.]+),/) ||
                line.match(/^(?:system|user),\s*([A-Za-z][\w.]+)/);
      if (m) bs.add(m[1]);
    }
    batt = [...bs];
  } catch (e) {
    box.replaceChildren(el("p", { className: "hint" },
      "Couldn't read the phone: " + (e?.message || e)));
    return;
  }

  const pkgOf = (c) => c.split("/")[0];
  const row = (pkg, btn) => el("div", { className: "row" },
    el("span", { className: "name" }, nameOf(pkg)),
    el("span", { className: "pkg" }, pkg), btn);
  const smallBtn = (text, danger, fn) => {
    const b = el("button", { className: `small${danger ? " danger" : ""}`,
                             type: "button" }, text);
    b.addEventListener("click", async () => { b.disabled = true; await fn(); });
    return b;
  };
  const kids = [];
  const group = (title, items, mk) => {
    kids.push(el("div", { className: "grouphead" }, `${title} (${items.length})`));
    if (!items.length) kids.push(el("p", { className: "hint" }, "None."));
    else for (const it of items) kids.push(mk(it));
  };

  group("Accessibility access", acc, (c) => row(pkgOf(c),
    smallBtn("Revoke", true, async () => {
      await revokeSecure("enabled_accessibility_services", pkgOf(c)); advReview();
    })));
  group("Notification access", notif, (c) => row(pkgOf(c),
    smallBtn("Revoke", true, async () => {
      await revokeSecure("enabled_notification_listeners", pkgOf(c)); advReview();
    })));
  group("Device admin", admins, (c) => row(pkgOf(c),
    smallBtn("Open on phone", false, async () => {
      try { await sh(`dpm remove-active-admin ${c}`); } catch {}
      try {
        await sh("am start -a android.settings.APPLICATION_DETAILS_SETTINGS " +
                 `-d package:${pkgOf(c)}`);
      } catch {}
    })));
  group("Battery unrestricted", batt, (pkg) => row(pkg,
    smallBtn("Restrict", false, async () => {
      try { await sh(`dumpsys deviceidle whitelist -${pkg}`); } catch {}
      advReview();
    })));
  box.replaceChildren(...kids);
}

async function declawFlagged() {
  const sus = suspects();
  const res = $("#adv-declaw-result");
  if (!sus.length) {
    res.hidden = false;
    res.textContent = "No flagged apps to declaw.";
    return;
  }
  const btn = $("#adv-declaw");
  btn.disabled = true;
  btn.textContent = "Declawing…";
  let ok = 0;
  for (const pkg of sus) {
    try {
      await sh(`cmd appops set ${pkg} SYSTEM_ALERT_WINDOW deny`);
      await sh(`cmd appops set ${pkg} POST_NOTIFICATION ignore`);
      await sh(`cmd appops set ${pkg} RUN_IN_BACKGROUND ignore`);
      await sh(`cmd appops set ${pkg} START_FOREGROUND deny`);
      await revokeSecure("enabled_accessibility_services", pkg);
      await revokeSecure("enabled_notification_listeners", pkg);
      try { await sh(`dumpsys deviceidle whitelist -${pkg}`); } catch {}
      await sh(`am force-stop ${pkg}`);
      ok++;
    } catch (e) { console.warn("declaw failed for", pkg, e); }
  }
  res.hidden = false;
  res.textContent = `Declawed ${ok}/${sus.length} flagged app(s). They stay ` +
    "installed but can't pop up, notify, or start in the background.";
  btn.disabled = false;
  btn.textContent = "Declaw flagged apps";
  refreshRunning();
  advReview();
}

function fmtDur(sec) {
  const d = Math.floor(sec / 86400), h = Math.floor(sec % 86400 / 3600),
        m = Math.floor(sec % 3600 / 60);
  return (d ? `${d}d ` : "") + (d || h ? `${h}h ` : "") + `${m}m`;
}

async function advDevice() {
  const box = $("#adv-device");
  box.replaceChildren(el("p", { className: "hint" }, "Reading…"));
  const rows = [];
  const add = (k, v) => rows.push(el("div", { className: "kv" },
    el("span", { className: "k" }, k), el("span", { className: "v" }, v)));
  const g = async (p) => (await sh(`getprop ${p}`)).trim();
  try {
    add("Model", `${await g("ro.product.manufacturer")} ${await g("ro.product.model")}`.trim());
    add("Marketing name", (await g("ro.product.vendor.marketname")) ||
                          (await g("ro.config.marketing_name")) || "—");
    add("Android", `${await g("ro.build.version.release")} (API ${await g("ro.build.version.sdk")})`);
    add("Security patch", (await g("ro.build.version.security_patch")) || "—");
    add("Build", await g("ro.build.display.id"));
    add("Serial", state.device?.serial || "—");
    add("Chipset", `${await g("ro.hardware")} · ${await g("ro.product.cpu.abi")}`);
    const kern = (await sh("uname -r").catch(() => "")).trim() ||
                 (await sh("cat /proc/sys/kernel/osrelease").catch(() => "")).trim();
    if (kern) add("Kernel", kern);
    const size = (await sh("wm size")).match(/Physical size:\s*(\S+)/);
    const dens = (await sh("wm density")).match(/Physical density:\s*(\d+)/);
    if (size) add("Screen", `${size[1]}${dens ? ` · ${dens[1]} dpi` : ""}`);
    const crypto = (await g("ro.crypto.state"));
    if (crypto) add("Encryption", crypto === "encrypted" ? "encrypted" : crypto);
    const su = (await sh("which su || true")).trim();
    add("Root (su)", su ? `present (${su})` : "not found");
    const bat = await sh("dumpsys battery");
    const lvl = (bat.match(/level:\s*(\d+)/) || [])[1];
    const temp = (bat.match(/temperature:\s*(\d+)/) || [])[1];
    const plugged = /AC powered: true|USB powered: true/.test(bat);
    if (lvl) add("Battery", `${lvl}%${temp ? ` · ${(temp / 10).toFixed(1)}°C` : ""}` +
                            `${plugged ? " · charging" : ""}`);
    const df = (await sh("df /data")).trim().split("\n").pop().split(/\s+/);
    if (df.length >= 5) add("Storage /data", `${df[2]} used of ${df[1]} (${df[4]} full)`);
    const up = (await sh("cat /proc/uptime")).trim().split(" ")[0];
    if (up) add("Uptime", fmtDur(+up));
    const apps = [...state.apps.values()];
    add("Apps", `${apps.filter((a) => !a.system).length} user · ` +
                `${apps.filter((a) => a.system).length} system`);
    add("Running now", `${state.running.length} user app(s)`);
    box.replaceChildren(...rows);
  } catch (e) {
    box.replaceChildren(el("p", { className: "hint" },
      "Couldn't read the phone: " + (e?.message || e)));
  }
}

function renderLog() {
  const box = $("#adv-log");
  if (!box) return;
  box.replaceChildren(...state.log.map((e) => {
    const line = el("div", { className: `logentry${e.err ? " err" : ""}` });
    const cmd = el("div", { className: "logcmd" }, "› " + e.cmd);
    if (e.n > 1) cmd.append(el("span", { className: "logn" }, ` ×${e.n}`));
    line.append(cmd);
    if (e.out) line.append(el("div", { className: "logout" }, e.out));
    return line;
  }));
  box.scrollTop = box.scrollHeight;
}

let pendingCmd = "";

function runManual() {
  const cmd = $("#adv-cmd").value.trim();
  if (!cmd || !state.adb) return;
  pendingCmd = cmd;
  $("#cmdwarn-cmd").textContent = cmd;
  $("#cmdwarn").showModal();
}

async function confirmManual() {
  $("#cmdwarn").close();
  if (!pendingCmd || !state.adb) return;
  $("#adv-cmd").value = "";
  try { await sh(pendingCmd); } catch { /* logCmd already recorded the error */ }
  pendingCmd = "";
}

function openAdv() {
  state.view = "adv";
  $("#adv-n").textContent = suspects().length;
  $("#adv-declaw-result").hidden = true;
  showStep("adv");
  $("#bar").hidden = true;
  $("#advanced").hidden = true;
  window.scrollTo(0, 0);
  renderLog();
  advDevice();
  advReview();
}

function openBasic() {
  state.view = "main";
  showStep("main");
  renderBar();
  window.scrollTo(0, 0);
}

// ------------------------------------------------------------ start

function init() {
  track("visit");
  const p = platform();
  $("#platform").textContent = { windows: "Windows PC", linux: "Linux PC",
                                 mac: "Mac" }[p] || "computer";
  if (p === "mobile") return showStep("mobile");
  // Brave ships without working WebUSB, Shields or not.
  const brave = !!navigator.brave;
  if (brave) $("#brave-note").hidden = false;
  if (brave || !manager || !window.isSecureContext) return showStep("browser");
  showStep("connect");
  loadBad();

  $("#connect").addEventListener("click", async () => {
    try {
      const d = await manager.requestDevice();
      if (d) connect(d);
    } catch (e) {
      status(e?.message || String(e), "error");
    }
  });
  $("#remove").addEventListener("click", () =>
    removeMany([...state.picked], "manual"));
  $("#opened-selectall").addEventListener("click", () => {
    const pick = [...state.opened.values()]
      .filter((o) => !o.removed && !openedIsSystem(o));
    const allSel = pick.length > 0 && pick.every((o) => state.picked.has(o.pkg));
    for (const o of pick)
      allSel ? state.picked.delete(o.pkg) : state.picked.add(o.pkg);
    renderOpened();
    renderApps();
  });
  $("#known-yes").addEventListener("click", removeKnown);
  $("#known-no").addEventListener("click", () => {
    $("#known").close();
    status("Kept them. Watching for popups — leave the phone on the home " +
           "screen.", "ok");
  });
  $("#sys").addEventListener("toggle", buildSys);

  $("#done").addEventListener("click", openHarden);
  $("#advanced").addEventListener("click", openAdv);
  $("#adv-back").addEventListener("click", openBasic);
  $("#adv-declaw").addEventListener("click", declawFlagged);
  $("#adv-run").addEventListener("click", runManual);
  $("#adv-cmd").addEventListener("keydown", (e) => {
    if (e.key === "Enter") runManual();
  });
  $("#adv-clear").addEventListener("click", () => { state.log = []; renderLog(); });
  document.addEventListener("keydown", (e) => {
    if (e.ctrlKey && e.shiftKey && e.code === "KeyE") {
      e.preventDefault();
      toggleSorter();
    }
  });
  $("#sort-find").addEventListener("input", renderSorter);
  $("#sort-showgood").addEventListener("change", renderSorter);
  $("#sort-save").addEventListener("click", saveSort);
  $("#cmdwarn-run").addEventListener("click", confirmManual);
  $("#cmdwarn-cancel").addEventListener("click", () => $("#cmdwarn").close());
  $("#harden-cancel").addEventListener("click", () => $("#harden").close());
  $("#h-all").addEventListener("change", (e) => {
    // The Chrome wipe can't be undone, so it is never ticked for you.
    for (const b of document.querySelectorAll(
        "#harden .list input[type=checkbox]:not([value=chrome])"))
      b.checked = e.target.checked;
  });
  $("#harden-apply").addEventListener("click", async () => {
    const opts = [...document.querySelectorAll("#harden .list input:checked")]
      .map((b) => b.value);
    const res = $("#harden-result");
    if (!opts.length) {
      res.hidden = false;
      res.textContent = "Nothing ticked — pick at least one guard, or Cancel.";
      return;
    }
    const btn = $("#harden-apply");
    btn.disabled = true;
    btn.textContent = "Applying…";
    const lines = await applyHarden(opts);
    track("protect");
    res.hidden = false;
    res.replaceChildren(...lines.flatMap((l) =>
      [typeof l === "string" ? document.createTextNode(l) : l, el("br")]));
    btn.textContent = "Done";
    btn.disabled = false;
    refreshRunning();
  });

  // After the first time, plugging the phone in is enough.
  navigator.usb.addEventListener("connect", () => setTimeout(autoConnect, 500));
  autoConnect();
}

init();
