// Chief's voice, shared by the dashboard and the phone page.
//
// The browser's own voices are free and stay on the device: we pick the most natural one this browser
// has (Edge's "Online (Natural)" voices, Google's in Chrome, Samantha on Apple), speak a little slower
// and softer than the default, and change the delivery with Chief's mood. When the server has a natural
// voice (CHIEF_TTS_API_KEY, see docs/voice.md), the words go to /tts and come back as audio; any problem
// there and the browser's voice says them instead. Nothing here loads anything from outside the page.
(() => {
  "use strict";
  const KEY = "chief-voice-name";            // a browser voice's name, or NATURAL; empty means "the best one"
  const NATURAL = "chief-natural";
  const MAX = 1000;                          // the server's limit for one reply
  const TEST_LINE = "Hi, it's me, Chief. Thank you for letting me help today. This is how I'll sound when we talk.";
  const has = "speechSynthesis" in window;

  // Gentler than the browser default, with a short breath between sentences. Happy is a little brighter,
  // worried a little slower and lower, calm is in between.
  const TONES = {
    calm: { rate: .93, pitch: 1.03, pause: 420 },
    happy: { rate: .99, pitch: 1.1, pause: 340 },
    worried: { rate: .87, pitch: .96, pause: 520 },
  };
  const moodOf = (m) => (m === "happy" || m === "worried" ? m : "calm");
  const tone = (mood) => TONES[moodOf(mood)];
  const lang = () => (navigator.language || "en-US").toLowerCase();
  const tag = (v) => (v.lang || "").toLowerCase().replace("_", "-");

  // Higher is more human. Neural voices sound far better than the old system ones.
  function score(v) {
    const n = v.name;
    let s = 0;
    if (/natural|neural/i.test(n)) s += 60;
    if (/online/i.test(n)) s += 20;
    if (/\b(aria|jenny|ava|emma|sonia|libby|michelle|natasha|clara|neerja)\b/i.test(n)) s += 25;
    if (/google (uk english female|us english)/i.test(n)) s += 35;
    else if (/^google\b/i.test(n)) s += 15;
    if (/\b(samantha|karen|moira|tessa|serena|ava|zoe|allison|susan)\b/i.test(n)) s += 30;  // Apple's better voices
    if (/enhanced|premium|siri/i.test(n)) s += 20;
    if (/\b(zira|hazel|susan|catherine|heera)\b/i.test(n)) s += 5;                      // among the old voices, a softer one
    if (/espeak|compact/i.test(n)) s -= 10;
    if (tag(v) === lang()) s += 10;
    return s;
  }

  // This device's voices for the browser's language (English if it has none), most natural first.
  function voices() {
    if (!has) return [];
    const all = speechSynthesis.getVoices(), base = lang().split("-")[0];
    const mine = all.filter((v) => tag(v).startsWith(base));
    const list = mine.length ? mine : all.filter((v) => tag(v).startsWith("en"));
    return list.map((v, i) => [score(v), -i, v]).sort((a, b) => b[0] - a[0] || b[1] - a[1]).map((x) => x[2]);
  }

  function saved() { try { return localStorage.getItem(KEY) || ""; } catch (_) { return ""; } }
  function save(name) { try { if (name) localStorage.setItem(KEY, name); else localStorage.removeItem(KEY); } catch (_) {} }

  // The voice you picked if this device still has it, otherwise the most natural one.
  function browserVoice() {
    const list = voices(), name = saved();
    return list.find((v) => v.name === name) || list[0] || null;
  }

  function utterance(text, mood) {
    const u = new SpeechSynthesisUtterance(text), t = tone(mood), v = browserVoice();
    if (v) { u.voice = v; u.lang = v.lang; } else u.lang = navigator.language || "en-US";
    u.rate = t.rate; u.pitch = t.pitch;
    return u;
  }

  // How long to breathe after a piece of speech: longer after a full stop than after a comma.
  const pause = (mood, part) => (/[.!?]["')]*\s*$/.test(part || "") ? tone(mood).pause : 200);
  const sentences = (text) => text.match(/[^.!?\n]+[.!?]*["')]*\s*|\n+/g) || [text];

  // Browser voice, one sentence at a time with a short pause between: for pages without their own loop.
  let run = 0, timer = 0;
  function sayInBrowser(text, mood, done) {
    if (!has) { if (done) done(); return; }
    speechSynthesis.cancel();
    clearTimeout(timer);
    const mine = ++run, parts = sentences(text).filter((p) => p.trim());
    const next = (i) => {
      if (mine !== run) return;
      if (i >= parts.length) { if (done) done(); return; }
      const u = utterance(parts[i].trim(), mood);
      u.onend = u.onerror = () => { if (mine === run) timer = setTimeout(() => next(i + 1), pause(mood, parts[i])); };
      speechSynthesis.speak(u);
    };
    next(0);
  }

  // ---- the natural voice from the server ------------------------------------------------------------
  let natural = false;                       // set from /status
  const useNatural = () => natural && (saved() === "" || saved() === NATURAL);
  function setNatural(on) {
    if (natural === !!on) return;
    natural = !!on;
    pickers.forEach(fill);
  }

  let ctx = null, current = null;
  // Call from a click or key press: browsers only let a page make sound after you touch it.
  function unlock() {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return;
    if (!ctx) { try { ctx = new Ctx(); } catch (_) { return; } }
    if (ctx.state === "suspended") ctx.resume().catch(() => {});
  }

  function stop() {
    run++;
    clearTimeout(timer);
    if (current) { const c = current; current = null; c.cancel(); }
  }

  // Plays `text` in the natural voice. Resolves true when it finished, false when stopped; rejects on any
  // problem (no key, slow network, an audio error) so the caller can use the browser voice instead.
  // onLevel gets the loudness (0 to 1) every frame, for the mouth; onProgress the share played so far.
  async function play(text, mood, opts = {}) {
    stop();
    unlock();
    if (!ctx || !text || text.length > MAX) throw new Error("natural voice unavailable");
    let cancelled = false, source = null, frame = 0;
    const abort = new AbortController();
    const late = setTimeout(() => abort.abort(), opts.timeout || 8000);
    const me = { cancel() { cancelled = true; abort.abort(); cancelAnimationFrame(frame); try { if (source) source.stop(); } catch (_) {} } };
    current = me;
    try {
      const res = await fetch("/tts", {
        method: "POST", signal: abort.signal,
        headers: { "Content-Type": "application/json", "X-Chief-Token": opts.token || "" },
        body: JSON.stringify({ text, mood: moodOf(mood) }),
      });
      if (!res.ok) throw new Error("natural voice: " + res.status);
      const bytes = await res.arrayBuffer();
      clearTimeout(late);
      if (cancelled) return false;
      const buffer = await new Promise((ok, bad) => ctx.decodeAudioData(bytes, ok, bad));  // the callback form works in older Safari
      if (cancelled) return false;
      if (ctx.state !== "running") {
        await Promise.race([ctx.resume().catch(() => {}), new Promise((r) => setTimeout(r, 300))]);
        if (ctx.state !== "running") throw new Error("audio is not allowed yet");
      }
      source = ctx.createBufferSource();
      source.buffer = buffer;
      const ear = ctx.createAnalyser();
      ear.fftSize = 1024; ear.smoothingTimeConstant = .4;
      source.connect(ear); ear.connect(ctx.destination);
      const wave = new Uint8Array(ear.fftSize);
      return await new Promise((done) => {
        const began = ctx.currentTime;
        source.onended = () => {
          cancelAnimationFrame(frame);
          if (current === me) current = null;
          if (opts.onLevel) opts.onLevel(0);
          done(!cancelled);
        };
        const listen = () => {
          ear.getByteTimeDomainData(wave);
          let sum = 0;
          for (let i = 0; i < wave.length; i++) { const x = (wave[i] - 128) / 128; sum += x * x; }
          const level = Math.min(1, Math.max(0, Math.sqrt(sum / wave.length) * 5 - .04));
          if (opts.onLevel) opts.onLevel(level);
          if (opts.onProgress) opts.onProgress(Math.min(1, (ctx.currentTime - began) / buffer.duration));
          frame = requestAnimationFrame(listen);
        };
        source.start();
        if (opts.onStart) opts.onStart(buffer.duration);
        listen();
      });
    } catch (e) {
      if (cancelled) return false;
      throw e;
    } finally {
      clearTimeout(late);
      if (current === me && !source) current = null;
    }
  }

  // Natural voice when the server has one and you have not picked a browser voice; otherwise the browser's.
  async function say(text, mood, opts = {}) {
    if (useNatural()) {
      try { await play(text, mood, opts); if (opts.done) opts.done(); return; } catch (_) {}
    }
    sayInBrowser(text, mood, opts.done);
  }

  // ---- the picker ---------------------------------------------------------------------------------
  const pickers = [];
  const label = (v) => v.name.replace(/^(Microsoft|Google)\s+/, "").replace(/\s+-\s+.*$/, "") + " (" + v.lang + ")";
  function option(value, text) { const o = document.createElement("option"); o.value = value; o.textContent = text; return o; }
  function fill(select) {
    const list = voices().slice(0, 40), options = [];
    if (natural) options.push(option(NATURAL, "Chief's natural voice (recommended)"));
    list.forEach((v, i) => options.push(option(v.name, label(v) + (i === 0 && !natural ? " (recommended)" : ""))));
    if (!options.length) options.push(option("", "This device's default voice"));
    select.replaceChildren(...options);
    select.value = useNatural() ? NATURAL : (browserVoice() || { name: "" }).name;
  }
  // Fills a <select> with the voices, most natural first, and remembers what you choose.
  function picker(select, onPick) {
    pickers.push(select);
    fill(select);
    select.addEventListener("change", () => { save(select.value); pickers.forEach(fill); if (onPick) onPick(select.value); });
  }
  if (has) {
    const refresh = () => pickers.forEach(fill);
    if (speechSynthesis.addEventListener) speechSynthesis.addEventListener("voiceschanged", refresh);
    else speechSynthesis.onvoiceschanged = refresh;
  }

  window.ChiefVoice = {
    MAX, NATURAL, TEST_LINE, voices, browserVoice, utterance, tone, pause, sentences, moodOf,
    sayInBrowser, say, play, stop, unlock, setNatural, useNatural, picker,
  };
})();
