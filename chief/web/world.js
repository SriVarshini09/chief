// The garden's world: a real-time 3D meadow, drawn with WebGL2 and no image files.
//
// The camera stands in the grass and looks across a lake at forest and mountains. Light comes from a
// sky model (Rayleigh and Mie scattering) and the clouds are volumes, marched through 3D noise.
// The camera never moves, so the landscape is painted once and only the living parts redraw:
//   noise  3D texture, built once
//   land   hills, forest, mountains: painted once, and again when the light changes
//   sky    atmosphere and clouds: each frame or two, at low resolution
//   blend  sky + land + lake: each frame
//   grass  instanced blades and wildflowers, bent by the wind: each frame
// If WebGL2 is missing, ChiefWorld.start returns null and the page falls back to its 2D garden.
(() => {
  "use strict";

  const QUAD = `#version 300 es
  out vec2 vUv;
  void main() { vec2 p = vec2((gl_VertexID << 1) & 2, gl_VertexID & 2); vUv = p; gl_Position = vec4(p * 2. - 1., 0., 1.); }`;

  const COMMON = `
  precision highp float; precision highp sampler3D; precision highp sampler2D;
  uniform sampler3D uNoise;
  uniform vec3 uSun, uSunColor, uAmbient, uCam, uHaze;
  uniform mat3 uBasis;
  uniform vec2 uView;
  uniform float uTime, uNight;

  vec3 rayDir(vec2 uv) { return normalize(uBasis * vec3((uv * 2. - 1.) * uView, 1.)); }
  vec2 project(vec3 d) { vec3 c = transpose(uBasis) * d; return c.xy / max(c.z, 1e-3) / uView * .5 + .5; }
  float n2(vec2 p) { return textureLod(uNoise, vec3(p, .37), 0.).r; }
  vec3 aces(vec3 x) { return clamp(x * (2.51 * x + .03) / (x * (2.43 * x + .59) + .14), 0., 1.); }
  vec3 display(vec3 c) { return pow(aces(c), vec3(1. / 2.2)); }

  // z runs away from the camera: meadow, then lake, then forested hills, then mountains
  float H(vec2 p) {
    float rolling = (n2(p * .0035) - .5) * 5. + (n2(p * .019) - .5) * 1.2;
    float shore = smoothstep(92., 132., p.y + (n2(p * .0055) - .5) * 34.);
    float h = mix(11.5 + rolling * .25 - p.y * .125, -4., shore);
    float bank = smoothstep(620., 1050., p.y + (n2(p * .0021) - .5) * 420.);
    float hills = pow(n2(p * .0011), 1.5) * 150. + n2(p * .0053) * 24. + n2(p * .03) * 5. + n2(p * .11) * 2.2;
    float peaks = smoothstep(2400., 5200., p.y) * pow(n2(p * .00032 + 3.1), 1.6) * 1500.;
    return mix(h, hills + peaks - 10., bank);
  }

  vec2 sphere(vec3 r0, vec3 rd, float sr) {
    float b = 2. * dot(rd, r0), c = dot(r0, r0) - sr * sr, d = b * b - 4. * c;
    if (d < 0.) return vec2(1e5, -1e5);
    d = sqrt(d);
    return vec2((-b - d) * .5, (-b + d) * .5);
  }

  // single scattering through the air: blue overhead, gold and red near a low sun
  vec3 atmosphere(vec3 r, vec3 sun) {
    const float R = 6371e3, TOP = 6471e3, HR = 8e3, HM = 1.2e3, G = .76, KM = 21e-6;
    const vec3 KR = vec3(5.5e-6, 13e-6, 22.4e-6);
    r.y = max(r.y, .004); r = normalize(r);
    vec3 o = vec3(0., R + 100., 0.);
    vec2 hit = sphere(o, r, TOP);
    float step1 = hit.y / 10., t = 0., odR = 0., odM = 0.;
    vec3 sumR = vec3(0.), sumM = vec3(0.);
    float mu = dot(r, sun), mm = mu * mu, gg = G * G;
    float pR = 3. / (16. * 3.14159) * (1. + mm);
    float pM = 3. / (8. * 3.14159) * ((1. - gg) * (mm + 1.)) / (pow(1. + gg - 2. * mu * G, 1.5) * (2. + gg));
    for (int i = 0; i < 10; i++) {
      vec3 p = o + r * (t + step1 * .5);
      float h = length(p) - R, dR = exp(-h / HR) * step1, dM = exp(-h / HM) * step1;
      odR += dR; odM += dM;
      float step2 = sphere(p, sun, TOP).y / 5., t2 = 0., lR = 0., lM = 0.;
      bool lit = true;
      for (int j = 0; j < 5; j++) {
        vec3 q = p + sun * (t2 + step2 * .5);
        float h2 = length(q) - R;
        if (h2 < 0.) { lit = false; break; }
        lR += exp(-h2 / HR) * step2; lM += exp(-h2 / HM) * step2; t2 += step2;
      }
      if (lit) {
        vec3 tr = exp(-(KM * 1.1 * (odM + lM) + KR * (odR + lR)));
        sumR += dR * tr; sumM += dM * tr;
      }
      t += step1;
    }
    return 22. * (pR * KR * sumR + pM * KM * sumM);
  }`;

  // ---- sky: atmosphere and volumetric clouds ----------------------------------------------------
  const SKY = `#version 300 es
  ${COMMON}
  in vec2 vUv; out vec4 frag;
  uniform float uCover;

  float cloud(vec3 p) {
    float h = clamp((p.y - 1500.) / 2400., 0., 1.);
    vec3 q = p * 2.9e-4 + vec3(uTime * .0016, 0., uTime * .0007);
    float shape = texture(uNoise, q * .36).r * .62 + texture(uNoise, q * 1.1).r * .38;
    shape *= smoothstep(0., .14, h) * smoothstep(1., .35, h);
    float d = smoothstep(uCover, uCover + .2, shape);
    if (d <= 0.) return 0.;
    return clamp(d - texture(uNoise, p * 7e-4 + uTime * .0016).g * .4 * (1. - d), 0., 1.);
  }

  void main() {
    vec3 r = rayDir(vUv);
    vec3 light = uNight > .5 ? normalize(vec3(.35, .6, .75)) : uSun;
    vec3 sky = atmosphere(r, uSun) * .9 + vec3(.0035, .006, .014) * uNight;
    sky = max(mix(vec3(dot(sky, vec3(.3, .59, .11))), sky, 1.4), 0.);
    vec3 sum = vec3(0.);
    float alpha = 0.;
    if (r.y > .012) {
      float t0 = 1500. / r.y, t1 = min(3900. / r.y, 42000.);
      const int STEPS = 22;
      float dt = (t1 - t0) / float(STEPS);
      float jitter = fract(sin(dot(gl_FragCoord.xy, vec2(12.9898, 78.233))) * 43758.5453);
      float t = t0 + dt * jitter * .45;
      float mu = dot(r, light);
      float phase = min(.55 * (1. - .64) / pow(1. + .64 - 1.6 * mu, 1.5) / 3., 2.4) + .45;
      for (int i = 0; i < STEPS && alpha < .985; i++) {
        vec3 p = r * t;
        float d = cloud(p);
        if (d > .01) {
          float shade = 0.;
          for (int k = 1; k <= 4; k++) shade += cloud(p + light * float(k * k) * 70.);
          float beer = exp(-shade * .8);
          float powder = 1. - exp(-d * 3.);
          vec3 lit = uSunColor * 2.4 * beer * phase * (.45 + powder * .8) + uAmbient * 1.15 * (.55 + .45 * clamp((p.y - 1500.) / 2400., 0., 1.));
          float a = 1. - exp(-d * dt * .0042);
          sum += lit * a * (1. - alpha);
          alpha += a * (1. - alpha);
        }
        t += dt;
      }
      float fade = smoothstep(.03, .2, r.y);
      sum *= fade * (uNight > .5 ? .2 : 1.); alpha *= fade;
    }
    frag = vec4(display(sky * (1. - alpha) + sum), alpha);
  }`;

  // ---- land: painted once for each light --------------------------------------------------------
  const LAND = `#version 300 es
  ${COMMON}
  in vec2 vUv; out vec4 frag;

  vec3 normalAt(vec2 p, float e) {
    float h = H(p);
    return normalize(vec3(h - H(p + vec2(e, 0.)), e, h - H(p + vec2(0., e))));
  }

  void main() {
    vec3 r = rayDir(vUv);
    frag = vec4(0.);
    if (r.y > .34) return;
    float t = .8, d = 0.;
    bool hit = false;
    for (int i = 0; i < 180; i++) {
      vec3 p = uCam + r * t;
      float ground = H(p.xz);
      if (ground < 0. && r.y < 0. && p.y < 0.) break;           // the lake is drawn each frame
      d = p.y - ground;
      if (d < .0012 * t) { hit = true; break; }
      t += max(d * .55, .012 * t);
      if (t > 15000.) break;
    }
    if (!hit) return;
    vec3 p = uCam + r * t;
    float e = max(.08, t * .0035);
    vec3 n = normalAt(p.xz, e);

    float patchy = n2(p.xz * .024), fine = n2(p.xz * .19), far = smoothstep(390., 760., p.z);
    vec3 meadow = mix(vec3(.022, .05, .012), vec3(.06, .095, .022), patchy);
    meadow = mix(meadow, vec3(.10, .10, .035), smoothstep(.6, .78, fine) * .4);
    vec3 forest = mix(vec3(.012, .04, .016), vec3(.04, .085, .024), smoothstep(.34, .68, n2(p.xz * .037)));
    forest *= .55 + .9 * n2(p.xz * .23) * (.6 + .8 * n2(p.xz * .9));
    vec3 alb = mix(meadow, forest, far);
    float height = p.y;
    alb = mix(alb, vec3(.12, .115, .11), smoothstep(190., 430., height + fine * 120.));
    alb = mix(alb, vec3(.92, .94, .98), smoothstep(620., 830., height + patchy * 200.) * smoothstep(.45, .85, n.y));
    alb = mix(alb, vec3(.075, .06, .035), smoothstep(.5, .0, p.y) * (1. - far));

    float sun = max(dot(n, uNight > .5 ? normalize(vec3(.35, .6, .75)) : uSun), 0.);
    vec3 col = alb * (uSunColor * sun * 1.5 + uAmbient * (.55 + .45 * n.y));
    float air = 1. - exp(-t * t * 2.5e-9 - t * 2.1e-4);
    col = mix(col, uHaze * (uNight > .5 ? .55 : 1.), clamp(air, 0., 1.));
    frag = vec4(display(col), 1.);
  }`;

  // ---- blend: sky, land and the living lake --------------------------------------------------------
  const BLEND = `#version 300 es
  ${COMMON}
  in vec2 vUv; out vec4 frag;
  uniform sampler2D uSky, uLand;
  uniform vec3 uMoon;
  uniform vec4 uRings[6];

  vec3 scene(vec2 uv) {
    vec4 land = texture(uLand, uv);
    return mix(texture(uSky, uv).rgb, land.rgb, land.a);
  }

  vec3 stars(vec3 r) {
    vec3 cell = floor(r * 170.);
    float h = fract(sin(dot(cell, vec3(127.1, 311.7, 74.7))) * 43758.5453);
    if (h < .9975) return vec3(0.);
    float twinkle = .6 + .4 * sin(uTime * (2. + h * 4.) + h * 90.);
    return vec3(.9, .94, 1.) * twinkle * smoothstep(.5, .1, length(fract(r * 170.) - .5));
  }

  void main() {
    vec3 r = rayDir(vUv);
    vec4 sky = texture(uSky, vUv);
    vec4 land = texture(uLand, vUv);
    vec3 col = sky.rgb;
    float clear = 1. - sky.a;

    if (uNight > .5) {
      col += stars(r) * clear * smoothstep(.02, .2, r.y);
      float m = dot(r, uMoon);
      col += vec3(.95, .96, .9) * smoothstep(.99982, .99990, m) * clear;
      col += vec3(.25, .3, .4) * pow(max(m, 0.), 900.) * .8;
    } else {
      float s = max(dot(r, uSun), 0.);
      col += uSunColor * (smoothstep(.99972, .99986, s) * 3. * clear + pow(s, 900.) * .55 * clear + pow(s, 40.) * .1 * (.3 + clear * .7));
    }

    if (r.y < 0. && land.a < .01) {
      float t = -uCam.y / r.y;
      vec3 p = uCam + r * t;
      vec2 q = p.xz;
      float fade = exp(-t * .0012);
      vec2 a = vec2(n2(q * .055 + uTime * vec2(.011, .017)), n2(q * .055 + vec2(.5) - uTime * vec2(.013, .009))) - .5;
      vec2 b = vec2(n2(q * .31 + uTime * vec2(-.03, .05)), n2(q * .31 + vec2(.2, .7) + uTime * vec2(.04, -.02))) - .5;
      vec2 slope = (a * .09 + b * .07) * clamp(150. / t, .03, 1.);
      for (int i = 0; i < 6; i++) {
        vec4 ring = uRings[i];
        float age = uTime - ring.z;
        if (ring.w <= 0. || age < 0. || age > 5.) continue;
        vec2 to = q - ring.xy;
        float dist = length(to);
        float wave = sin(dist * 8. - age * 13.) * exp(-abs(dist - age * 1.65) * 1.5) * exp(-age * .9) * ring.w;
        slope += to / max(dist, .001) * wave * .26;
      }
      vec3 n = normalize(vec3(slope.x, 1., slope.y));
      vec3 bounce = reflect(r, n);
      float fres = .02 + .98 * pow(1. - max(dot(-r, n), 0.), 5.);
      vec3 mirror = vec3(r.x + n.x * .35, -r.y + abs(n.z) * .05, r.z + n.z * .35);
      vec3 seen = scene(clamp(project(normalize(mirror)), vec2(.001), vec2(.999)));
      vec3 deep = pow(vec3(.012, .06, .10) + uAmbient * .06, vec3(1. / 2.2));
      col = mix(deep, seen, clamp(fres + .5, 0., 1.)) * vec3(.9, .96, 1.);
      vec3 light = uNight > .5 ? uMoon : uSun;
      float glitter = pow(max(dot(bounce, light), 0.), 720.) * (uNight > .5 ? .5 : 5.);
      col += uSunColor * glitter;
      col = mix(col, pow(uHaze, vec3(1. / 2.2)) * .92, 1. - exp(-t * 1.2e-4));
    } else if (land.a > .01) {
      vec2 w = vUv * vec2(2.2, 7.) + vec2(uTime * .006, 0.);
      float passing = smoothstep(.42, .66, n2(w));
      col = mix(col, land.rgb * (1. - passing * .2 * (1. - uNight)), land.a);
    }
    frag = vec4(col, 1.);
  }`;

  // ---- grass: one instance is one blade --------------------------------------------------------
  const PLANT = `
  ${COMMON}
  uniform float uDepth, uSeed, uTall;
  const float NEAR = .05, FAR = 500.;

  vec4 hash4(uint n) {
    uvec4 k = uvec4(n + 1U) * uvec4(1597334677U, 3812015801U, 2798796415U, 1979697957U);
    k = (k ^ (k >> 15U)) * uvec4(3812015801U, 2798796415U, 1979697957U, 1597334677U);
    k = (k ^ (k >> 13U)) * 1597334677U;
    return vec4(k) / 4294967295.;
  }

  vec2 windAt(vec2 p) {
    float gust = textureLod(uNoise, vec3(p * .013 + vec2(uTime * .045, uTime * .012), .62), 1.).r;
    float wave = dot(p, vec2(.86, .5)) * .42 - uTime * 1.9;
    float flutter = sin(wave) * .6 + sin(wave * 2.3 + 1.2) * .3;
    return vec2(.86, .5) * (flutter * .17 + (gust - .47) * 2.5);
  }

  struct Blade { vec3 root; vec2 face; vec2 lean; float tall, width, far; vec4 h; };

  Blade grow(uint id) {
    Blade b;
    b.h = hash4(id + uint(uSeed));
    float z = mix(2.4, uDepth, pow(b.h.x, 2.3));
    float wide = uView.x * 1.22 * z + 1.1;
    b.root = vec3((b.h.y * 2. - 1.) * wide + uCam.x, 0., z);
    b.root.y = H(b.root.xz);
    float a = b.h.z * 6.2832;
    b.face = vec2(cos(a), sin(a));
    b.far = z;
    float spread = 1. + z * .1;
    b.tall = (.3 + b.h.w * .46) * (1. + .3 * exp(-z * .12) + z * .012) * mix(1., 1.22, uTall);
    b.width = (.0085 + b.h.y * .0055) * spread;
    b.lean = b.face * (.18 + .46 * b.h.w) + windAt(b.root.xz) * (.7 + uTall * .35);
    if (b.root.y < .06) b.tall = 0.;
    return b;
  }

  vec3 along(Blade b, float s) {
    vec3 p = b.root;
    p.xz += b.lean * b.tall * s * s;
    p.y += b.tall * s * (1. - .22 * min(dot(b.lean, b.lean), 1.6) * s);
    return p;
  }

  vec4 clip(vec3 w) {
    vec3 c = transpose(uBasis) * (w - uCam);
    float A = (FAR + NEAR) / (FAR - NEAR), B = -2. * FAR * NEAR / (FAR - NEAR);
    return vec4(c.x / uView.x, c.y / uView.y, A * c.z + B, c.z);
  }`;

  const GRASS_V = `#version 300 es
  ${PLANT}
  out vec3 vN, vW;
  out vec4 vH;
  out float vS, vSide, vShade, vPatch;
  void main() {
    Blade b = grow(uint(gl_InstanceID));
    vPatch = n2(b.root.xz * .03) * .65 + n2(b.root.xz * .13) * .35;
    int seg = gl_VertexID / 2;
    float s = float(seg) / 4., side = float(gl_VertexID & 1) * 2. - 1.;
    if (gl_VertexID >= 8) { s = 1.; side = 0.; }
    vec3 w = along(b, s);
    vec3 tan1 = normalize(along(b, min(s + .08, 1.08)) - along(b, max(s - .08, 0.)));
    vec3 right = vec3(-b.face.y, 0., b.face.x);
    w += right * side * b.width * (1. - s * s * .9) * (1. + uTall * -.25);
    vN = normalize(cross(right, tan1) + right * side * .55);
    vW = w; vS = s; vSide = side; vH = b.h;
    vShade = mix(.66, 1., smoothstep(.36, .62, n2(b.root.xz * .0065 + vec2(uTime * .006, 0.))));
    gl_Position = b.tall > 0. ? clip(w) : vec4(2., 2., 2., 1.);
  }`;

  const GRASS_F = `#version 300 es
  ${COMMON}
  in vec3 vN, vW;
  in vec4 vH;
  in float vS, vSide, vShade, vPatch;
  out vec4 frag;
  uniform float uDew, uTall;
  void main() {
    vec3 v = normalize(vW - uCam);
    vec3 n = normalize(vN);
    if (dot(n, v) > 0.) n = -n;
    vec3 base = mix(vec3(.016, .05, .012), vec3(.05, .10, .018), vH.w);
    vec3 alb = mix(base, vec3(.12, .135, .035), vH.x * vS * vS * .85);
    alb = mix(alb, vec3(.24, .19, .07), (smoothstep(.86, .98, vH.y) + smoothstep(.56, .74, vPatch) * .55 * vH.w) * vS);
    alb *= .62 + .75 * smoothstep(.3, .66, vPatch);
    if (uTall > .5) alb = vec3(.09, .19, .05);
    alb *= 1. + (vSide * .12);
    vec3 light = uNight > .5 ? normalize(vec3(.35, .6, .75)) : uSun;
    float wrap = max(dot(n, light) * .7 + .3, 0.);
    float through = pow(max(dot(v, light), 0.), 3.) * (.3 + .7 * vS);
    float ao = mix(.12, 1., pow(vS, .75));
    vec3 col = alb * (uAmbient * .8 * ao + uSunColor * vShade * (wrap * ao + through * 1.15));
    float spec = pow(max(dot(reflect(v, n), light), 0.), 22.);
    col += uSunColor * vShade * spec * .09 * vS;
    float dew = step(.9985, fract(vH.z * 917.)) * smoothstep(.9, .97, vS) * uDew * pow(.5 + .5 * sin(uTime * 2.2 + vH.x * 80.), 4.);
    col += uSunColor * dew * 1.6;
    float z = length(vW - uCam);
    col = mix(col, uHaze * .72, 1. - exp(-z * .0065));
    frag = vec4(display(col), 1.);
  }`;

  const FLOWER_V = `#version 300 es
  ${PLANT}
  out vec2 vP;
  out vec4 vH;
  out vec3 vW;
  out float vShade;
  void main() {
    Blade b = grow(uint(gl_InstanceID));
    vec2 corner = vec2(float(gl_VertexID & 1), float((gl_VertexID >> 1) & 1)) * 2. - 1.;
    vec3 top = along(b, 1.);
    float size = (.011 + b.h.y * .012) * (1. + b.far * .035);
    vec3 right = uBasis[0], up = normalize(uBasis[1] + vec3(0., .3, 0.));
    vec3 w = top + (right * corner.x + up * corner.y) * size;
    vP = corner; vH = b.h; vW = w;
    vShade = mix(.66, 1., smoothstep(.36, .62, n2(b.root.xz * .0065 + vec2(uTime * .006, 0.))));
    gl_Position = b.tall > 0. ? clip(w) : vec4(2., 2., 2., 1.);
  }`;

  const FLOWER_F = `#version 300 es
  ${COMMON}
  in vec2 vP;
  in vec4 vH;
  in vec3 vW;
  in float vShade;
  out vec4 frag;
  void main() {
    int kind = int(floor(vH.z * 5.));
    float r = length(vP), a = atan(vP.y, vP.x) + vH.w * 6.28;
    vec3 petal, heart;
    float count, heartSize;
    if (kind == 0)      { petal = vec3(.62, .03, .012); heart = vec3(.012, .01, .012); count = 5.;  heartSize = .2; }
    else if (kind == 1) { petal = vec3(.80, .79, .70);  heart = vec3(.72, .45, .03);   count = 13.; heartSize = .3; }
    else if (kind == 2) { petal = vec3(.74, .52, .02);  heart = vec3(.55, .28, .02);   count = 5.;  heartSize = .16; }
    else if (kind == 3) { petal = vec3(.09, .14, .55);  heart = vec3(.30, .16, .52);   count = 9.;  heartSize = .16; }
    else                { petal = vec3(.60, .20, .42);  heart = vec3(.75, .55, .15);   count = 7.;  heartSize = .2; }
    float wave = abs(cos(a * count * .5));
    float reach = mix(.5, .98, pow(wave, kind == 1 ? 1.6 : .55));
    float edge = reach - r;
    if (edge < -.02) discard;
    vec3 alb = mix(petal * .6, petal, smoothstep(.1, .85, r / reach));
    alb *= .86 + .14 * sin(a * count * 3.) ;
    alb = mix(heart, alb, smoothstep(heartSize, heartSize + .07, r));
    vec3 light = uNight > .5 ? normalize(vec3(.35, .6, .75)) : uSun;
    vec3 v = normalize(vW - uCam);
    float through = pow(max(dot(v, light), 0.), 3.);
    vec3 col = alb * (uAmbient * .75 + uSunColor * vShade * (.55 + through * 1.1));
    float z = length(vW - uCam);
    col = mix(col, uHaze * .72, 1. - exp(-z * .0065));
    frag = vec4(display(col), clamp(edge * 18., 0., 1.));
  }`;

  // ---- noise ------------------------------------------------------------------------------------
  function makeNoise(gl) {
    const N = 48, data = new Uint8Array(N * N * N * 2);
    const mod = (a, n) => ((a % n) + n) % n;
    const lattice = (period) => {
      const v = new Float32Array(period * period * period);
      for (let i = 0; i < v.length; i++) v[i] = Math.random();
      return v;
    };
    const smooth = (t) => t * t * t * (t * (t * 6 - 15) + 10);
    const layers = [[3, .5], [6, .25], [12, .15], [24, .1]].map(([p, w]) => ({ p, w, v: lattice(p) }));
    const value = (x, y, z, { p, v }) => {
      const fx = x * p, fy = y * p, fz = z * p;
      const ix = Math.floor(fx), iy = Math.floor(fy), iz = Math.floor(fz);
      const u = smooth(fx - ix), w = smooth(fy - iy), t = smooth(fz - iz);
      const at = (a, b, c) => v[mod(a, p) + mod(b, p) * p + mod(c, p) * p * p];
      const mix = (a, b, k) => a + (b - a) * k;
      return mix(
        mix(mix(at(ix, iy, iz), at(ix + 1, iy, iz), u), mix(at(ix, iy + 1, iz), at(ix + 1, iy + 1, iz), u), w),
        mix(mix(at(ix, iy, iz + 1), at(ix + 1, iy, iz + 1), u), mix(at(ix, iy + 1, iz + 1), at(ix + 1, iy + 1, iz + 1), u), w), t);
    };
    const seeds = [5, 10, 20].map((p) => {
      const pts = new Float32Array(p * p * p * 3);
      for (let i = 0; i < pts.length; i++) pts[i] = Math.random();
      return { p, pts };
    });
    const worley = (x, y, z, { p, pts }) => {
      const fx = x * p, fy = y * p, fz = z * p, ix = Math.floor(fx), iy = Math.floor(fy), iz = Math.floor(fz);
      let best = 9;
      for (let a = -1; a <= 1; a++) for (let b = -1; b <= 1; b++) for (let c = -1; c <= 1; c++) {
        const i = (mod(ix + a, p) + mod(iy + b, p) * p + mod(iz + c, p) * p * p) * 3;
        const dx = ix + a + pts[i] - fx, dy = iy + b + pts[i + 1] - fy, dz = iz + c + pts[i + 2] - fz;
        best = Math.min(best, dx * dx + dy * dy + dz * dz);
      }
      return Math.min(Math.sqrt(best), 1);
    };
    const raw = new Float32Array(N * N * N);
    const centre = Math.round(.37 * N) % N;
    let low = 9, high = -9;
    for (let z = 0, i = 0; z < N; z++) for (let y = 0; y < N; y++) for (let x = 0; x < N; x++, i++) {
      let sum = 0;
      for (const layer of layers) sum += value(x / N, y / N, z / N, layer) * layer.w;
      raw[i] = sum; low = Math.min(low, sum); high = Math.max(high, sum);
      const cells = (1 - worley(x / N, y / N, z / N, seeds[0])) * .6 + (1 - worley(x / N, y / N, z / N, seeds[1])) * .28 + (1 - worley(x / N, y / N, z / N, seeds[2])) * .12;
      data[i * 2 + 1] = cells * 255;
    }
    for (let i = 0; i < raw.length; i++) data[i * 2] = (raw[i] - low) / (high - low) * 255;
    const tex = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_3D, tex);
    gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1);
    gl.texImage3D(gl.TEXTURE_3D, 0, gl.RG8, N, N, N, 0, gl.RG, gl.UNSIGNED_BYTE, data);
    gl.generateMipmap(gl.TEXTURE_3D);
    gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_MIN_FILTER, gl.LINEAR_MIPMAP_LINEAR);
    gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    for (const w of [gl.TEXTURE_WRAP_S, gl.TEXTURE_WRAP_T, gl.TEXTURE_WRAP_R]) gl.texParameteri(gl.TEXTURE_3D, w, gl.REPEAT);
    return { tex, origin: data[centre * N * N * 2] / 255 };                 // n2(0, 0): gives the ground under the camera
  }

  function program(gl, vs, fs) {
    const make = (type, src) => {
      const s = gl.createShader(type);
      gl.shaderSource(s, src); gl.compileShader(s);
      if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s));
      return s;
    };
    const p = gl.createProgram();
    gl.attachShader(p, make(gl.VERTEX_SHADER, vs)); gl.attachShader(p, make(gl.FRAGMENT_SHADER, fs));
    gl.linkProgram(p);
    if (!gl.getProgramParameter(p, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(p));
    const cache = {};
    return { p, u: (name) => (name in cache ? cache[name] : (cache[name] = gl.getUniformLocation(p, name))) };
  }

  function target(gl, w, h) {
    const tex = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, tex);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA8, w, h, 0, gl.RGBA, gl.UNSIGNED_BYTE, null);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    const fbo = gl.createFramebuffer();
    gl.bindFramebuffer(gl.FRAMEBUFFER, fbo);
    gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, tex, 0);
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    return { tex, fbo, w, h, free() { gl.deleteTexture(tex); gl.deleteFramebuffer(fbo); } };
  }

  // ---- the light for each hour ------------------------------------------------------------------
  const mix3 = (a, b, k) => a.map((v, i) => v + (b[i] - v) * k);
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
  const norm = (v) => { const l = Math.hypot(...v); return v.map((x) => x / l); };

  function lightFor(hour) {
    const day = (hour - 6) / 13.5;                                  // sunrise 6:00, sunset 19:30
    const night = hour < 5.5 || hour >= 20 ? 1 : 0;
    const up = night ? -.4 : Math.sin(Math.PI * clamp(day, -.03, 1.03)) * .56;
    const az = (hour - 12.5) / 6.75 * .78;
    const sun = norm([Math.sin(az) * Math.cos(up), Math.sin(up), Math.cos(az) * Math.cos(up)]);
    const warm = clamp(1 - up / .3, 0, 1);
    const dim = clamp(up / .05 + .35, 0, 1);
    const sunColor = night ? [.20, .25, .40] : mix3([1.5, 1.4, 1.2], [1.65, .82, .34], warm).map((v) => v * dim);
    const ambient = night ? [.028, .042, .085] : mix3([.46, .60, .82], [.40, .33, .34], warm).map((v) => v * (.35 + .65 * dim));
    const haze = night ? [.022, .034, .07] : mix3([.62, .74, .86], [1.0, .62, .38], warm).map((v) => v * (.4 + .6 * dim));
    return { sun, night, sunColor, ambient, haze, dew: hour >= 5 && hour < 10.5 ? 1 : .15 };
  }

  // ---- start ---------------------------------------------------------------------------------------
  function start(canvas, options) {
    const gl = canvas.getContext("webgl2", { antialias: true, alpha: false, powerPreference: "high-performance", preserveDrawingBuffer: !!options.still });
    if (!gl) return null;
    let sky, blend, land, grass, flower, noise, camY;
    try {
      sky = program(gl, QUAD, SKY); blend = program(gl, QUAD, BLEND); land = program(gl, QUAD, LAND);
      grass = program(gl, GRASS_V, GRASS_F); flower = program(gl, FLOWER_V, FLOWER_F);
      const made = makeNoise(gl);
      noise = made.tex;
      camY = 11.5 + (made.origin - .5) * 6.2 * .25 + 1.15;
    } catch (error) {
      console.error("Garden world could not start:", error.message);
      return null;
    }

    const empty = gl.createVertexArray();
    gl.bindVertexArray(empty);
    const small = () => innerWidth < 700;
    const TIERS = [
      { scale: 1.5, sky: .4, every: 2, blades: 220000, flowers: 1300 },
      { scale: 1, sky: .3, every: 3, blades: 110000, flowers: 800 },
      { scale: .8, sky: .24, every: 4, blades: 45000, flowers: 360 },
    ];
    let tier = small() ? 1 : 0;
    let hour = options.hour, light = lightFor(hour), W = 0, H = 0, skyTarget = null, landTarget = null, frameNo = 0;
    let dirty = true;
    const rings = new Float32Array(24);
    let ringAt = 0;

    const pitch = .2;
    const cosP = Math.cos(pitch), sinP = Math.sin(pitch);
    const basis = new Float32Array([1, 0, 0, 0, cosP, -sinP, 0, sinP, cosP]);     // columns: right, up, forward
    let view = [1, 1];
    const TAN = Math.tan(31 * Math.PI / 180);

    function common(prog) {
      gl.useProgram(prog.p);
      gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_3D, noise);
      gl.uniform1i(prog.u("uNoise"), 0);
      gl.uniform3fv(prog.u("uSun"), light.sun);
      gl.uniform3fv(prog.u("uSunColor"), light.sunColor);
      gl.uniform3fv(prog.u("uAmbient"), light.ambient);
      gl.uniform3fv(prog.u("uHaze"), light.haze);
      gl.uniform3f(prog.u("uCam"), 0, camY, 0);
      gl.uniformMatrix3fv(prog.u("uBasis"), false, basis);
      gl.uniform2fv(prog.u("uView"), view);
      gl.uniform1f(prog.u("uNight"), light.night);
    }

    function resize() {
      const t = TIERS[tier];
      const dpr = Math.min(devicePixelRatio || 1, t.scale);
      W = Math.round(canvas.clientWidth * dpr); H = Math.round(canvas.clientHeight * dpr);
      canvas.width = W; canvas.height = H;
      view = [TAN * W / H, TAN];
      if (skyTarget) skyTarget.free();
      if (landTarget) landTarget.free();
      skyTarget = target(gl, Math.max(160, Math.round(W * t.sky)), Math.max(120, Math.round(H * t.sky)));
      landTarget = target(gl, W, H);
      dirty = true;
    }

    function paintLand() {
      gl.bindFramebuffer(gl.FRAMEBUFFER, landTarget.fbo);
      gl.viewport(0, 0, landTarget.w, landTarget.h);
      gl.disable(gl.DEPTH_TEST);
      gl.clearColor(0, 0, 0, 0); gl.clear(gl.COLOR_BUFFER_BIT);
      common(land);
      gl.drawArrays(gl.TRIANGLES, 0, 3);
      gl.bindFramebuffer(gl.FRAMEBUFFER, null);
      dirty = false;
    }

    function paintSky(time) {
      gl.bindFramebuffer(gl.FRAMEBUFFER, skyTarget.fbo);
      gl.viewport(0, 0, skyTarget.w, skyTarget.h);
      gl.disable(gl.DEPTH_TEST);
      common(sky);
      gl.uniform1f(sky.u("uTime"), time);
      gl.uniform1f(sky.u("uCover"), options.cover || .55);
      gl.drawArrays(gl.TRIANGLES, 0, 3);
      gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    }

    function blendAll(time) {
      gl.viewport(0, 0, W, H);
      gl.disable(gl.DEPTH_TEST);
      gl.depthMask(false);
      common(blend);
      gl.uniform1f(blend.u("uTime"), time);
      gl.uniform3fv(blend.u("uMoon"), norm([.33, .36, .87]));
      gl.uniform4fv(blend.u("uRings"), rings);
      gl.activeTexture(gl.TEXTURE1); gl.bindTexture(gl.TEXTURE_2D, skyTarget.tex); gl.uniform1i(blend.u("uSky"), 1);
      gl.activeTexture(gl.TEXTURE2); gl.bindTexture(gl.TEXTURE_2D, landTarget.tex); gl.uniform1i(blend.u("uLand"), 2);
      gl.drawArrays(gl.TRIANGLES, 0, 3);
    }

    function plants(time) {
      const t = TIERS[tier];
      gl.enable(gl.DEPTH_TEST); gl.depthMask(true); gl.depthFunc(gl.LEQUAL);
      gl.clear(gl.DEPTH_BUFFER_BIT);
      const setup = (prog, seed, tall, depth) => {
        common(prog);
        gl.uniform1f(prog.u("uTime"), time);
        gl.uniform1f(prog.u("uDepth"), depth);
        gl.uniform1f(prog.u("uSeed"), seed);
        gl.uniform1f(prog.u("uTall"), tall);
      };
      setup(grass, 0, 0, 104);
      gl.uniform1f(grass.u("uDew"), light.dew);
      gl.drawArraysInstanced(gl.TRIANGLE_STRIP, 0, 9, t.blades);
      setup(grass, 7000000, 1, 70);
      gl.uniform1f(grass.u("uDew"), 0);
      gl.drawArraysInstanced(gl.TRIANGLE_STRIP, 0, 9, t.flowers);
      setup(flower, 7000000, 1, 70);
      gl.enable(gl.SAMPLE_ALPHA_TO_COVERAGE);
      gl.drawArraysInstanced(gl.TRIANGLE_STRIP, 0, 4, t.flowers);
      gl.disable(gl.SAMPLE_ALPHA_TO_COVERAGE);
    }

    function frame(time) {
      const t = TIERS[tier];
      if (dirty) { paintLand(); paintSky(time); }
      else if (frameNo % t.every === 0) paintSky(time);
      frameNo++;
      blendAll(time);
      plants(time);
    }

    function setHour(next) { hour = next; light = lightFor(hour); dirty = true; }

    function lighter() {
      if (tier >= TIERS.length - 1) return false;
      tier++;
      resize();
      return true;
    }

    // a screen point on the lake, or null if that point is not water
    function ripple(x, y, power, time) {
      const u = x / canvas.clientWidth, v = 1 - y / canvas.clientHeight;
      const cx = (u * 2 - 1) * view[0], cy = (v * 2 - 1) * view[1];
      const dy = cy * cosP + sinP, dz = -cy * sinP + cosP;
      if (dy >= 0) return false;
      const t = -camY / dy, wz = dz * t;
      if (wz < 96) return false;
      rings.set([cx * t, wz, time, power], ringAt * 4);
      ringAt = (ringAt + 1) % 6;
      return true;
    }

    // where the land meets the lake on screen, as a share of the height
    const horizon = () => .5 + (sinP / cosP) / view[1] * .5;

    resize();
    return { frame, resize, setHour, lighter, ripple, horizon, get tier() { return tier; } };
  }

  window.ChiefWorld = { start };
})();
