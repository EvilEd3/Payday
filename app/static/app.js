/* Payday. Small enhancements; every page still works with this file missing. */
(() => {
  "use strict";
  const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
  const $ = (sel, root = document) => root.querySelector(sel);
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
    const SEL = "details.more, details.sheetwrap, details.switcher, details.usermenu, details.colmenu";
    const menus = () => $$(SEL);
    document.addEventListener("toggle", (e) => {
      const d = e.target;
      if (d.matches && d.matches(SEL) && d.open) menus().forEach((o) => { if (o !== d) o.open = false; });
      if (d.matches && d.matches("details.more")) {
        d.classList.remove("up");
        const pop = d.querySelector(".pop");
        if (d.open && pop) {
          const r = pop.getBoundingClientRect(), top = d.getBoundingClientRect().top;
          const tab = document.querySelector(".tabbar");
          const floor = tab && getComputedStyle(tab).display !== "none" ? tab.getBoundingClientRect().top : window.innerHeight;
          const below = floor - r.top, need = r.height + 16;
          if (below < need && top > need) d.classList.add("up");
        }
      }
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
  const keep = {  // remembered in this browser across visits (sort order, columns)
    get(k) { try { return JSON.parse(localStorage.getItem(k) || "null"); } catch (e) { return null; } },
    set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) { /* private window: just don't remember */ } },
  };
  /* ---- drag a column's right edge to resize it; double-click the edge to reset. Remembered in this browser ---- */
  const WIDE = window.matchMedia("(min-width: 760px)");
  function resizable(table, heads, key) {
    if (table.classList.contains("ledger") || heads.length < 2) return;
    const last = heads.length - 1;
    let flex = -1;  // the description column, or else the widest, takes whatever is left
    const desc = heads.findIndex((h) => h.classList.contains("c-desc"));
    const pick = (widths) => (desc >= 0 ? desc : widths.indexOf(Math.max(...widths)));
    const apply = (widths) => {
      if (flex < 0) flex = pick(widths);
      table.classList.add("sized");
      heads.forEach((h, i) => { h.style.width = i === flex ? "auto" : (widths[i] ? widths[i] + "px" : ""); });
    };
    const clear = () => { table.classList.remove("sized"); heads.forEach((h) => { h.style.width = ""; }); };
    const restore = () => {
      const saved = keep.get(key);
      if (WIDE.matches && saved && Array.isArray(saved.w) && saved.w.length === heads.length) { flex = saved.f; apply(saved.w); }
      else clear();
    };
    restore();
    WIDE.addEventListener("change", restore);
    heads.forEach((th, i) => {
      if (i === last) return;
      const grip = document.createElement("span");
      grip.className = "colgrip";
      grip.title = "Drag to resize. Double-click to reset all columns";
      grip.setAttribute("aria-hidden", "true");
      th.appendChild(grip);
      grip.addEventListener("click", (e) => e.stopPropagation());
      grip.addEventListener("dblclick", (e) => { e.stopPropagation(); keep.set(key, null); flex = -1; clear(); });
      grip.addEventListener("pointerdown", (e) => {
        if (!WIDE.matches || e.button !== 0) return;
        e.preventDefault(); e.stopPropagation();
        grip.setPointerCapture(e.pointerId);
        const widths = heads.map((h) => Math.round(h.getBoundingClientRect().width));
        if (flex < 0) flex = pick(widths);
        const startX = e.clientX, start = widths[i];
        if (i === flex) flex = widths.indexOf(Math.max(...widths.filter((w, k) => k !== i)));
        const room = () => table.parentElement.clientWidth - widths.reduce((t, w, k) => t + (k === i || k === flex ? 0 : w), 0) - 96;
        apply(widths);
        const move = (ev) => {
          widths[i] = Math.round(Math.min(Math.max(48, start + ev.clientX - startX), Math.max(48, room())));
          heads[i].style.width = widths[i] + "px";
        };
        const up = () => {
          grip.removeEventListener("pointermove", move);
          grip.removeEventListener("pointerup", up);
          grip.removeEventListener("pointercancel", up);
          table.classList.remove("resizing");
          keep.set(key, { w: widths, f: flex });
        };
        table.classList.add("resizing");
        grip.addEventListener("pointermove", move);
        grip.addEventListener("pointerup", up);
        grip.addEventListener("pointercancel", up);
      });
    });
  }

  function tables(root) {
    $$("table", root || document).forEach((table, tIndex) => {
      if (table.dataset.ready) return;
      table.dataset.ready = "1";
      const head = table.tHead && table.tHead.rows[0];
      const body = table.tBodies[0];
      if (!head || !body) return;
      const heads = [...head.cells];
      const dataRows = () => [...body.rows].filter((r) => r.cells.length >= heads.length);
      // remembered per page and table, so the order survives changing period or coming back
      const key = "sort:" + location.pathname + ":" + (table.id || table.className.split(" ")[0] || "t") + ":" + tIndex;

      [...body.rows].forEach((r) => [...r.cells].forEach((c, i) => {
        if (!c.hasAttribute("data-label") && heads[i]) c.dataset.label = heads[i].textContent.trim();
      }));

      resizable(table, heads, key.replace("sort:", "cols:"));

      if (!table.classList.contains("nosort")) heads.forEach((th, i) => {
        if (!th.textContent.trim()) return;
        th.classList.add("sortable");
        th.tabIndex = 0;
        th.setAttribute("role", "columnheader");
        const sort = (forced) => {
          const rows = dataRows();
          const sample = rows.length ? cellText(rows[0].cells[i]) : "";
          const numeric = asNumber(sample) !== null || /^\d{4}-\d\d-\d\d/.test(sample);
          const now = th.getAttribute("aria-sort");
          const dir = forced || (now ? (now === "ascending" ? "descending" : "ascending") : (numeric ? "descending" : "ascending"));
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
          if (!forced) keep.set(key, { col: th.textContent.trim(), dir });
        };
        th.addEventListener("click", () => sort());
        th.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); sort(); } });
        const saved = keep.get(key);
        if (saved && saved.col === th.textContent.trim() && (saved.dir === "ascending" || saved.dir === "descending")) sort(saved.dir);
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

  /* ---- turn on notifications for this device ---- */
  function pushSetup() {
    const box = $("[data-push]");
    if (!box) return;
    const btn = $("[data-push-on]", box), msg = $("[data-push-msg]", box);
    const say = (text, bad) => { msg.hidden = false; msg.textContent = text; msg.classList.toggle("bad", !!bad); };
    const ios = /iPhone|iPad|iPod/.test(navigator.userAgent);
    const installed = window.matchMedia("(display-mode: standalone)").matches || navigator.standalone;
    if (!window.isSecureContext) { $("[data-push-http]", box).hidden = false; btn.disabled = true; return; }
    if (!("serviceWorker" in navigator) || !("PushManager" in window)) {
      if (ios && !installed) $("[data-push-ios]", box).hidden = false;
      else say("This browser can’t show notifications from websites.", true);
      btn.disabled = true;
      return;
    }
    const key = (b64) => {
      const s = atob((b64 + "===".slice((b64.length + 3) % 4)).replace(/-/g, "+").replace(/_/g, "/"));
      return Uint8Array.from(s, (c) => c.charCodeAt(0));
    };
    const send = async (sub) => {
      const j = sub.toJSON(), body = new FormData();
      body.set("endpoint", j.endpoint); body.set("p256dh", j.keys.p256dh); body.set("auth", j.keys.auth);
      const r = await fetch("/me/push/subscribe", { method: "POST", body, credentials: "same-origin" });
      if (!r.ok) throw new Error("save");
    };
    navigator.serviceWorker.ready.then(async (reg) => {
      const sub = await reg.pushManager.getSubscription();
      if (sub && Notification.permission === "granted") { btn.textContent = "On for this device"; btn.classList.add("quiet"); send(sub).catch(() => {}); }
    }).catch(() => {});
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      try {
        const perm = await Notification.requestPermission();
        if (perm !== "granted") { say("Notifications are blocked for Payday. Allow them in this browser’s site settings, then try again.", true); return; }
        const reg = await navigator.serviceWorker.register("/sw.js");
        await navigator.serviceWorker.ready;
        const sub = (await reg.pushManager.getSubscription()) ||
          await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: key(box.dataset.key) });
        await send(sub);
        say("Notifications are on for this device. Reloading…");
        setTimeout(() => location.reload(), 700);
      } catch (e) {
        say("Couldn’t turn notifications on here. Try again, or check this browser allows notifications.", true);
      } finally { btn.disabled = false; }
    });
  }

  /* ---- Columns menu: choose which columns a table shows, remembered in this browser ---- */
  function columns(root) {
    $$("details.colmenu[data-cols]", root || document).forEach((menu) => {
      const table = document.getElementById(menu.dataset.cols);
      if (!table) return;
      const key = "cols:" + menu.dataset.cols;
      const hidden = new Set(keep.get(key) || []);
      const apply = () => $$("input[type=checkbox]", menu).forEach((box) => {
        box.checked = !hidden.has(box.value);
        table.classList.toggle("hide-" + box.value, hidden.has(box.value));
      });
      apply();
      menu.addEventListener("change", (e) => {
        if (!e.target.matches("input[type=checkbox]")) return;
        e.target.checked ? hidden.delete(e.target.value) : hidden.add(e.target.value);
        keep.set(key, [...hidden]);
        apply();
      });
    });
  }

  /* ---- search boxes that ask the server (Transactions, Payees): results update as you type ---- */
  function liveSearch() {
    $$("form.searchbar[data-live]").forEach((form) => {
      const input = form.querySelector("input[type=search]");
      const target = document.querySelector(form.dataset.live);
      if (!input || !target) return;
      let timer = null, ctrl = null;
      const run = async () => {
        const url = new URL(form.action, location.href);
        new FormData(form).forEach((v, k) => { if (v !== "") url.searchParams.set(k, v); });
        if (ctrl) ctrl.abort();
        ctrl = new AbortController();
        try {
          const res = await fetch(url, { signal: ctrl.signal, headers: { "X-Live": "1" } });
          if (!res.ok) return;
          const doc = new DOMParser().parseFromString(await res.text(), "text/html");
          const fresh = doc.querySelector(form.dataset.live);
          if (!fresh) return;
          target.replaceChildren(...[...fresh.childNodes].map((n) => document.importNode(n, true)));
          const head = doc.querySelector(".pagehead"), here = document.querySelector(".pagehead");
          if (head && here) here.replaceChildren(...[...head.childNodes].map((n) => document.importNode(n, true)));
          history.replaceState(null, "", url.pathname + url.search);
          tables(target); columns(target);
        } catch (e) { /* aborted or offline: the Search button still works */ }
      };
      input.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(run, 300); });
      form.addEventListener("submit", (e) => { e.preventDefault(); clearTimeout(timer); run(); });
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

  /* ---- split a transaction: show what's left to give a category ---- */
  function splitForm() {
    const form = document.querySelector("form[data-split-total]");
    if (!form) return;
    const total = parseFloat(form.dataset.splitTotal), out = form.querySelector("[data-split-left]");
    const show = () => {
      const used = [...form.querySelectorAll("[data-split-amount]")].reduce((sum, i) => sum + (parseFloat(i.value.replace(/[$,]/g, "")) || 0), 0);
      const left = Math.round((total - used) * 100) / 100;
      out.textContent = left === 0 ? "All of it has a category." : (left > 0 ? "$" + left.toFixed(2) + " still to give a category." : "$" + (-left).toFixed(2) + " more than the transaction.");
      out.className = "small " + (left === 0 ? "pos" : "neg");
    };
    form.addEventListener("input", show);
    show();
  }

  /* ---- "What's new" beside the version number after an update, until you've opened the update log ---- */
  /* ---- "Bank data from ..." pill: tap or click shows the full sync details; tap away or Escape hides them ---- */
  function freshTip() {
    const set = (w, on) => { w.classList.toggle("open", on); const b = w.querySelector("[data-freshtip]"); if (b) b.setAttribute("aria-expanded", on); };
    document.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-freshtip]");
      $$(".freshtip").forEach((w) => { if (!w.contains(e.target)) set(w, false); });
      if (btn) { const w = btn.closest(".freshtip"); set(w, !w.classList.contains("open")); }
    });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") $$(".freshtip.open").forEach((w) => set(w, false)); });
  }

  function whatsNew() {
    const link = document.querySelector(".sitefoot a.ver[data-version]");
    if (!link) return;
    const now = link.dataset.version, seen = keep.get("seen-version");
    if (document.body.classList.contains("page-updates") || seen === null) keep.set("seen-version", now);
    else if (seen !== now) link.classList.add("new");
  }

  document.addEventListener("DOMContentLoaded", () => {
    whatsNew(); freshTip(); splitForm(); sync(); folds(); popups(); forms(); filters(); tables(); tabs(); copying(); cycleForm(); liveSearch(); columns(); pushSetup();
    tidyAddress();
    restoreScroll();
  });
})();
