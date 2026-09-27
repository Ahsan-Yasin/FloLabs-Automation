/* Home page: the hero demo (Motion) and "How it works" (GSAP ScrollTrigger).
   Without JavaScript, or with reduced motion, everything shows its final,
   fully marked-up state; nothing here hides content permanently. */
(function () {
  "use strict";
  const { qs, qsa, reduced, Motion, EASE_OUT, fmt } = window.HC;

  /* ------------------------------------------------------------ hero demo */
  const demo = qs("[data-demo]");
  if (demo) {
    const lines = qsa("[data-demo-line]", demo);
    const strikes = qsa(".demo-strike", demo);
    const highlights = qsa(".demo-hl", demo);
    const whys = qsa(".demo-why", demo);
    const cutTexts = qsa("[data-cut] .t", demo);
    const cutSegs = qsa(".demo-bar.orig .seg.x", demo);
    const finalBar = qs(".demo-bar.final", demo);
    const chips = qsa(".out-chip", demo);
    const counter = qs("[data-demo-counter]", demo);
    const replay = qs("[data-demo-replay]", demo);
    const FROM_S = 98 * 60 + 12;
    const TO_S = 41 * 60 + 5;
    let running = null;

    const play = () => {
      if (!Motion || reduced) return;
      if (running) running.stop();
      const { animate, stagger } = Motion;
      const ease = EASE_OUT;
      // start state (set inline; the finished animation leaves final values inline)
      animate(lines, { opacity: 0, y: 6 }, { duration: 0 });
      animate([...strikes, ...highlights], { scaleX: 0 }, { duration: 0 });
      animate(whys, { opacity: 0 }, { duration: 0 });
      animate(cutTexts, { opacity: 1 }, { duration: 0 });
      animate(cutSegs, { opacity: 0.55 }, { duration: 0 });
      animate(finalBar, { scaleX: 0 }, { duration: 0 });
      animate(chips, { opacity: 0, y: 6 }, { duration: 0 });
      if (counter) counter.textContent = fmt.clock(FROM_S);

      const sequence = [[lines, { opacity: [0, 1], y: [6, 0] }, { duration: 0.35, delay: stagger(0.05), ease }]];
      lines.forEach((line, i) => {
        const at = i === 0 ? "+0.1" : "+0.06";
        if (line.hasAttribute("data-cut")) {
          sequence.push([qs(".demo-strike", line), { scaleX: [0, 1] }, { duration: 0.22, at, ease }]);
          sequence.push([qs(".t", line), { opacity: [1, 0.5] }, { duration: 0.22, at: "<", ease }]);
          const why = qs(".demo-why", line);
          if (why) sequence.push([why, { opacity: [0, 1] }, { duration: 0.18, at: "<", ease }]);
        } else {
          sequence.push([qs(".demo-hl", line), { scaleX: [0, 1] }, { duration: 0.24, at, ease }]);
        }
      });
      sequence.push([cutSegs, { opacity: [0.55, 0.14] }, { duration: 0.3, at: "+0.1", ease }]);
      sequence.push([finalBar, { scaleX: [0, 1] }, { duration: 0.5, at: "<", ease }]);
      sequence.push([chips, { opacity: [0, 1], y: [6, 0] }, { duration: 0.28, delay: stagger(0.05), at: "-0.2", ease }]);
      running = animate(sequence);
      running.then(() => { if (replay) replay.hidden = false; });
      if (counter) {
        setTimeout(() => {
          animate(FROM_S, TO_S, { duration: 1.1, ease, onUpdate: (v) => { counter.textContent = fmt.clock(v); } });
        }, 1500);
      }
    };

    if (Motion && !reduced && Motion.inView) {
      const stopWatching = Motion.inView(demo, () => { play(); stopWatching(); }, { amount: 0.35 });
      if (replay) replay.addEventListener("click", () => { replay.hidden = true; play(); });
    }
  }

  /* ------------------------------------------------------------ how it works */
  const steps = qsa(".how-step");
  const scenes = qsa(".how-visual .how-scene");
  const setActive = (index) => {
    steps.forEach((s, i) => s.classList.toggle("is-active", i === index));
    scenes.forEach((s, i) => s.classList.toggle("is-active", i === index));
  };
  const gsap = window.gsap;
  const ScrollTrigger = window.ScrollTrigger;
  if (steps.length && gsap && ScrollTrigger && !reduced) {
    gsap.registerPlugin(ScrollTrigger);
    const mm = gsap.matchMedia();
    mm.add("(min-width: 901px)", () => {
      steps.forEach((step, i) => {
        ScrollTrigger.create({
          trigger: step,
          start: "top 58%",
          end: "bottom 58%",
          onToggle: (self) => { if (self.isActive) setActive(i); },
        });
      });
      // progress indicator: linear on purpose (it tracks the scroll position)
      gsap.fromTo(".how-rail-fill", { scaleY: 0 }, {
        scaleY: 1,
        ease: "none",
        scrollTrigger: { trigger: ".how-steps", start: "top 58%", end: "bottom 58%", scrub: 0.4 },
      });
      return () => setActive(0);
    });
  } else {
    steps.forEach((s) => s.classList.add("is-active"));
    const fill = qs(".how-rail-fill");
    if (fill) fill.style.transform = "scaleY(1)";
  }
})();
