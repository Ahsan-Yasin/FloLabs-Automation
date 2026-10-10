/* Highlight Cutter: page motion for the public site and the sign-in pages
   (the app leaves this file out). GSAP runs the entrances and the scroll
   reveals, Lenis smooths wheel scrolling. The reveal helpers are small
   vanilla ports of React Bits components (SplitText, AnimatedContent,
   AnimatedList, CountUp; see web/static/vendor/README.md).

   - The head script in base.html adds .hc-motion when the reader allows
     motion, and .hc-intro, which hides the first screen's elements until this
     file shows them (it lets go by itself after 2s if this file never runs).
   - Every animation plays once, ends on the page's normal styles and leaves
     no inline transform behind.
   - With prefers-reduced-motion: reduce nothing here runs. */
(function () {
  "use strict";

  const root = document.documentElement;
  const gsap = window.gsap;
  if (!gsap || !root.classList.contains("hc-motion")) {
    root.classList.remove("hc-intro");
    return;
  }
  const ScrollTrigger = window.ScrollTrigger || null;
  const SplitText = window.SplitText || null;
  gsap.registerPlugin(...[ScrollTrigger, SplitText].filter(Boolean));

  const qs = (sel, scope) => (scope || document).querySelector(sel);
  const qsa = (sel, scope) => Array.from((scope || document).querySelectorAll(sel));
  const belowFold = (el) => el.getBoundingClientRect().top > window.innerHeight;

  /* ------------------------------------------------------------ smooth scrolling (public pages) */
  if (window.Lenis && qs("[data-header]") && !document.body.classList.contains("app-body")) {
    const lenis = new window.Lenis({ lerp: 0.11, allowNestedScroll: true, autoRaf: false });
    if (ScrollTrigger) lenis.on("scroll", ScrollTrigger.update);
    gsap.ticker.add((time) => lenis.raf(time * 1000));
    gsap.ticker.lagSmoothing(0);
    /* in-page links glide too; a keyboard press keeps the browser's own jump
       and focus move (the skip link always does) */
    document.addEventListener("click", (e) => {
      const a = e.target.closest('a[href*="#"]');
      if (!a || e.defaultPrevented || e.detail === 0 || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
      if (a.classList.contains("skip-link")) return;
      const url = new URL(a.href, location.href);
      if (url.origin !== location.origin || url.pathname !== location.pathname || !url.hash) return;
      const target = document.getElementById(decodeURIComponent(url.hash.slice(1)));
      if (!target) return;
      e.preventDefault();
      lenis.scrollTo(target);
      history.pushState(null, "", url.hash);
    });
  }

  /* ------------------------------------------------------------ helpers */
  /* SplitText (React Bits): a headline's lines rise out of a mask. The split is
     undone when the lines land, so the heading is plain text again. */
  function splitLines(heading) {
    if (!SplitText || !heading) return null;
    const html = heading.innerHTML;
    // a <br> hidden at this width must not become a line break while split
    qsa("br", heading).forEach((br) => { if (getComputedStyle(br).display === "none") br.remove(); });
    const split = SplitText.create(heading, { type: "lines", mask: "lines", linesClass: "split-line" });
    return {
      lines: split.lines,
      done() { split.revert(); heading.innerHTML = html; },
    };
  }

  /* AnimatedContent / AnimatedList (React Bits): content below the fold rises
     into place the first time it scrolls into view, lists one item after another. */
  function reveal(targets, { y = 24, stagger = 0, duration = 0.85, start = "top 86%", trigger = null } = {}) {
    if (!ScrollTrigger) return;
    const els = gsap.utils.toArray(targets).filter(belowFold);
    if (!els.length) return;
    gsap.set(els, { opacity: 0, y });
    ScrollTrigger.create({
      trigger: trigger || els[0], start, once: true,
      onEnter: () => gsap.to(els, { opacity: 1, y: 0, duration, stagger, ease: "power3.out", clearProps: "transform,opacity" }),
    });
  }

  /* CountUp (React Bits), for timecodes: counts 0:00 up to "41:05" or "1:38:12" */
  function countTimecode(tl, node, at, duration, ease) {
    const text = node.textContent.trim();
    const parts = text.split(":").map(Number);
    if (parts.some(Number.isNaN)) return;
    const total = parts.reduce((sum, p) => sum * 60 + p, 0);
    const pad = (n) => String(n).padStart(2, "0");
    const format = (s) => {
      s = Math.round(s);
      return parts.length === 3
        ? `${Math.floor(s / 3600)}:${pad(Math.floor((s % 3600) / 60))}:${pad(s % 60)}`
        : `${Math.floor(s / 60)}:${pad(s % 60)}`;
    };
    const counter = { s: 0 };
    node.textContent = format(0);
    tl.to(counter, {
      s: total, duration, ease,
      onUpdate: () => { node.textContent = format(counter.s); },
      onComplete: () => { node.textContent = text; },
    }, at);
  }

  /* The marking pass: a transcript excerpt arrives as plain text, then the
     editor marks it line by line (highlight for kept, grey strike and reason
     for cut). Optional: a status that reads "AI editing" until the last mark,
     and items (the downloads) that appear once it's done. Sets the plain state
     now and returns start(), to call once the excerpt has settled in place:
     the marks begin when its lines are in view. */
  function markPass(scope, { status = null, after = [] } = {}) {
    const lines = qsa(".line", scope);
    if (!lines.length || !ScrollTrigger) return () => {};
    scope.classList.add("is-marking");
    const done = status ? { cls: status.className, text: status.textContent } : null;
    if (status) {
      status.className = "status is-running";
      status.textContent = "AI editing";
    }
    if (after.length) gsap.set(after, { opacity: 0, y: 6 });
    const run = () => {
      const tl = gsap.timeline();
      lines.forEach((line, i) => tl.call(() => line.classList.add("is-marked"), null, i * 0.16));
      const end = (lines.length - 1) * 0.16 + 0.7;
      if (status) tl.call(() => { status.className = done.cls; status.textContent = done.text; }, null, end);
      if (after.length) tl.to(after, { opacity: 1, y: 0, duration: 0.45, stagger: 0.04, ease: "power2.out", clearProps: "transform,opacity" }, end);
      tl.call(() => {
        scope.classList.remove("is-marking");
        lines.forEach((line) => line.classList.remove("is-marked"));
      }, null, end + 0.2);
    };
    return () => ScrollTrigger.create({ trigger: qs(".lines", scope) || scope, start: "top bottom-=48", once: true, onEnter: run });
  }

  /* ------------------------------------------------------------ first screen */
  function intro() {
    if (!root.classList.contains("hc-intro")) return; // too late: the page is already showing
    const tl = gsap.timeline({ defaults: { ease: "power3.out" } });
    const rise = (els, at, stagger = 0.08) => {
      els = els.filter(Boolean);
      if (els.length) tl.fromTo(els, { opacity: 0, y: 16 }, { opacity: 1, y: 0, duration: 0.9, stagger, clearProps: "transform" }, at);
    };
    const headline = (h1, at) => {
      let split = null;
      try { split = splitLines(h1); } catch (e) { split = null; }
      if (!split) return rise([h1], at);
      gsap.set(h1, { opacity: 1 });
      tl.from(split.lines, { yPercent: 108, duration: 1.05, ease: "expo.out", stagger: 0.09, onComplete: split.done }, at);
    };

    const hero = qs(".hero");
    const pageHero = qs(".page-hero");
    const errorPage = qs(".error-page");
    const auth = qs(".auth-page");
    if (hero) {
      const copy = qs(".hero-copy", hero);
      const h1 = qs(".display", copy);
      headline(h1, 0);
      rise(Array.from(copy.children).filter((el) => el !== h1), 0.2);
      // the product window rises and settles flat, like a screen tilted up
      const shot = qs(".hero-shot", hero);
      if (shot) {
        tl.fromTo(shot,
          { opacity: 0, y: 56, rotateX: 14, scale: 0.97, transformPerspective: 1800, transformOrigin: "50% 0%" },
          { opacity: 1, y: 0, rotateX: 0, scale: 1, duration: 1.5, ease: "expo.out", clearProps: "transform" }, 0.3);
        tl.call(markPass(shot, { status: qs(".shot-head .status", shot), after: qsa(".shot-side .artifact", shot) }), null, 1.15);
      }
    } else if (pageHero) {
      const box = qs(".container", pageHero);
      const h1 = qs(".display", box);
      headline(h1, 0);
      rise(Array.from(box.children).filter((el) => el !== h1), 0.15);
    } else if (errorPage) {
      rise(Array.from(qs(".container", errorPage).children), 0, 0.07);
    } else if (auth) {
      rise(qsa(".auth-card > *", auth), 0, 0.05);
      const aside = qs(".auth-aside", auth);
      if (aside) {
        rise(Array.from(aside.children), 0.15, 0.12);
        const sheet = qs(".sheet", aside);
        if (sheet) tl.call(markPass(sheet), null, 0.75);
      }
    }
    root.classList.remove("hc-intro");
  }

  /* ------------------------------------------------------------ further down */
  function scrolling() {
    reveal(".section .section-head");
    reveal(".steps li", { stagger: 0.1, trigger: ".steps" });
    // the manifest's box is already on screen while its rows list in, so start as it enters
    reveal(".files li", { y: 12, stagger: 0.05, duration: 0.6, trigger: ".files", start: "top 96%" });
    reveal(".band-head > *", { stagger: 0.1, trigger: ".band-head" });
    reveal(".band .code-card", { y: 32 });
    reveal(".band-points li", { stagger: 0.08, trigger: ".band-points" });
    reveal(".qa > .h2");
    reveal(".qa dl", { y: 16 });
    reveal(".closing .container > *", { stagger: 0.08, trigger: ".closing" });
    reveal(".two-col > *", { stagger: 0.1 });
    reveal(".principles li", { y: 16, stagger: 0.06, trigger: ".principles" });
    reveal(".plans .plan", { stagger: 0.1, trigger: ".plans" });

    // the timeline: the recording plays out, then the shorter final video does.
    // In 3D (cutline3d.js, fetched as it nears the screen) or, failing that, as flat bars.
    const cut = qs(".cutline");
    if (cut && ScrollTrigger) timeline(cut);
  }

  function timeline(cut) {
    const flatAgain = () => {
      cut.classList.remove("is-3d");
      qsa(".cutline-stage", cut).forEach((stage) => stage.remove());
    };
    if (!belowFold(cut)) {
      // already on screen (a reload, a link to #the-edit): no sequence, the 3D stage as it ends
      if (cut.dataset.scene) {
        import(cut.dataset.scene).then((m) => {
          const scene = m.mount(cut, { gsap, countTimecode });
          if (scene) scene.show();
        }).catch(flatAgain);
      }
    } else {
      const legend = qs(".legend", cut);
      gsap.set(cut, { opacity: 0, y: 24 });
      gsap.set(qsa(".reel", cut), { clipPath: "inset(0% 100% 0% 0%)" });
      if (legend) gsap.set(legend, { opacity: 0 });
      let scene = null;
      let started = false;
      let loading = Promise.resolve();
      if (cut.dataset.scene) {
        ScrollTrigger.create({
          trigger: cut, start: "top bottom+=900", once: true,
          onEnter: () => {
            loading = import(cut.dataset.scene).then((m) => {
              if (started) return; // too late: the flat bars are already drawing
              scene = m.mount(cut, { gsap, countTimecode });
            }).catch(flatAgain); // the flat bars draw instead
          },
        });
      }
      ScrollTrigger.create({
        trigger: cut, start: "top 80%", once: true,
        onEnter: () => {
          gsap.to(cut, { opacity: 1, y: 0, duration: 0.8, ease: "power3.out", clearProps: "transform,opacity" });
          Promise.race([loading, new Promise((r) => setTimeout(r, 1200))]).then(() => {
            if (started) return;
            started = true;
            const tl = scene ? scene.play() : flatTimeline(cut);
            if (legend) tl.to(legend, { opacity: 1, duration: 0.5, clearProps: "opacity" }, ">-0.25");
          });
        },
      });
    }
  }

  function flatTimeline(cut) {
    const tl = gsap.timeline({ defaults: { ease: "power3.out" } });
    qsa(".reel-row", cut).forEach((row, i) => {
      const reel = qs(".reel", row);
      const tc = qs(".tc", row);
      const at = 0.3 + i * 0.95;
      const duration = i ? 0.8 : 1.15;
      const ease = i ? "power3.out" : "power2.inOut";
      if (reel) tl.to(reel, { clipPath: "inset(0% 0% 0% 0%)", duration, ease, clearProps: "clipPath" }, at);
      if (tc) countTimecode(tl, tc, at, duration, ease);
    });
    return tl;
  }

  /* wait for the fonts (at most 0.45s) so the headline splits on its real lines */
  const fontsReady = document.fonts && document.fonts.ready
    ? Promise.race([document.fonts.ready, new Promise((r) => setTimeout(r, 450))])
    : Promise.resolve();
  fontsReady.then(() => {
    try {
      intro();
      scrolling();
    } finally {
      root.classList.remove("hc-intro");
    }
  });
})();
