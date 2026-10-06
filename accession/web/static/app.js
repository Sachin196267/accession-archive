// Live values: the page polls /api/live and swaps text, states and bars in place.
(function () {
  const root = document.documentElement;
  try {
    const saved = localStorage.getItem("accession-theme");
    if (saved) root.dataset.theme = saved;
  } catch (e) {}

  document.addEventListener("click", (ev) => {
    const t = ev.target.closest("[data-theme-toggle]");
    if (t) {
      const dark = root.dataset.theme
        ? root.dataset.theme === "dark"
        : matchMedia("(prefers-color-scheme: dark)").matches;
      root.dataset.theme = dark ? "light" : "dark";
      try { localStorage.setItem("accession-theme", root.dataset.theme); } catch (e) {}
    }
    const c = ev.target.closest("[data-confirm]");
    if (c && !confirm(c.dataset.confirm)) ev.preventDefault();
  });

  // On serverless hosts there is no always-on worker: an open page keeps one
  // bounded burst of background work running at a time (the server enforces that).
  const tickUrl = document.body.dataset.tick;
  if (tickUrl) {
    (async function drive() {
      for (;;) {
        let wait = 15000;
        try {
          const r = await fetch(tickUrl, { method: "POST" });
          const d = await r.json();
          wait = d.ran ? 300 : d.reason === "busy" ? 8000 : 15000;
        } catch (e) {
          wait = 20000;
        }
        await new Promise((res) => setTimeout(res, wait));
      }
    })();
  }

  const live = document.body.dataset.live;
  if (!live) return;
  let token = null;
  let failures = 0;

  async function tick() {
    try {
      const r = await fetch(live, { headers: { Accept: "application/json" } });
      if (!r.ok) throw new Error(r.status);
      const data = await r.json();
      failures = 0;
      for (const [k, v] of Object.entries(data.k)) {
        document.querySelectorAll(`[data-k="${k}"]`).forEach((el) => {
          if (el.textContent !== v) el.textContent = v;
        });
      }
      for (const [k, v] of Object.entries(data.st)) {
        document.querySelectorAll(`[data-st="${k}"]`).forEach((el) => {
          el.dataset.s = v;
          el.dataset.state = v;
        });
      }
      for (const [k, v] of Object.entries(data.bar)) {
        document.querySelectorAll(`[data-bar="${k}"]`).forEach((el) => {
          el.style.width = Math.max(0, Math.min(100, v)).toFixed(1) + "%";
        });
      }
      for (const [k, v] of Object.entries(data.html)) {
        const el = document.querySelector(`[data-html="${k}"]`);
        if (el && el.innerHTML !== v) el.innerHTML = v;
      }
      if (token !== null && data.token !== token) {
        document.querySelector(".note-bar")?.classList.add("show");
      }
      token = data.token;
    } catch (e) {
      failures++;
    }
    setTimeout(tick, failures > 3 ? 10000 : 2000);
  }
  tick();
})();
