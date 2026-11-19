/* Payday. Small enhancements; every page still works with this file missing. */
(() => {
  "use strict";
  const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
  const store = {
    get(k) { try { return sessionStorage.getItem(k); } catch (e) { return null; } },
    set(k, v) { try { sessionStorage.setItem(k, v); } catch (e) {} },
    del(k) { try { sessionStorage.removeItem(k); } catch (e) {} },
  };

  /* ---- install as an app ---- */
  if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
  let installEvent = null;
  window.addEventListener("beforeinstallprompt", (e) => {
    e.preventDefault();
    installEvent = e;
    $$("[data-install]").forEach((b) => (b.hidden = false));
  });

  /* ---- stay where you were after saving something ----
     Forms reload the page. Remember the scroll position on submit and put it back afterwards. */
  document.addEventListener("submit", (e) => {
    const form = e.target;
    if (form.dataset.confirm && !window.confirm(form.dataset.confirm)) { e.preventDefault(); return; }
    if ((form.method || "").toLowerCase() === "post") store.set("scroll", JSON.stringify({ path: location.pathname, y: window.scrollY }));
  }, true);

  function restoreScroll() {
    const saved = store.get("scroll");
    if (!saved) return;
    store.del("scroll");
    try {
      const { path, y } = JSON.parse(saved);
      if (path === location.pathname) window.scrollTo(0, y);
    } catch (e) {}
  }

  /* ---- one-off messages: drop ?msg= / ?changed= from the address so a refresh doesn't repeat them ---- */
  function tidyAddress() {
    const url = new URL(location.href);
    let changed = false;
    ["msg", "changed", "sync"].forEach((k) => { if (url.searchParams.has(k)) { url.searchParams.delete(k); changed = true; } });
    if (changed) history.replaceState(null, "", url.pathname + (url.search || "") + url.hash);
  }

  /* ---- folding panels remember whether they're open ---- */
  function folds() {
    $$("details.fold[id]").forEach((d) => {
      try { const v = localStorage.getItem("fold:" + d.id); if (v !== null) d.open = v === "1"; } catch (e) {}
      if (location.hash === "#" + d.id) d.open = true;
      d.addEventListener("toggle", () => { try { localStorage.setItem("fold:" + d.id, d.open ? "1" : "0"); } catch (e) {} });
    });
  }

  /* ---- pop-up menus: one open at a time; click away or Escape closes ---- */
  function popups() {
    const SEL = "details.more, details.sheetwrap, details.switcher, details.usermenu";
    const menus = () => $$(SEL);
    document.addEventListener("toggle", (e) => {
      const d = e.target;
      if (d.matches && d.matches(SEL) && d.open) menus().forEach((o) => { if (o !== d) o.open = false; });
    }, true);
    document.addEventListener("click", (e) => { menus().forEach((d) => { if (d.open && !d.contains(e.target)) d.open = false; }); });
    document.addEventListener("keydown", (e) => {
      if (e.key !== "Escape") return;
      menus().forEach((d) => { if (d.open) { d.open = false; const s = d.querySelector("summary"); if (s) s.focus(); } });
    });
  }

  /* ---- small form behaviours ---- */
  function forms() {
    document.addEventListener("change", (e) => {
      const el = e.target;
      if (el.matches("[data-autosubmit]")) el.form.requestSubmit();
      if (el.matches("[data-limit-mode]")) limitRow(el);
    });
    $$("[data-limit-mode]").forEach(limitRow);
    document.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-install]");
      if (btn && installEvent) installEvent.prompt();
      const go = e.target.closest("[data-href]");
      if (go) location.href = go.dataset.href;
    });
  }
  function limitRow(select) {
    const amount = select.form.querySelector("[data-limit-amount]");
    if (amount) amount.hidden = select.value !== "fixed";
  }

  /* ---- filter a list as you type: <input data-filter="#id"> hides children whose text doesn't match ---- */
  function filters() {
    $$("input[data-filter]").forEach((input) => {
      const lists = $$(input.dataset.filter);
      const count = input.parentElement.querySelector(".count");
      input.addEventListener("input", () => {
        const words = input.value.toLowerCase().split(/\s+/).filter(Boolean);
        let shown = 0, all = 0;
        lists.forEach((list) => {
          let any = 0;
          [...list.children].forEach((item) => {
            const hay = (item.dataset.search || item.textContent).toLowerCase();
            const ok = words.every((w) => hay.includes(w));
            item.hidden = !ok; all++;
            if (ok) { shown++; any++; }
          });
          list.classList.toggle("none", words.length > 0 && !any);
          const fold = list.closest("details.fold");
          if (fold && words.length && any) fold.open = true;
          const head = document.querySelector('[data-count-for="#' + list.id + '"]');
          if (head) head.textContent = words.length ? any + " of " + list.children.length : list.children.length;
        });
        if (count) count.textContent = words.length ? shown + " of " + all : "";
      });
    });
  }

  /* ---- tables: labels for the phone layout, click a heading to sort, search box on long tables ---- */
  const cellText = (cell) => {
    if (!cell) return "";
    if (cell.dataset.sort !== undefined) return cell.dataset.sort;
    const sel = cell.querySelector("select");
    if (sel) return ((sel.options[sel.selectedIndex] || {}).text || "").trim();
    return cell.innerText.trim();
  };
  const asNumber = (v) => {
    const t = v.replace(/[−–]/g, "-").replace(/[\s$,×%+]/g, "");
    return /^-?\d+(\.\d+)?$/.test(t) ? parseFloat(t) : null;
  };
  function tables() {
    $$("table").forEach((table) => {
      const head = table.tHead && table.tHead.rows[0];
      const body = table.tBodies[0];
      if (!head || !body) return;
      const heads = [...head.cells];
      const dataRows = () => [...body.rows].filter((r) => r.cells.length >= heads.length);

      [...body.rows].forEach((r) => [...r.cells].forEach((c, i) => {
        if (!c.hasAttribute("data-label") && heads[i]) c.dataset.label = heads[i].textContent.trim();
      }));

      if (!table.classList.contains("nosort")) heads.forEach((th, i) => {
        if (!th.textContent.trim()) return;
        th.classList.add("sortable");
        th.tabIndex = 0;
        th.setAttribute("role", "columnheader");
        const sort = () => {
          const rows = dataRows();
          const sample = rows.length ? cellText(rows[0].cells[i]) : "";
          const numeric = asNumber(sample) !== null || /^\d{4}-\d\d-\d\d/.test(sample);
          const now = th.getAttribute("aria-sort");
          const dir = now ? (now === "ascending" ? "descending" : "ascending") : (numeric ? "descending" : "ascending");
          heads.forEach((h) => h.removeAttribute("aria-sort"));
          th.setAttribute("aria-sort", dir);
          rows.sort((a, b) => {
            const x = cellText(a.cells[i]), y = cellText(b.cells[i]);
            let nx = asNumber(x), ny = asNumber(y);
            if (nx === null && x === "" && ny !== null) nx = 0;
            if (ny === null && y === "" && nx !== null) ny = 0;
            const r = nx !== null && ny !== null ? nx - ny : x.localeCompare(y, undefined, { numeric: true, sensitivity: "base" });
            return dir === "ascending" ? r : -r;
          });
          rows.forEach((r) => body.appendChild(r));
        };
        th.addEventListener("click", sort);
        th.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); sort(); } });
      });

      if (table.classList.contains("nosearch") || dataRows().length < 8) return;
      const tools = document.createElement("div");
      tools.className = "table-tools";
      tools.innerHTML = '<input type="search" placeholder="Search this list" aria-label="Search this list"><span class="count" aria-live="polite"></span>';
      const holder = table.closest(".panel") || table;
      holder.parentNode.insertBefore(tools, holder);
      const input = tools.querySelector("input"), count = tools.querySelector(".count");
      input.addEventListener("input", () => {
        const words = input.value.toLowerCase().replace(/[$,]/g, "").split(/\s+/).filter(Boolean);
        const rows = dataRows();
        let shown = 0;
        rows.forEach((r) => {
          const picked = $$("select", r).map((x) => (x.options[x.selectedIndex] || {}).text || "").join(" ");
          const hay = (r.innerText + " " + picked).toLowerCase().replace(/[$,]/g, "");
          const ok = words.every((w) => hay.includes(w));
          r.hidden = !ok;
          if (ok) shown++;
        });
        count.textContent = words.length ? shown + " of " + rows.length : "";
      });
    });
  }

  /* ---- live sync progress ---- */
  function sync() {
    const panel = document.getElementById("sync-panel");
    if (!panel) return;
    let timer = null;
    const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
    const render = (st) => {
      panel.hidden = false;
      const steps = st.steps || [];
      panel.querySelector("h3").textContent = st.running ? "Syncing with Akahu…" : (steps.length ? (st.ok ? "Sync finished" : "Sync failed") : "Last sync");
      panel.querySelector(".sync-steps").innerHTML = steps.length
        ? steps.map((x) => '<li class="' + esc(x.status) + '"><span class="ic" aria-hidden="true"></span><span>' + esc(x.label) + (x.detail ? "<small>" + esc(x.detail) + "</small>" : "") + "</span></li>").join("")
        : '<li class="done"><span class="ic"></span><span>' + esc(st.last || "No sync has run yet.") + (st.last_time ? "<small>" + esc(st.last_time) + "</small>" : "") + "</span></li>";
      panel.querySelector(".sync-meta").textContent = st.started
        ? "Started " + st.started + (st.finished ? ", finished " + st.finished : "") + (st.trigger === "scheduled" ? " (daily sync)" : st.trigger === "catch-up" ? " (caught up after the app was off)" : "") : "";
      panel.querySelector("[data-sync-reload]").hidden = st.running || !st.ok;
    };
    const poll = async () => {
      clearTimeout(timer);
      try {
        const st = await (await fetch("/sync/status", { headers: { Accept: "application/json" } })).json();
        render(st);
        if (st.running) timer = setTimeout(poll, 1000);
      } catch (e) { timer = setTimeout(poll, 3000); }
    };
    document.addEventListener("click", async (e) => {
      if (e.target.closest("[data-sync-start]")) {
        e.preventDefault();
        await fetch("/sync", { method: "POST", headers: { Accept: "application/json" } });
        poll();
      } else if (e.target.closest("[data-sync-show]")) { e.preventDefault(); poll(); }
      else if (e.target.closest("[data-sync-reload]")) location.href = "/";
    });
    if (new URLSearchParams(location.search).has("sync") || document.body.dataset.syncRunning === "1") poll();
  }

  /* ---- keep the current settings section in view when the section strip scrolls sideways ---- */
  function tabs() {
    const strip = document.querySelector(".tabs"), current = strip && strip.querySelector("[aria-current]");
    if (current) strip.scrollLeft = current.offsetLeft - (strip.clientWidth - current.offsetWidth) / 2;
  }

  /* ---- copy buttons (invite links, backup codes) ---- */
  function copying() {
    document.addEventListener("click", async (e) => {
      const b = e.target.closest("[data-copy]");
      if (!b) return;
      try { await navigator.clipboard.writeText(b.dataset.copy); const was = b.textContent; b.textContent = "Copied"; setTimeout(() => { b.textContent = was; }, 1600); }
      catch (err) { const i = b.parentElement.querySelector("input[data-select-all]"); if (i) i.select(); }
    });
    $$("input[data-select-all]").forEach((i) => i.addEventListener("focus", () => i.select()));
  }

  /* ---- pay cycle form: show only the fields for the chosen frequency ---- */
  function cycleForm() {
    const pick = document.querySelector("[data-cycle-pick]");
    if (!pick) return;
    const show = () => $$("[data-for]").forEach((el) => { el.hidden = !el.dataset.for.split(" ").includes(pick.value); });
    pick.addEventListener("change", show);
    show();
  }

  document.addEventListener("DOMContentLoaded", () => {
    sync(); folds(); popups(); forms(); filters(); tables(); tabs(); copying(); cycleForm();
    tidyAddress();
    restoreScroll();
  });
})();
