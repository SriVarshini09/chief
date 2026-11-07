/* Walking guidance for both pages (index.html and dashboard.html).

   After a walking directions answer the page shows a big "Guide me" button. While guiding, this script watches
   your position and sends it to /guide/tick every few seconds; the server (chief/guide.py) decides what to say
   and finds a new route when you go the wrong way. Speech goes through the page's own speak(). The screen is
   kept awake where the Wake Lock API exists. GPS and voice only: no camera.

   The page calls:
     ChiefGuide.init({ api, speak, ensureLocation })   once
     ChiefGuide.offer(proposal, box)                     after showing an answer: adds Guide me to walking directions
     ChiefGuide.handle(proposal)                         after an answer: "guide me there" / "stop guiding" said aloud
   For testing in a desktop browser: ChiefGuide.start(proposal, {lat, lon}) then ChiefGuide.simulate(lat, lon). */
(() => {
  "use strict";
  const G = { active: false, simulated: false, last: null };
  let cfg = { api: null, speak: () => {}, ensureLocation: async () => true };
  let watchId = null, wake = null, sending = false, queued = null, bar = null, lastProposal = null;

  const CSS = `
  #chiefGuide { position: fixed; left: 12px; right: 12px; bottom: 12px; z-index: 2147483000; max-width: 640px; margin: 0 auto;
    background: rgba(12, 16, 24, .94); color: #fff; border-radius: 22px; padding: 18px; box-shadow: 0 10px 40px rgba(0,0,0,.45);
    font: 600 18px/1.35 system-ui, -apple-system, "Segoe UI", sans-serif; }
  #chiefGuide[hidden] { display: none; }
  #chiefGuide .cg-warn { font-size: 15px; font-weight: 700; color: #ffd166; letter-spacing: .02em; margin-bottom: 8px; }
  #chiefGuide .cg-now { font-size: 26px; line-height: 1.25; min-height: 2.5em; margin-bottom: 14px; }
  #chiefGuide .cg-stop { display: block; width: 100%; min-height: 64px; border: 0; border-radius: 16px; background: #e5484d;
    color: #fff; font: 800 24px/1 system-ui, sans-serif; cursor: pointer; }
  .cg-offer { display: block; width: 100%; min-height: 60px; margin: 12px 0 4px; border: 0; border-radius: 16px; background: #2f9e44;
    color: #fff; font: 800 22px/1 system-ui, sans-serif; cursor: pointer; }
  .cg-offer:disabled { opacity: .6; }`;

  function ui() {
    if (bar) return bar;
    const style = document.createElement("style");
    style.textContent = CSS;
    document.head.append(style);
    bar = document.createElement("section");
    bar.id = "chiefGuide"; bar.hidden = true;
    bar.setAttribute("role", "region"); bar.setAttribute("aria-label", "Walking guidance");
    const warn = document.createElement("div"); warn.className = "cg-warn"; warn.textContent = "Watch the road, not the screen";
    const now = document.createElement("div"); now.className = "cg-now"; now.setAttribute("aria-live", "polite");
    const stop = document.createElement("button"); stop.type = "button"; stop.className = "cg-stop"; stop.textContent = "Stop";
    stop.addEventListener("click", () => { if (G.active) { G.stop(); } else { hideBar(); } });
    bar.append(warn, now, stop);
    document.body.append(bar);
    return bar;
  }
  function show(text) { if (text) ui().querySelector(".cg-now").textContent = text; }
  function showBar() { ui().hidden = false; ui().querySelector(".cg-stop").textContent = "Stop"; document.body.classList.add("guiding"); }
  function hideBar() { if (bar) bar.hidden = true; document.body.classList.remove("guiding"); }

  // ---- keep the screen on while guiding (released when the page is hidden; asked again when it's back)
  async function lockScreen() {
    if (wake || !("wakeLock" in navigator) || document.visibilityState !== "visible") return;
    try {
      wake = await navigator.wakeLock.request("screen");
      wake.addEventListener("release", () => { wake = null; });
    } catch (_) { wake = null; }  // not allowed here (battery saver, an old browser): guidance still works
  }
  function unlockScreen() { if (wake) { wake.release().catch(() => {}); wake = null; } }
  document.addEventListener("visibilitychange", () => { if (G.active && document.visibilityState === "visible") lockScreen(); });

  function post(path, body) {
    return cfg.api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
  }
  function say(text) { if (text) { try { cfg.speak(text); } catch (_) {} } }

  function handleOut(out) {
    G.last = out;
    say(out.say);
    show(out.show || out.say);
    if (!out.active) finish();
  }

  async function position(lat, lon, accuracy) {
    if (!G.active) return;
    if (sending) { queued = { lat, lon, accuracy }; return; }  // only the newest position matters
    sending = true;
    try { handleOut(await post("/guide/tick", { lat, lon, accuracy })); }
    catch (_) { show("Lost the connection. Still trying…"); }
    finally {
      sending = false;
      if (queued) { const q = queued; queued = null; position(q.lat, q.lon, q.accuracy); }
    }
  }

  function watch() {
    if (watchId !== null || G.simulated || !navigator.geolocation) return;
    watchId = navigator.geolocation.watchPosition(
      (p) => position(p.coords.latitude, p.coords.longitude, p.coords.accuracy),
      () => show("I can't see your location. Check that location is on."),
      { enableHighAccuracy: true, maximumAge: 2000, timeout: 30000 },
    );
  }
  function unwatch() { if (watchId !== null) navigator.geolocation.clearWatch(watchId); watchId = null; }

  function here() {
    return new Promise((resolve, reject) => {
      if (!navigator.geolocation) return reject(new Error("This browser can't share a location."));
      navigator.geolocation.getCurrentPosition(
        (p) => resolve({ lat: p.coords.latitude, lon: p.coords.longitude, accuracy: p.coords.accuracy }),
        () => reject(new Error("I couldn't see where you are. Check that location is on.")),
        { enableHighAccuracy: true, timeout: 15000, maximumAge: 5000 },
      );
    });
  }

  function destination(p) {  // the arrive step of the directions answer: the server matches it to the place's name
    const items = (p && p.items) || [];
    const end = items.filter((it) => typeof it.lat === "number" && typeof it.lon === "number").pop();
    return end ? { lat: end.lat, lon: end.lon } : null;
  }

  G.init = (c) => { cfg = { ...cfg, ...c }; };

  // Already started on the server ("guide me there" said aloud): just follow.
  G.follow = (out) => {
    G.active = true;
    showBar();
    show((out && (out.show || out.say)) || "Starting…");
    lockScreen();
    watch();
  };

  G.start = async (p, at) => {
    p = p || lastProposal;
    try {
      if (!at && !(await cfg.ensureLocation())) return;
      showBar(); show("Finding where you are…");
      if (at) G.simulated = true;
      const pos = at || await here();
      const out = await post("/guide/start", { lat: pos.lat, lon: pos.lon, accuracy: pos.accuracy, to: destination(p) });
      G.last = out;
      if (!out.active) { say(out.say); show(out.say); finish(); return; }
      G.follow(out);
      say(out.say);
    } catch (e) {
      show(e.message || "Guidance didn't start."); say(e.message);
      G.active = false; unwatch(); unlockScreen();
      if (bar) bar.querySelector(".cg-stop").textContent = "Close";
    }
  };

  function finish() {  // arrived, or the server stopped: keep the last words on screen with a Close button
    G.active = false; G.simulated = false;
    unwatch(); unlockScreen();
    if (bar) bar.querySelector(".cg-stop").textContent = "Close";
  }

  G.stop = async (quiet) => {
    const was = G.active;
    G.active = false; G.simulated = false;
    unwatch(); unlockScreen(); hideBar();
    if (!quiet && was) {
      try { const out = await post("/guide/stop", {}); say(out.say); } catch (_) {}
    }
  };

  G.simulate = (lat, lon, accuracy) => position(lat, lon, accuracy == null ? 5 : accuracy);

  // A big Guide me button under a walking directions answer.
  G.offer = (p, box) => {
    document.querySelectorAll(".cg-offer").forEach((b) => b.remove());
    const items = (p && p.items) || [];
    if (!p || p.skill !== "directions" || !items.length || items[0].guide !== true || !box) return;
    lastProposal = p;
    ui();
    const btn = document.createElement("button");
    btn.type = "button"; btn.className = "cg-offer"; btn.textContent = "Guide me";
    btn.addEventListener("click", async () => { btn.disabled = true; await G.start(p); btn.disabled = false; });
    box.append(btn);
  };

  // What was said aloud: "guide me there" started a guide on the server; "stop guiding" ended it.
  G.handle = (p) => {
    if (!p) return false;
    if (p.needs === "guide") { G.simulated = false; G.follow({ say: p.summary, show: p.summary }); return true; }
    if (p.needs === "guide_stop") { G.stop(true); return true; }
    return false;
  };

  window.ChiefGuide = G;
})();
