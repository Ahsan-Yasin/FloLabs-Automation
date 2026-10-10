/* Highlight Cutter: the home page's "before and after" timeline in 3D.

   motion.js imports this module as the timeline nears the screen and draws
   the flat bars instead if WebGL or the import fails. The flat bars stay in
   the page as the blueprint: every block takes its position and width from
   the bar it stands for, so the scene lines up with the labels and timecodes
   at any width, and it takes its colours from the same CSS tokens (light and
   dark). The recording builds up, the cuts fade back, copies of the kept parts
   travel down into the final video and close up, and the highlights reel
   drops in at its start. It renders only while something changes. */
import {
  AmbientLight, CanvasTexture, DirectionalLight, ExtrudeGeometry, Group, MathUtils, Mesh,
  MeshBasicMaterial, MeshLambertMaterial, OrthographicCamera, PlaneGeometry, RepeatWrapping,
  SRGBColorSpace, Scene, Shape, WebGLRenderer,
} from "../vendor/three-0.186.1.min.js";

const TILT = MathUtils.degToRad(30); // the camera looks down 30 degrees: tops and fronts show
const HEIGHT = 22;                   // block height, px, at full size
const DEPTH = 40;                    // block depth, px, at full size (narrow screens scale both down)
const ROOM = 30;                     // stage space above the first row, for blocks in flight
const BLUR = 9;                      // contact shadow softness, px

export function mount(fig, { gsap, countTimecode }) {
  const reels = fig.querySelector(".reels");
  const rows = Array.from(fig.querySelectorAll(".reel-row"));
  if (!reels || rows.length !== 2 || !rows[1].querySelector(".reel .hl")) return null;

  const canvas = document.createElement("canvas");
  let renderer;
  try {
    renderer = new WebGLRenderer({ canvas, antialias: true, alpha: true, powerPreference: "low-power" });
  } catch (e) {
    return null; // no WebGL: the flat bars draw instead
  }
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  canvas.className = "cutline-stage";
  canvas.setAttribute("aria-hidden", "true");
  fig.classList.add("is-3d");
  reels.appendChild(canvas);

  const scene = new Scene();
  const camera = new OrthographicCamera(-1, 1, 1, -1, -5000, 5000);
  camera.rotation.x = -TILT;
  scene.add(new AmbientLight(0xffffff, 0.7));
  const sun = new DirectionalLight(0xffffff, 3.6); // from above and in front: lit tops, softer fronts
  sun.position.set(-0.25, 0.83, 0.5);
  scene.add(sun);

  const plane = new PlaneGeometry(1, 1);
  let colors = readColors(fig);
  let hatch = hatchTexture(colors, renderer);
  let blocks = [];
  let size = { h: HEIGHT, d: DEPTH, k: 1 };
  let played = false;
  let tl = null;

  /* ---------------------------------------------------------- the blocks */
  function block(kind, w, x, z, lane) {
    const material = new MeshLambertMaterial({ transparent: true });
    paint(material, kind);
    const mesh = new Mesh(blockGeometry(w, size), material);
    const shadowMap = shadowTexture(w, size.d);
    const shadow = new Mesh(plane, new MeshBasicMaterial({ map: shadowMap, color: 0x000000, transparent: true, depthWrite: false }));
    shadow.rotation.x = -Math.PI / 2;
    shadow.scale.set(shadowMap.image.width, shadowMap.image.height, 1);
    shadow.position.set(0, 0.05, 5); // a touch forward, so it shows under the front edge
    shadow.renderOrder = -1;
    const group = new Group();
    group.position.set(x, 0, z);
    group.add(shadow, mesh);
    scene.add(group);
    return { group, mesh, shadow, kind, lane, to: { x, z }, from: null, lift: 0, alpha: 1 };
  }

  function paint(material, kind) {
    if (kind === "cut") {
      material.color.set(0xffffff);
      material.map = hatch;
    } else {
      material.color.set(kind === "hl" ? colors.marker : colors.kept);
      material.map = null;
    }
    material.needsUpdate = true;
  }

  function sync(b) {
    b.mesh.position.y = b.lift;
    b.mesh.material.opacity = b.alpha;
    b.mesh.visible = b.alpha > 0.001;
    const up = Math.max(0, b.lift);
    b.shadow.material.opacity = colors.shadow * b.alpha * Math.max(0, 1 - up / (40 * size.k));
    b.shadow.visible = b.mesh.visible;
  }

  function draw() {
    blocks.forEach(sync);
    renderer.render(scene, camera);
  }

  /* ---------------------------------------------------------- layout, from the flat bars */
  function measure() {
    const box = reels.getBoundingClientRect();
    const column = rows[0].querySelector(".reel").getBoundingClientRect();
    const width = Math.max(1, Math.round(column.width));
    const k = Math.min(1, Math.max(0.6, width / 640));
    size = { h: HEIGHT * k, d: DEPTH * k, k };
    const height = Math.round(box.height + ROOM + 8);
    const lanes = rows.map((row) => {
      const r = row.getBoundingClientRect();
      const fromTop = r.top - box.top + r.height / 2 + ROOM; // the row's middle, from the stage top
      return ((size.h * Math.cos(TILT)) / 2 - (height / 2 - fromTop)) / Math.sin(TILT);
    });
    const segments = rows.map((row) => Array.from(row.querySelectorAll(".reel > span")).map((s) => {
      const r = s.getBoundingClientRect();
      const kind = s.classList.contains("x") ? "cut" : s.classList.contains("hl") ? "hl" : "kept";
      return { kind, w: r.width, x: r.left - column.left + r.width / 2 - width / 2 };
    }));
    return { left: column.left - box.left, width, height, lanes, segments };
  }

  function build() {
    blocks.forEach((b) => {
      b.mesh.geometry.dispose();
      b.mesh.material.dispose();
      b.shadow.material.map.dispose();
      b.shadow.material.dispose();
      scene.remove(b.group);
    });
    blocks = [];
    const m = measure();
    Object.assign(canvas.style, { left: `${m.left}px`, top: `${-ROOM}px`, width: `${m.width}px`, height: `${m.height}px` });
    renderer.setSize(m.width, m.height, false);
    Object.assign(camera, { left: -m.width / 2, right: m.width / 2, top: m.height / 2, bottom: -m.height / 2 });
    camera.updateProjectionMatrix();

    // the recording, as it is
    m.segments[0].forEach((s) => blocks.push(block(s.kind, s.w, s.x, m.lanes[0], "rec")));
    // the final video: the highlights reel, then the kept parts closed up into the kept bar
    const kept = blocks.filter((b) => b.kind === "kept");
    const keptBar = m.segments[1].find((s) => s.kind === "kept");
    const hlBar = m.segments[1].find((s) => s.kind === "hl");
    if (keptBar && kept.length) {
      const gap = 2;
      const total = kept.reduce((sum, b) => sum + b.mesh.geometry.userData.w, 0);
      const scale = (keptBar.w - gap * (kept.length - 1)) / total;
      let x = keptBar.x - keptBar.w / 2;
      kept.forEach((src) => {
        const w = src.mesh.geometry.userData.w * scale;
        const b = block("kept", w, x + w / 2, m.lanes[1], "fin");
        b.from = { x: src.to.x, z: src.to.z, sx: 1 / scale };
        blocks.push(b);
        x += w + gap;
      });
    }
    if (hlBar) blocks.push(block("hl", hlBar.w, hlBar.x, m.lanes[1], "fin"));
    (played ? atEnd : atStart)();
    draw();
  }

  function atStart() {
    blocks.forEach((b) => {
      b.alpha = 0;
      b.lift = (b.lane === "rec" ? -12 : b.kind === "hl" ? 34 : 0) * size.k;
      if (b.from) b.group.position.set(b.from.x, 0, b.from.z);
      b.group.scale.set(b.from ? b.from.sx : b.kind === "hl" ? 0.7 : 1, 1, 1);
    });
  }

  function atEnd() {
    blocks.forEach((b) => {
      b.alpha = b.kind === "cut" ? 0.55 : 1;
      b.lift = 0;
      b.group.position.set(b.to.x, 0, b.to.z);
      b.group.scale.set(1, 1, 1);
    });
  }

  /* ---------------------------------------------------------- the sequence */
  function play() {
    played = true;
    atStart();
    tl = gsap.timeline({ onUpdate: draw, onComplete: draw });
    const [recTc, finTc] = rows.map((row) => row.querySelector(".tc"));
    const rec = blocks.filter((b) => b.lane === "rec");
    rec.forEach((b, i) => tl.to(b, { lift: 0, alpha: 1, duration: 0.6, ease: "power3.out" }, i * 0.045));
    if (recTc) countTimecode(tl, recTc, 0, 1.1, "power2.inOut");
    tl.to(rec.filter((b) => b.kind === "cut"), { alpha: 0.55, duration: 0.45, ease: "power2.out" }, 1.0);
    blocks.filter((b) => b.from).forEach((b, i) => {
      const at = 1.15 + i * 0.08;
      tl.set(b, { alpha: 1 }, at);
      tl.to(b.group.position, { x: b.to.x, z: b.to.z, duration: 0.95, ease: "power3.inOut" }, at);
      tl.to(b.group.scale, { x: 1, duration: 0.95, ease: "power3.inOut" }, at);
      tl.to(b, { keyframes: [{ lift: 26 * size.k, duration: 0.42, ease: "power2.out" }, { lift: 0, duration: 0.53, ease: "power2.in" }] }, at);
    });
    const hl = blocks.find((b) => b.kind === "hl");
    if (hl) {
      tl.to(hl, { lift: 0, alpha: 1, duration: 0.6, ease: "power3.out" }, 2.05);
      tl.to(hl.group.scale, { x: 1, duration: 0.6, ease: "power3.out" }, 2.05);
    }
    if (finTc) countTimecode(tl, finTc, 1.15, 1.45, "power2.inOut");
    return tl;
  }

  /* ---------------------------------------------------------- keep it in step with the page */
  function recolor() {
    colors = readColors(fig);
    const old = hatch;
    hatch = hatchTexture(colors, renderer);
    blocks.forEach((b) => paint(b.mesh.material, b.kind));
    old.dispose();
    draw();
  }
  new MutationObserver(recolor).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });

  let lastWidth = 0;
  const rebuild = () => {
    const w = Math.round(reels.getBoundingClientRect().width);
    if (w === lastWidth) return;
    lastWidth = w;
    if (tl && tl.isActive()) tl.progress(1);
    build();
  };
  rebuild();
  new ResizeObserver(() => requestAnimationFrame(rebuild)).observe(reels);

  /* already on screen when it arrived (a reload, a link to #the-edit): the end state, faded in */
  function show() {
    played = true;
    atEnd();
    draw();
    gsap.fromTo(canvas, { opacity: 0 }, { opacity: 1, duration: 0.35, ease: "power2.out", clearProps: "opacity" });
  }

  return { play, show };
}

/* ------------------------------------------------------------ helpers */
function readColors(el) {
  const style = getComputedStyle(el);
  const token = (name) => style.getPropertyValue(name).trim();
  const dark = document.documentElement.getAttribute("data-theme") === "dark";
  return { kept: token("--seg-kept"), hatch: token("--hatch"), paper: token("--surface"), marker: token("--marker"), shadow: dark ? 0.6 : 0.24 };
}

/* a block with softly rounded edges, standing on the ground (y = 0), centred on x and z */
function blockGeometry(w, { h, d }) {
  const bevel = Math.min(2, w * 0.18);
  const r = Math.max(bevel + 0.4, Math.min(4.5, w / 2 - 0.3));
  const x0 = -w / 2;
  const x1 = w / 2;
  const s = new Shape();
  s.moveTo(x0 + r, 0);
  s.lineTo(x1 - r, 0);
  s.quadraticCurveTo(x1, 0, x1, r);
  s.lineTo(x1, h - r);
  s.quadraticCurveTo(x1, h, x1 - r, h);
  s.lineTo(x0 + r, h);
  s.quadraticCurveTo(x0, h, x0, h - r);
  s.lineTo(x0, r);
  s.quadraticCurveTo(x0, 0, x0 + r, 0);
  const depth = d - 2 * bevel;
  const g = new ExtrudeGeometry(s, {
    depth, curveSegments: 5, bevelEnabled: true, bevelThickness: bevel, bevelSize: bevel, bevelOffset: -bevel, bevelSegments: 3,
  });
  g.translate(0, 0, -depth / 2);
  g.userData.w = w;
  return g;
}

/* the cut parts' diagonal hatch, in world pixels like the flat bars' stripes */
function hatchTexture(c, renderer) {
  const size = 28;
  const cv = document.createElement("canvas");
  cv.width = cv.height = size * 2;
  const g = cv.getContext("2d");
  g.scale(2, 2);
  g.fillStyle = c.paper;
  g.fillRect(0, 0, size, size);
  g.strokeStyle = c.hatch;
  g.lineWidth = 1.5;
  for (let x = -size; x <= size; x += 7) {
    g.beginPath();
    g.moveTo(x, size);
    g.lineTo(x + size, 0);
    g.stroke();
  }
  const t = new CanvasTexture(cv);
  t.wrapS = t.wrapT = RepeatWrapping;
  t.repeat.set(1 / size, 1 / size);
  t.colorSpace = SRGBColorSpace;
  t.anisotropy = renderer.capabilities.getMaxAnisotropy();
  return t;
}

/* a soft contact shadow the size of a block's footprint (1 canvas px = 1 world px) */
function shadowTexture(w, d) {
  const cw = Math.ceil(w + BLUR * 4);
  const ch = Math.ceil(d + BLUR * 4);
  const cv = document.createElement("canvas");
  cv.width = cw;
  cv.height = ch;
  const g = cv.getContext("2d");
  g.shadowColor = "#000";
  g.shadowBlur = BLUR;
  g.shadowOffsetX = cw + 20;
  g.fillStyle = "#000";
  g.beginPath();
  g.roundRect(BLUR * 2 - (cw + 20), BLUR * 2, w, d, 6);
  g.fill();
  return new CanvasTexture(cv);
}
