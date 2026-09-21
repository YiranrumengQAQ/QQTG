// Headless smoke test of the admin SPA using jsdom against a running bridge.
const { JSDOM, VirtualConsole } = require("jsdom");
const BASE = process.argv[2] || "http://127.0.0.1:18321";
const USER = process.argv[3] || "owner", PASS = process.argv[4] || "password123";
const errors = [];
const vc = new VirtualConsole();
vc.on("jsdomError", (e) => errors.push("jsdomError: " + (e && e.message || e)));
vc.on("error", (...a) => errors.push("console.error: " + a.join(" ")));

(async () => {
  const html = await (await fetch(BASE + "/")).text();
  const cookies = {};
  const dom = new JSDOM(html, { url: BASE + "/", runScripts: "dangerously", resources: "usable", pretendToBeVisual: true, virtualConsole: vc });
  const w = dom.window;
  w.fetch = async (url, opts = {}) => {
    const headers = Object.assign({}, opts.headers || {});
    const ck = Object.entries(cookies).map(([k, v]) => `${k}=${v}`).join("; ");
    if (ck) headers["Cookie"] = ck;
    const res = await fetch(new URL(url, BASE).href, { method: opts.method || "GET", headers, body: opts.body, redirect: "manual" });
    for (const sc of (res.headers.getSetCookie ? res.headers.getSetCookie() : [])) {
      const [kv] = sc.split(";"); const [k, v] = kv.split("="); if (v === "" || /Max-Age=0/i.test(sc)) delete cookies[k]; else cookies[k] = v;
    }
    const text = await res.text();
    return { ok: res.ok, status: res.status, json: async () => JSON.parse(text), text: async () => text, headers: res.headers };
  };
  w.addEventListener("error", (e) => errors.push("window.error: " + (e.error && e.error.stack || e.message)));
  w.addEventListener("unhandledrejection", (e) => errors.push("unhandledrejection: " + (e.reason && e.reason.stack || e.reason)));
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const waitFor = async (sel, ms = 8000) => { const t = Date.now(); while (Date.now() - t < ms) { const el = w.document.querySelector(sel); if (el) return el; await sleep(100); } throw new Error("timeout waiting for " + sel + " | body: " + w.document.body.textContent.slice(0, 200)); };

  const form = await waitFor("form#f");
  const boot = w.document.body.textContent;
  if (form.querySelector("[name=setup_token]")) throw new Error("bridge still needs setup; run e2e first");
  form.querySelector("[name=username]").value = USER; form.querySelector("[name=password]").value = PASS;
  form.dispatchEvent(new w.Event("submit", { bubbles: true, cancelable: true }));
  await waitFor(".sidebar", 10000);
  console.log("login OK ->", w.document.querySelector("h1").textContent);
  const routes = ["#/", "#/connections", "#/chats", "#/bridges", "#/bridges/new", "#/bridges/1", "#/messages", "#/messages?status=failed", "#/logs", "#/logs?category=message", "#/settings"];
  for (const r of routes) {
    w.location.hash = r;
    w.dispatchEvent(new w.Event("hashchange"));
    await sleep(1200);
    const h1 = w.document.querySelector("h1");
    const txt = w.document.body.textContent;
    if (/加载失败|无法连接/.test(txt)) throw new Error("view error at " + r + ": " + txt.slice(0, 300));
    console.log(`  ${r.padEnd(26)} -> ${h1 ? h1.textContent.trim() : "(no h1)"}  cards=${w.document.querySelectorAll(".card").length}`);
  }
  // wizard: pick chats and go to step 3
  w.location.hash = "#/bridges/new"; w.dispatchEvent(new w.Event("hashchange")); await sleep(1200);
  let pick = w.document.querySelector("[data-pick]"); if (!pick) throw new Error("no qq chat choices");
  pick.click(); await sleep(100); w.document.querySelector("[data-next]").click(); await sleep(200);
  pick = w.document.querySelector("[data-pick]"); if (!pick) throw new Error("no tg chat choices");
  pick.click(); await sleep(100); w.document.querySelector("[data-next]").click(); await sleep(200);
  if (!w.document.querySelector("#w-name")) throw new Error("wizard step 3 not rendered");
  console.log("  wizard step 3 OK:", w.document.querySelector("#w-name").value);
  // bridge detail: toggle + save form roundtrip
  w.location.hash = "#/bridges/1"; w.dispatchEvent(new w.Event("hashchange")); await sleep(1200);
  const fb = w.document.querySelector("#fb"); if (!fb) throw new Error("bridge form missing");
  fb.dispatchEvent(new w.Event("submit", { bubbles: true, cancelable: true })); await sleep(1200);
  const toast = w.document.querySelector(".toast"); console.log("  bridge save toast:", toast ? toast.textContent : "(none)");
  // settings save roundtrip
  w.location.hash = "#/settings"; w.dispatchEvent(new w.Event("hashchange")); await sleep(1500);
  const fs = w.document.querySelector("#fs"); fs.dispatchEvent(new w.Event("submit", { bubbles: true, cancelable: true })); await sleep(1200);
  console.log("  settings save toast:", (w.document.querySelector(".toast") || {}).textContent);
  // diagnose modal
  w.document.querySelector("[data-diag]").click(); await sleep(6000);
  console.log("  diagnose rows:", w.document.querySelectorAll("#modal-root .ci").length);
  const real = errors.filter((e) => !/Could not parse CSS stylesheet|Not implemented: HTMLFormElement.prototype.requestSubmit|Not implemented: navigation/.test(e));
  if (real.length) { console.log("JS ERRORS:\n" + real.join("\n")); process.exit(1); }
  console.log("UI SMOKE OK (no JS errors)");
  process.exit(0);
})().catch((e) => { console.error("UI SMOKE FAILED:", e.message); console.error(errors.join("\n")); process.exit(1); });
