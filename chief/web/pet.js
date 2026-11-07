// Chief's body: a soft white kitten, drawn live in 3D with WebGL2 and no image files.
//
// The shape is a signed distance field, so its parts melt into each other like soft clay. `pet.frame`
// runs a small behaviour loop (idle, walk, sleep, and the moods the dashboard sets) and passes the pose
// to the shader as numbers. Only what the kitten really does is decided here: what it says, and when
// it is thinking or waiting, comes from the dashboard, which takes that from Chief's real state.
// If WebGL2 is missing, ChiefPet.start returns null and the page keeps its drawn character.
(() => {
  "use strict";

  const QUAD = `#version 300 es
  out vec2 vUv;
  void main() { vec2 p = vec2((gl_VertexID << 1) & 2, gl_VertexID & 2); vUv = p * 2. - 1.; gl_Position = vec4(vUv, 0., 1.); }`;

  const SHADE = `#version 300 es
  precision highp float;
  in vec2 vUv; out vec4 frag;
  uniform float uPixel, uCover, uLean, uYaw, uHeadYaw, uHeadPitch, uHeadRoll, uRoll, uBob, uStride, uBlink, uMouth, uSmile, uEar, uTail, uArmL, uArmR, uSit, uLookX, uLookY, uSquash, uNight;

  float smin(float a, float b, float k) { float h = clamp(.5 + .5 * (b - a) / k, 0., 1.); return mix(b, a, h) - k * h * (1. - h); }
  float ell(vec3 p, vec3 r) { float k0 = length(p / r), k1 = length(p / (r * r)); return k0 * (k0 - 1.) / k1; }
  float cap(vec3 p, vec3 a, vec3 b, float r) { vec3 pa = p - a, ba = b - a; return length(pa - ba * clamp(dot(pa, ba) / dot(ba, ba), 0., 1.)) - r; }
  float rcone(vec3 p, vec3 a, vec3 b, float r1, float r2) {
    vec3 ba = b - a; float l2 = dot(ba, ba), rr = r1 - r2, a2 = l2 - rr * rr, il2 = 1. / l2;
    vec3 pa = p - a; float y = dot(pa, ba), z = y - l2, d = pa * l2 - ba * y == vec3(0.) ? 0. : dot(pa * l2 - ba * y, pa * l2 - ba * y);
    float y2 = y * y * l2, z2 = z * z * l2, k = sign(rr) * rr * rr * d;
    if (sign(z) * a2 * z2 > k) return sqrt(dot(pa - ba, pa - ba)) - r2;
    if (sign(y) * a2 * y2 < k) return sqrt(dot(pa, pa)) - r1;
    return (sqrt(d * a2 * il2) + y * rr) * il2 - r1;
  }
  mat2 rot(float a) { float c = cos(a), s = sin(a); return mat2(c, -s, s, c); }

  vec2 hit(vec2 a, vec2 b) { return a.x < b.x ? a : b; }

  // x: distance, y: material (0 fur, 1 black, 2 inner ear)
  vec2 map(vec3 p) {
    p.y -= uBob;
    p.y = (p.y + .72) / uSquash - .72;
    p.xz *= sqrt(uSquash);
    p.xz *= rot(uYaw);
    p.y += .72; p.xy *= rot(uRoll); p.yz *= rot(uLean); p.y -= .72;

    float sit = uSit;
    vec3 q = p; q.y += sit * .13;
    float d = ell(q - vec3(0., .0, 0.), vec3(.42, .5 - sit * .05, .38 + sit * .07));

    for (int i = 0; i < 2; i++) {
      float s = i == 0 ? -1. : 1.;
      float swing = s * uStride;
      float lift = max(0., sin(acos(clamp(swing, -1., 1.)))) * abs(uStride) * .13;
      vec3 foot = vec3(s * .21, -.57 + lift + sit * .1, .25 * swing + sit * .34);
      d = smin(d, ell(q - foot, vec3(.2, .155, .245)), .07);
      float raise = i == 0 ? uArmL : uArmR;
      vec3 shoulder = vec3(s * .38, .24, .02);
      vec3 down = shoulder + vec3(s * .15, -.32, -swing * .17);
      vec3 up = shoulder + vec3(s * .34, .36, .16);
      vec3 paw = mix(mix(down, up, raise), vec3(s * .43, 1.13, 1.0), uCover);
      d = smin(d, rcone(q, shoulder, paw, .135, .125), .08);
    }

    vec3 t1 = vec3(.1, -.28, -.3), t2 = vec3(.5 + uTail * .06, -.42, -.34), t3 = vec3(.82 + uTail * .14, -.2, -.3), t4 = vec3(.8 + uTail * .3, .14, -.26);
    d = smin(d, cap(q, t1, t2, .1), .08);
    d = smin(d, cap(q, t2, t3, .105), .05);
    d = smin(d, rcone(q, t3, t4, .115, .1), .05);

    vec3 h = q - vec3(0., .5, .01);
    h.xz *= rot(uHeadYaw); h.yz *= rot(uHeadPitch); h.xy *= rot(uHeadRoll);
    h.y -= .62;
    float head = ell(h, vec3(1.06, .82, .88));
    for (int i = 0; i < 2; i++) {
      float s = i == 0 ? -1. : 1.;
      vec3 e = h; e.z *= 1.25;
      vec3 base = vec3(s * .6, .42, -.04), tip = vec3(s * (.98 + uEar * .34), 1.2 - uEar * .55, -.12);
      head = smin(head, rcone(e, base, tip, .47, .1), .1);
    }
    d = smin(d, head, .05);
    vec2 res = vec2(d, 0.);

    if (length(h) < 1.3 && h.z > .2) {
      float face = 1e3;
      float blink = 1. - uBlink * .93;
      for (int i = 0; i < 2; i++) {
        float s = i == 0 ? -1. : 1.;
        vec3 centre = vec3(s * .46 + uLookX * .03, -.1 + uLookY * .025, .768);
        if (uSmile > .5) {
          vec3 c = h - centre;
          face = min(face, cap(c, vec3(-.1, -.03, .03), vec3(0., .05, .05), .024));
          face = min(face, cap(c, vec3(.1, -.03, .03), vec3(0., .05, .05), .024));
        } else {
          face = min(face, ell(h - centre, vec3(.125, max(.018, .15 * blink), .06)));
        }
      }
      vec3 m = h - vec3(0., -.2, .845);
      if (uMouth > .12) {
        face = min(face, ell(m - vec3(0., -.03, 0.), vec3(.075 + uMouth * .05, .03 + uMouth * .085, .05)));
      } else {
        face = min(face, cap(m, vec3(-.125, .02, -.012), vec3(-.062, -.03, 0.), .024));
        face = min(face, cap(m, vec3(-.062, -.03, 0.), vec3(0., .014, .006), .024));
        face = min(face, cap(m, vec3(0., .014, .006), vec3(.062, -.03, 0.), .024));
        face = min(face, cap(m, vec3(.062, -.03, 0.), vec3(.125, .02, -.012), .024));
      }
      res = hit(res, vec2(face, 1.));
    }
    return res;
  }

  vec3 normal(vec3 p) {
    const vec2 e = vec2(1., -1.) * .0015;
    return normalize(e.xyy * map(p + e.xyy).x + e.yyx * map(p + e.yyx).x + e.yxy * map(p + e.yxy).x + e.xxx * map(p + e.xxx).x);
  }

  float softShadow(vec3 ro, vec3 rd) {
    float res = 1., t = .03;
    for (int i = 0; i < 12 && t < 2.2; i++) {
      float h = map(ro + rd * t).x;
      res = min(res, 7. * h / t);
      t += clamp(h, .03, .3);
      if (res < .01) break;
    }
    return clamp(res, 0., 1.);
  }

  float occlusion(vec3 p, vec3 n) {
    float occ = 0., w = 1.;
    for (int i = 1; i <= 4; i++) {
      float h = .05 * float(i) * float(i) * .55;
      occ += (h - map(p + n * h).x) * w;
      w *= .62;
    }
    return clamp(1. - occ * 1.5, 0., 1.);
  }

  vec3 aces(vec3 x) { return clamp(x * (2.51 * x + .03) / (x * (2.43 * x + .59) + .14), 0., 1.); }

  void main() {
    vec2 uv = vUv * 2.42 + vec2(0., 1.26);
    vec3 ro = vec3(uv.x, uv.y + .12, 9.);
    vec3 rd = normalize(vec3(-uv.x * .03, -uv.y * .03 - .02, -1.));

    float t = 6., closest = 1e3, at = t;
    vec2 m = vec2(1., 0.);
    for (int i = 0; i < 56 && t < 12.; i++) {
      m = map(ro + rd * t);
      if (m.x < closest) { closest = m.x; at = t; }
      if (m.x < .0012) break;
      t += m.x * .92;
    }
    float alpha = 1. - smoothstep(0., uPixel * 1.2, closest);
    vec2 ground = (uv - vec2(.05, -.97)) / vec2(.98, .14);
    float shade = .42 * exp(-dot(ground, ground) * 1.5) / (1. + uBob * 5.);
    if (alpha <= .002) { frag = vec4(0., 0., 0., shade); return; }

    vec3 p = ro + rd * at;
    vec2 mat = map(p);
    vec3 n = normal(p);
    vec3 v = -rd;

    vec3 keyDir = normalize(vec3(-.5, .8, .62));
    vec3 keyCol = uNight > .5 ? vec3(.5, .58, .85) : vec3(1.28, 1.22, 1.13);
    vec3 skyCol = uNight > .5 ? vec3(.16, .2, .36) : vec3(.56, .62, .72);
    vec3 col;
    if (mat.y > .5) {
      vec3 h = normalize(keyDir + v);
      float spec = pow(max(dot(n, h), 0.), 90.);
      float rim = pow(1. - max(dot(n, v), 0.), 4.);
      col = vec3(.012) + vec3(.9) * spec + vec3(.05) * rim + skyCol * .05 * max(n.y, 0.);
    } else {
      vec3 alb = vec3(.965, .96, .95);
      float shadow = softShadow(p + n * .02, keyDir);
      float ao = occlusion(p, n);
      float wrap = clamp((dot(n, keyDir) + .32) / 1.32, 0., 1.);
      float sky = .5 + .5 * n.y;
      float bounce = clamp(.5 - .5 * n.y, 0., 1.);
      float fres = pow(1. - max(dot(n, v), 0.), 3.);
      col = alb * (keyCol * wrap * mix(.32, 1., shadow) * .92 + skyCol * sky * ao * .72 + vec3(.34, .36, .3) * bounce * ao * .5);
      col += vec3(1., .96, .92) * .05 * (1. - wrap) * ao;             // light travels a little way through soft material
      col += skyCol * fres * .1 * ao;
      col *= mix(.72, 1., ao);
    }
    col = pow(aces(col * 1.02), vec3(1. / 2.2));
    frag = vec4(col * alpha, alpha + shade * (1. - alpha));
  }`;

  function compile(gl, vs, fs) {
    const part = (type, src) => {
      const s = gl.createShader(type);
      gl.shaderSource(s, src); gl.compileShader(s);
      if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s));
      return s;
    };
    const p = gl.createProgram();
    gl.attachShader(p, part(gl.VERTEX_SHADER, vs)); gl.attachShader(p, part(gl.FRAGMENT_SHADER, fs));
    gl.linkProgram(p);
    if (!gl.getProgramParameter(p, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(p));
    return p;
  }

  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
  const near = (now, target, rate, dt) => now + (target - now) * (1 - Math.exp(-rate * dt));
  const rand = (a, b) => a + Math.random() * (b - a);

  function start(canvas, options = {}) {
    const gl = canvas.getContext("webgl2", { alpha: true, premultipliedAlpha: true, antialias: false, powerPreference: "low-power" });
    if (!gl) return null;
    let program;
    try {
      program = compile(gl, QUAD, SHADE);
    } catch (error) {
      console.error("Chief's kitten could not start:", error.message);
      return null;
    }
    gl.useProgram(program);
    gl.bindVertexArray(gl.createVertexArray());
    gl.clearColor(0, 0, 0, 0);
    const uniforms = {};
    const u = (name) => (name in uniforms ? uniforms[name] : (uniforms[name] = gl.getUniformLocation(program, name)));

    const host = canvas.parentElement;
    let scale = 1, slow = 0, frames = 0;
    function resize() {
      const size = canvas.clientWidth, dpr = Math.min(devicePixelRatio || 1, 1.6) * scale;
      canvas.width = canvas.height = Math.max(64, Math.round(size * dpr));
      gl.viewport(0, 0, canvas.width, canvas.height);
      gl.uniform1f(u("uPixel"), 3.5 / canvas.width);
    }
    resize();

    const pose = { cover: 0, lean: 0, yaw: .22, headYaw: 0, headPitch: 0, headRoll: 0, roll: 0, bob: 0, stride: 0, blink: 0, mouth: 0, ear: 0, tail: 0, armL: 0, armR: 0, sit: 0, lookX: 0, lookY: 0, squash: 1 };
    let mood = "idle", moodIs = "idle", moodIs2 = "idle", talking = false, x = null, blinkAt = 2, blinking = 0, idleTime = 0, asleep = false;
    let plan = [], act = null, goal = null, arrived = false, nextRoutine = 6, yawGoal = .22, velocity = 0, speed = 0, phase = 0;
    let lift = 0, hopV = 0, waving = 0, stretch = 0, sitting = 0, gesture = 0, nod = 0, earFlick = 0, breath = 0;
    let gazeX = 0, gazeY = 0, gazeHold = 1, pointerAt = -9, clock = 0, sounds = [], now = 0;
    let lookAtX = 0, lookAtY = 0, wave = 0, zAt = 0, night = options.night ? 1 : 0;
    let y = null, guiding = false, pointing = 0, shy = false, shrugging = 0, landed = 0;

    function width() { return Math.max(0, host.clientWidth - canvas.clientWidth); }
    const size = () => canvas.clientWidth;

    // What makes it feel alive is intention and timing, so it does things for a reason. Each routine
    // is a short plan of actions (walk here, turn to you, wave), and it finishes one before the next.
    const WALK_SPEED = 54, STRIDE_LENGTH = 62;                  // px per second, px per full step cycle

    // Where it lives: the page says where its patch of ground is. x and y are the page position of its feet.
    function homeSpot() { return options.home ? options.home() : { x: Math.min(width() * .1, 70), y: 300 }; }
    function home() { return homeSpot().x; }
    function area() { return [Math.max(0, home() - 30), Math.min(width(), home() + 330)]; }
    const atHome = () => Math.abs(y - homeSpot().y) < 6;

    // Going somewhere on the page: it walks along the level it is on, and jumps to reach a different one.
    function goTo(tx, ty, then) {
      guiding = true;
      wake();
      tx = clamp(tx, 0, width());
      const steps = [];
      if (Math.abs(ty - y) > 8) {
        const run = clamp(tx - x, -150, 150);
        if (Math.abs(run) > 20) steps.push({ walk: x + run });
        steps.push({ jump: [tx, ty] });
      } else steps.push({ walk: tx });
      steps.push({ face: "you" });
      if (then) steps.push({ call: then });
      plan = steps; act = null; goal = null; pointing = 0;
    }

    function goHome(then) {
      const spot = homeSpot();
      goTo(spot.x + 30, spot.y, () => { guiding = false; nextRoutine = rand(4, 8); if (then) then(); });
    }

    function wake() {
      idleTime = 0;
      if (asleep) { asleep = false; plan = [{ stand: 1 }, { face: "you" }, { stretch: 1.6 }]; act = null; }
    }

    function hop(height = .22) { hopV = Math.sqrt(height) * 4.2; }

    canvas.addEventListener("pointerdown", (e) => {
      e.stopPropagation();
      wake();
      plan = [{ face: "you" }, { wave: 1.2 }];
      act = null;
      if (options.onPet) options.onPet();
    });

    function chooseRoutine() {
      const [low, high] = area(), spot = rand(low, high), roll = Math.random();
      if (roll < .46) plan = [{ walk: spot }, { pause: rand(.5, 1.2) }, { face: "you" }];
      else if (roll < .62) plan = [{ face: "you" }, { look: [rand(-.8, .8), 1], time: rand(1.6, 2.8) }];   // the blossoms overhead
      else if (roll < .76) plan = [{ face: "you" }, { look: [.3, -1], time: rand(1.2, 2.2) }];              // the cards below
      else if (roll < .88) plan = [{ face: "you" }, { stretch: 2.1 }];
      else plan = [{ face: "you" }, { sit: rand(5, 9) }, { stand: 1 }];
      nextRoutine = rand(2.5, 7);
    }

    function startAct() {
      act = plan.shift() || null;
      if (act) act.t = 0;
    }

    function runAct(dt) {
      if (!act) startAct();
      if (!act) return;
      act.t += dt;
      if ("walk" in act) {
        goal = clamp(act.walk, -canvas.clientWidth * 1.2, width());
        if (Math.abs(x - goal) < 1.2 && speed < 4) { goal = null; act = null; }
      } else if ("jump" in act) {
        // crouch, spring in an arc, land soft: the way a small animal gets onto something
        if (!act.from) {
          act.from = [x, y];
          const dx = act.jump[0] - x, dy = act.jump[1] - y;
          act.time = .52 + Math.min(.45, Math.abs(dy) / 1500);
          act.peak = 70 + Math.abs(dx) * .12 + Math.max(0, -dy) * .25 + Math.max(0, dy) * .3;
          yawGoal = Math.sign(dx || 1) * .9;
        }
        const crouch = .16, p = clamp((act.t - crouch) / act.time, 0, 1);
        if (act.t < crouch) pose.squash = near(pose.squash, .84, 30, dt);
        else {
          const [fx, fy] = act.from, [tx, ty] = act.jump;
          x = fx + (tx - fx) * p;
          y = fy + (ty - fy) * p - act.peak * 4 * p * (1 - p);
          pose.squash = near(pose.squash, 1.07, 20, dt);
          if (p >= 1) { x = tx; y = ty; landed = 1; act = null; }
        }
      } else if ("point" in act) {
        pointing = 1;
        act = null;
      } else if ("face" in act) {
        yawGoal = act.face === "you" ? (mood === "waiting" ? .5 : .22) : act.face;
        if (Math.abs(pose.yaw - yawGoal) < .1) act = null;
      } else if ("pause" in act) {
        if (act.t >= act.pause) act = null;
      } else if ("wave" in act) {
        waving = 1;
        if (act.t >= act.wave) { waving = 0; act = null; }
      } else if ("look" in act) {
        gazeX = act.look[0]; gazeY = act.look[1]; gazeHold = .4;
        if (act.t >= act.time) act = null;
      } else if ("stretch" in act) {
        const p = act.t / act.stretch;
        stretch = Math.sin(Math.min(p, 1) * Math.PI);
        if (p >= 1) { stretch = 0; act = null; }
      } else if ("sit" in act) {
        sitting = 1;
        if (act.t >= act.sit) act = null;
      } else if ("stand" in act) {
        sitting = 0;
        if (pose.sit < .08) act = null;
      } else if ("call" in act) {
        const call = act.call;
        act = null;
        call();
      } else act = null;
    }

    function update(dt, time) {
      if (x === null) {
        x = options.still ? home() : home() - 70;
        y = homeSpot().y;
        if (options.still) { arrived = true; }
        else plan = [{ walk: home() + 40 }, { face: "you" }, { call() { arrived = true; if (options.onArrive) options.onArrive(); } }, { wave: 1.7 }];
      }
      if (arrived) x = Math.min(x, width());
      const jumping = act && "jump" in act;
      if (!guiding && !jumping) y = homeSpot().y;                                        // the page can move its ground
      idleTime += dt;
      if (mood !== "idle" || talking) idleTime = Math.min(idleTime, 20);
      if (!asleep && arrived && mood === "idle" && !talking && idleTime > (options.sleepAfter || 70)) {
        asleep = true; plan = []; act = null; goal = null;
      }

      // what it should be doing right now
      if (arrived && !asleep && guiding) {
        moodIs = mood;                                                                    // on its way somewhere: the page leads
      } else if (arrived && !asleep) {
        if (talking) {
          plan = plan.filter((step) => "wave" in step);                                 // it can wave and talk, but it stops to speak
          if (act && !("wave" in act)) act = null;
          goal = null;
          yawGoal = .22;
        } else if (mood === "waiting" && moodIs !== "waiting") {
          plan = [{ walk: area()[1] - 20 }, { face: "you" }];
          act = null;
        } else if (mood === "working" && !plan.length && !act) {
          const [low, high] = area();
          plan = [{ walk: x < (low + high) / 2 ? high : low }];
        } else if (mood === "worried" || mood === "thinking") {
          if (moodIs !== mood) { plan = [{ face: "you" }]; act = null; goal = null; }
        } else if (mood === "idle" && !plan.length && !act) {
          nextRoutine -= dt;
          if (nextRoutine <= 0) chooseRoutine();
        }
        moodIs = mood;
      }
      if (!asleep) runAct(dt);

      // walking: turn first, then move, and the feet advance exactly as far as the body does
      const fast = mood === "working" ? 1.45 : 1;
      let wanted = 0;
      if (goal !== null) {
        const gap = goal - x, dir = Math.sign(gap) || 1;
        yawGoal = dir * .98;
        if (Math.abs(pose.yaw - yawGoal) < .42) wanted = dir * Math.min(WALK_SPEED * fast, Math.abs(gap) * 2.6);
      }
      velocity = near(velocity, wanted, 6, dt);
      if (Math.abs(velocity) < .4 && wanted === 0) velocity = 0;
      x += velocity * dt;
      speed = Math.abs(velocity);
      phase += speed * dt / STRIDE_LENGTH * Math.PI * 2;
      const gait = clamp(speed / WALK_SPEED, 0, 1);
      if (goal === null && gait < .05) phase = Math.round(phase / Math.PI) * Math.PI;   // both feet settle together

      hopV -= 9.5 * dt;
      lift = Math.max(0, lift + hopV * dt);
      if (lift === 0) hopV = 0;
      if (mood === "happy" && moodIs2 !== "happy") hop();
      moodIs2 = mood;

      pose.yaw = near(pose.yaw, yawGoal, 5.5, dt);
      pose.stride = Math.sin(phase) * gait;
      pose.roll = near(pose.roll, Math.sin(phase) * gait * .05, 16, dt);                // weight shifts side to side
      pose.lean = near(pose.lean, gait * .08 + (mood === "worried" ? .05 : 0), 5, dt);  // and it leans into the walk
      pose.bob = Math.abs(Math.cos(phase)) * gait * .035 + lift;
      breath = Math.sin(time * 1.35);
      if (!jumping) pose.squash = near(pose.squash, 1 + breath * .009 - Math.sin(landed * Math.PI) * .13, 18, dt);
      landed = Math.max(0, landed - dt * 4);

      // the eyes move first, in quick glances, then the head follows them
      gazeHold -= dt;
      const pointerNear = time - pointerAt < 2.2;
      if (gazeHold <= 0) {
        gazeHold = rand(1.1, 3.4);
        if (!pointerNear) { gazeX = rand(-.5, .5); gazeY = rand(-.2, .4); }
      }
      if (pointerNear) { gazeX = lookAtX; gazeY = lookAtY; }
      let targetX = gazeX, targetY = gazeY;
      if (goal !== null) { targetX = Math.sign(yawGoal) * .3; targetY = 0; }
      if (talking) { targetX *= .25; targetY = targetY * .25 + .1; }
      if (mood === "thinking") { targetX = .5; targetY = .9; }
      pose.lookX = near(pose.lookX, targetX, 30, dt);
      pose.lookY = near(pose.lookY, targetY, 30, dt);

      nod = near(nod, 0, 7, dt);
      let hy = goal !== null ? -Math.sign(yawGoal) * .42 * (1 - gait * .6) : targetX * .38;
      let hp = -targetY * .18 + nod, hr = 0;
      if (goal === null) hy -= (pose.yaw - yawGoal) * .9;                               // the head arrives before the body
      if (mood === "thinking") { hr = .14; }
      if (mood === "worried") { hp += .22; }
      if (shy) { hp = .14; hy = -.1; }
      shrugging = Math.max(0, shrugging - dt);
      if (shrugging > 0) { hr = .2 * Math.sin((1 - shrugging / 1.4) * Math.PI); }
      if (asleep) { hp = .3 + Math.sin(time * 1.1) * .04; hr = .1; hy = -.2; }
      pose.headYaw = near(pose.headYaw, hy, 7, dt);
      pose.headPitch = near(pose.headPitch, hp, 8, dt);
      pose.headRoll = near(pose.headRoll, hr + Math.sin(phase) * gait * .025, 7, dt);

      // the rest of it
      pose.sit = near(pose.sit, asleep || sitting ? 1 : 0, 3, dt);
      wave += dt * 8;
      gesture = near(gesture, 0, 2.6, dt);
      const shrug = shrugging > 0 ? .5 * Math.sin((1 - shrugging / 1.4) * Math.PI) : 0;
      const armRight = waving ? .84 + Math.sin(wave) * .15 : pointing ? .62 : mood === "thinking" ? .54 : mood === "waiting" ? .42 : mood === "happy" ? .9 : Math.max(stretch, gesture * .36, shrug);
      const armLeft = mood === "happy" ? .9 : Math.max(stretch, gesture * .12, shrug);
      pose.armR = near(pose.armR, armRight, 7, dt);
      pose.armL = near(pose.armL, armLeft, 7, dt);
      pose.cover = near(pose.cover, shy ? 1 : 0, 9, dt);                                  // paws over its eyes
      pose.ear = near(pose.ear, mood === "worried" ? 1 : asleep ? .55 : earFlick > 0 ? .16 : 0, 16, dt);
      earFlick -= dt;
      if (earFlick < -rand(3, 8)) earFlick = .09;
      pose.tail = Math.sin(time * (mood === "happy" ? 7 : 1.4 + gait * 1.6)) * (asleep ? .12 : mood === "happy" ? .9 : .55);

      // blinks come in their own rhythm, and often at the end of a sentence
      blinkAt -= dt;
      if (blinkAt <= 0) { blinking = 1; blinkAt = rand(2.2, 6); if (Math.random() < .16) blinkAt = .26; }
      blinking = Math.max(0, blinking - dt * 7);
      const shut = asleep ? 1 : Math.max(Math.sin(Math.min(blinking, 1) * Math.PI), stretch > .6 ? .75 : 0);
      pose.blink = near(pose.blink, shut, 32, dt);

      // the mouth follows syllables, closing between them
      clock += dt;
      while (sounds.length && sounds[0].end < clock) sounds.shift();
      let open = stretch * .95;
      const sound = sounds[0];
      if (sound && clock >= sound.at) open = Math.max(open, Math.sin((clock - sound.at) / (sound.end - sound.at) * Math.PI) * sound.size);
      pose.mouth = near(pose.mouth, open, 34, dt);

      if (options.onMove) options.onMove(x, y, size());
      if (asleep && options.onSnore) { zAt -= dt; if (zAt <= 0) { zAt = 2.6; options.onSnore(); } }
    }

    function draw() {
      const set = (name, value) => gl.uniform1f(u(name), value);
      set("uYaw", pose.yaw); set("uHeadYaw", pose.headYaw); set("uHeadPitch", pose.headPitch); set("uHeadRoll", pose.headRoll);
      set("uLean", -pose.lean); set("uRoll", pose.roll); set("uBob", pose.bob); set("uStride", pose.stride); set("uBlink", pose.blink);
      set("uMouth", pose.mouth); set("uSmile", mood === "happy" ? 1 : 0); set("uEar", pose.ear); set("uTail", pose.tail);
      set("uArmL", pose.armL); set("uArmR", pose.armR); set("uSit", pose.sit); set("uLookX", pose.lookX); set("uLookY", pose.lookY);
      set("uSquash", pose.squash); set("uNight", night); set("uCover", pose.cover);
      gl.clear(gl.COLOR_BUFFER_BIT);
      gl.drawArrays(gl.TRIANGLES, 0, 3);
      const top = (y === null ? 0 : y) - size() * .96;                                     // its feet sit near the canvas bottom
      canvas.style.transform = `translate(${(x === null ? 0 : x).toFixed(1)}px, ${top.toFixed(1)}px)`;
    }

    return {
      frame(time, dt) {
        now = time;
        update(dt, time);
        draw();
        if (dt > .001 && dt < .25 && scale > .6) {
          frames++;
          if (dt > .034) slow++;
          if (frames === 90) {
            if (slow > 55) { scale = Math.max(.6, scale - .2); resize(); }
            frames = slow = 0;
          }
        }
      },
      mood(next) { mood = next; if (next !== "idle") wake(); },
      talk(on) { talking = on; if (on) wake(); },
      ready() { return arrived; },
      // Each word is broken into syllables, so the mouth opens and closes the way speech does.
      word(text, ms) {
        const groups = text.toLowerCase().match(/[aeiouy]+/g) || ["a"];
        const each = ms / groups.length / 1000;
        let at = Math.max(clock, sounds.length ? sounds[sounds.length - 1].end : 0);
        for (const vowels of groups) {
          const size = /[ao]/.test(vowels) ? rand(.8, 1) : /[eu]/.test(vowels) ? rand(.55, .75) : rand(.4, .6);
          sounds.push({ at, end: at + each * .92, size });
          at += each;
        }
      },
      // A new sentence: a small movement of the hand and a nod, the way people talk.
      sentence() { gesture = rand(.6, 1); nod = -.05; if (Math.random() < .5) blinkAt = Math.min(blinkAt, .12); },
      pauseSpeech() { sounds = []; nod = .07; },
      look(px, py) {
        const box = canvas.getBoundingClientRect();
        lookAtX = clamp((px - box.left - box.width / 2) / 320, -1, 1);
        lookAtY = clamp((box.top + box.height * .3 - py) / 320, -1, 1);
        pointerAt = now;
      },
      wake, hop, resize, goTo, goHome,
      point(on) { pointing = on ? 1 : 0; },
      shy(on) { if (on) wake(); shy = !!on; },
      shrug() { wake(); shrugging = 1.4; },
      where() { return { x, y, size: size() }; },
      busy() { return guiding; },
      setNight(on) { night = on ? 1 : 0; },
    };
  }

  window.ChiefPet = { start };
})();
