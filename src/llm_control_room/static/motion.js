"use strict";
/* Micro-interactions: ripple, spotlight, stagger, count-up, bar grow, typing reveal, busy state, flashes. */
(() => {
  const reduce = matchMedia("(prefers-reduced-motion: reduce)");
  const fine = matchMedia("(pointer: fine)");
  let last = null; // last button pressed, for success/error flashes

  document.addEventListener("pointerdown", (e) => {
    const b = e.target.closest && e.target.closest("button, .btn");
    if (!b || b.disabled) return;
    last = b;
    if (reduce.matches) return;
    const r = b.getBoundingClientRect(), s = Math.max(r.width, r.height) * 2;
    const i = document.createElement("span");
    i.className = "ripple"; i.setAttribute("aria-hidden", "true");
    i.style.cssText = `width:${s}px;height:${s}px;left:${e.clientX - r.left - s / 2}px;top:${e.clientY - r.top - s / 2}px`;
    b.appendChild(i); setTimeout(() => i.remove(), 650);
  }, true);

  document.addEventListener("pointermove", (e) => {
    if (!fine.matches || reduce.matches) return;
    const c = e.target.closest && e.target.closest(".card, .tile, .scen");
    if (!c) return;
    const r = c.getBoundingClientRect();
    c.style.setProperty("--mx", e.clientX - r.left + "px"); c.style.setProperty("--my", e.clientY - r.top + "px");
  }, { passive: true });

  function flash(kind, el) {
    el = el || last; if (!el || !el.isConnected) return;
    const c = kind === "err" ? "flash-err" : "flash-ok";
    el.classList.remove("flash-ok", "flash-err"); void el.offsetWidth; el.classList.add(c);
    setTimeout(() => el.classList.remove(c), 800);
  }
  window.lcrFlash = flash;

  function countUp(el) {
    if (el.dataset.counted || el.children.length) return; el.dataset.counted = "1";
    const txt = el.textContent, m = /-?\d[\d,]*(\.\d+)?/.exec(txt);
    if (!m || reduce.matches) return;
    const to = parseFloat(m[0].replace(/,/g, "")), dec = (m[1] || "").length, grp = m[0].includes(",");
    if (!isFinite(to) || Math.abs(to) < 2) return;
    const fmt = (v) => grp ? v.toLocaleString(undefined, { minimumFractionDigits: dec, maximumFractionDigits: dec }) : v.toFixed(dec);
    const t0 = performance.now(), D = 700;
    const step = (t) => {
      const p = Math.min(1, (t - t0) / D), e = 1 - Math.pow(1 - p, 3);
      el.textContent = txt.replace(m[0], fmt(to * e));
      if (p < 1) requestAnimationFrame(step); else el.textContent = txt;
    };
    requestAnimationFrame(step);
  }

  function typeIn(el) {
    if (el.dataset.typed) return; el.dataset.typed = "1";
    if (reduce.matches) return;
    const full = el.textContent; if (full.length < 2) return;
    const t0 = performance.now(), D = Math.min(900, 250 + full.length * 8);
    el.setAttribute("aria-label", full); el.classList.add("typing");
    const step = (t) => {
      const p = Math.min(1, (t - t0) / D);
      el.textContent = full.slice(0, Math.ceil(full.length * p));
      if (p < 1) requestAnimationFrame(step);
      else { el.textContent = full; el.classList.remove("typing"); el.removeAttribute("aria-label"); }
    };
    requestAnimationFrame(step);
  }

  function growBar(i) {
    if (i.dataset.grown || reduce.matches) return; i.dataset.grown = "1";
    const w = i.style.width; i.style.width = "0"; void i.offsetWidth; i.style.width = w;
  }


  /* floating labels and valid check marks for simple label + text/number input pairs */
  function floatField(root) {
    root.querySelectorAll("div > label + input").forEach((inp) => {
      const w = inp.parentElement, lab = inp.previousElementSibling;
      if (w.classList.contains("fl") || w.children.length !== 2 || !/^(text|number|search|)$/.test(inp.getAttribute("type") || "")) return;
      if (inp.hasAttribute("data-l") && !lab.textContent.trim()) return;
      if (!lab.textContent.replace(/ /g, "").trim()) return;
      w.classList.add("fl");
      const ph = inp.getAttribute("placeholder");
      if (ph) w.classList.add("up"); else inp.setAttribute("placeholder", " ");
      if (inp.type === "number" || inp.value) w.classList.add("up");
      lab.removeAttribute("for"); lab.style.display = "";
      inp.style.minWidth = Math.max(inp.type === "number" ? 104 : 0, Math.ceil(lab.scrollWidth * 0.9 + 30)) + "px";
      const upd = () => {
        const filled = inp.value.trim() !== "";
        w.classList.toggle("ok", filled && inp.checkValidity());
        w.classList.toggle("bad", filled && !inp.checkValidity());
        if (!ph && inp.type !== "number") w.classList.toggle("up", filled);
      };
      inp.addEventListener("input", upd); inp.addEventListener("blur", upd);
    });
  }

  function enhance(root) {
    if (root.nodeType !== 1) return;
    floatField(root);
    const items = root.matches(".card, .tile, .scen") ? [root] : [];
    items.push(...root.querySelectorAll(".card, .tile, .scen"));
    let n = 0;
    for (const c of items) {
      if (c.dataset.in || c.closest("#r-log")) continue; c.dataset.in = "1";
      if (!reduce.matches) { c.style.setProperty("--i", Math.min(n++, 10)); c.classList.add("enter"); setTimeout(() => c.classList.remove("enter"), 1000); }
    }
    root.querySelectorAll(".tile .v").forEach(countUp);
    root.querySelectorAll(".bars .t i").forEach(growBar);
    root.querySelectorAll("#h-out .answer").forEach(typeIn);
  }

  new MutationObserver((muts) => {
    for (const m of muts) {
      if (m.type === "attributes") {
        const b = m.target; if (b.tagName !== "BUTTON") continue;
        b.classList.toggle("busy", b.disabled);
        if (b.id === "h-run") { const h = b.closest(".hero"); if (h) h.classList.toggle("scanning", b.disabled); }
        if (b.disabled) b.dataset.wasBusy = "1";
        else if (b.dataset.wasBusy) {
          delete b.dataset.wasBusy;
          flash(b.id === "h-run" && document.querySelector("#h-out .chip.bad") ? "err" : "ok", b);
        }
      } else m.addedNodes.forEach(enhance);
    }
  }).observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ["disabled"] });

  document.addEventListener("invalid", (e) => {
    const el = e.target; el.classList.remove("shake"); void el.offsetWidth; el.classList.add("shake");
    setTimeout(() => el.classList.remove("shake"), 500);
  }, true);
  document.addEventListener("click", (e) => {
    const b = e.target.closest && e.target.closest(".chips button");
    if (b) { b.classList.remove("pop"); void b.offsetWidth; b.classList.add("pop"); }
  });
})();
