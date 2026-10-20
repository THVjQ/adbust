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
  opened: new Map(), // pkg -> {pkg,kind,activity,first,last,count}
  picked: new Set(),
  busy: false,
  watcher: null,
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

const sh = (cmd) => state.adb.subprocess.noneProtocol.spawnWaitText(cmd);

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
async function loadIcons(pkgs) {
  const want = [...new Set(pkgs)]
    .filter((p) => state.apps.get(p) && !state.apps.get(p).icon);
  if (!want.length) return false;
  let got = false;
  try {
    await pushHelper();
    for (let i = 0; i < want.length; i += 40) {
      const chunk = want.slice(i, i + 40);          // pkg names are [\w.]+, safe unquoted
      const out = await sh(`CLASSPATH=${HELPER} app_process / AppList --icons ` +
                           chunk.join(" "));
      for (const line of out.split("\n")) {
        const t = line.indexOf("\t");
        if (t < 0) continue;
        const pkg = line.slice(0, t), b64 = line.slice(t + 1).replace(/\r$/, "");
        const a = state.apps.get(pkg);
        if (a && b64) { a.icon = "data:image/png;base64," + b64; got = true; }
      }
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
          "connection. Close adb, Android Studio, scrcpy or Smart Switch, " +
          "unplug the phone and plug it back in.");
      }
      throw e;
    }
    status("Look at the phone and tap Allow (tick \"Always allow\").", "ask");
    const transport = await AdbDaemonTransport.authenticate({
      serial: device.serial, connection: conn, credentialStore: credentials,
    });
    state.adb = new Adb(transport);
    state.device = device;
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
    renderApps();
    showStep("main");
    // Icons load in the background so they don't hold up the list.
    loadIcons([...apps.values()].filter((a) => a.launcher || !a.system)
      .map((a) => a.pkg)).then((ok) => { if (ok) { renderApps(); renderOpened(); } });
    const known = [...apps.values()]
      .filter((a) => !a.system && state.bad.has(a.pkg));
    if (known.length) askKnown(known);
    else status("No apps from the shared database. Watching for popups — " +
                "leave the phone on the home screen.", "ok");
    startWatch();
    return true;
  } catch (e) {
    console.error(e);
    status(e?.message || String(e), "error");
    showStep("connect");
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
         `removed by ${b.reports} ${b.reports === 1 ? "person" : "people"}`)));
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
      // Only apps caught opening by themselves, or already in the shared
      // database, are reported. Tidying up other apps stays private.
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
  // Popup culprits and database hits go back into the shared database.
  await report(removed.filter((r) => r.how !== "manual"));
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
  if (bad) tags.push(el("span", { className: "tag bad" },
                        `removed by ${bad.reports}`));
  if (!a.launcher && !a.system) tags.push(el("span", { className: "tag" },
                                             "no icon"));
  const ico = a.icon
    ? el("img", { className: "ico", src: a.icon, alt: "", loading: "lazy" })
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
    loadIcons(missing).then((ok) => {
      state.iconBusy = false;
      if (ok) { renderOpened(); renderApps(); }
    });
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

function renderBar() {
  for (const p of state.picked) if (!state.apps.has(p)) state.picked.delete(p);
  const n = state.picked.size;
  const btn = $("#remove");
  btn.disabled = n === 0 || state.busy;
  btn.textContent = n ? `Uninstall ${n} selected app${n > 1 ? "s" : ""}`
                      : "Tick apps to uninstall";
  $("#bar").hidden = !state.adb && n === 0;
}

// ------------------------------------------------------------ start

function init() {
  const p = platform();
  $("#platform").textContent = { windows: "Windows", linux: "Linux",
                                 mac: "Mac", mobile: "phone",
                                 other: "" }[p];
  if (p === "mac") return showStep("mac");
  if (p === "mobile") return showStep("mobile");
  if (!manager || !window.isSecureContext) return showStep("browser");
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

  // After the first time, plugging the phone in is enough.
  navigator.usb.addEventListener("connect", () => setTimeout(autoConnect, 500));
  autoConnect();
}

init();
