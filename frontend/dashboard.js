"use strict";

const token = localStorage.getItem("webex_token");

// ─── WebSocket Manager (real-time results) ──────────────────────────
let _ws = null;
let _wsSessionId = null;
let _wsReconnectTimer = null;
let _wsReconnectAttempts = 0;
const _wsCallbacks = { onResult: null, onStatus: null, onInit: null, onDone: null };

function wsConnect(sessionId) {
  wsDisconnect();
  _wsSessionId = sessionId;
  _wsReconnectAttempts = 0;
  _wsConnectInner();
}

function _wsConnectInner() {
  if (!token || !_wsSessionId) return;
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const url = proto + "//" + location.host + "/ws/" + _wsSessionId + "?token=" + encodeURIComponent(token);
  try {
    _ws = new WebSocket(url);
  } catch (e) {
    console.warn("[WS] connect error:", e);
    _wsScheduleReconnect();
    return;
  }
  _ws.onopen = function () {
    console.log("[WS] connected to session", _wsSessionId);
    _wsReconnectAttempts = 0;
    // Ping every 30s to keep alive
    _ws._pingInterval = setInterval(function () {
      if (_ws && _ws.readyState === WebSocket.OPEN) _ws.send("ping");
    }, 30000);
  };
  _ws.onmessage = function (evt) {
    try {
      const msg = JSON.parse(evt.data);
      if (msg.type === "result" && _wsCallbacks.onResult) {
        _wsCallbacks.onResult(msg);
      } else if (msg.type === "status") {
        if (msg.status === "done" || msg.status === "stopped") {
          if (_wsCallbacks.onDone) _wsCallbacks.onDone(msg.status);
        } else if (_wsCallbacks.onStatus) {
          _wsCallbacks.onStatus(msg.status);
        }
      } else if (msg.type === "init" && _wsCallbacks.onInit) {
        _wsCallbacks.onInit(msg);
      } else if (msg.type === "pong") {
        // heartbeat ack
      }
    } catch (e) {
      console.warn("[WS] parse error:", e);
    }
  };
  _ws.onclose = function () {
    console.log("[WS] disconnected");
    if (_ws && _ws._pingInterval) clearInterval(_ws._pingInterval);
    _ws = null;
    _wsScheduleReconnect();
  };
  _ws.onerror = function (e) {
    console.warn("[WS] error:", e);
  };
}

function _wsScheduleReconnect() {
  if (!_wsSessionId) return;
  if (_wsReconnectAttempts > 30) {
    // WebSocket failed — start HTTP polling fallback
    console.log("[WS] giving up on reconnect, starting HTTP poll fallback");
    _startPollFallback();
    return;
  }
  _wsReconnectAttempts++;
  const delay = Math.min(1000 * Math.pow(1.5, _wsReconnectAttempts), 10000);
  console.log("[WS] reconnecting in", Math.round(delay), "ms (attempt", _wsReconnectAttempts, ")");
  _wsReconnectTimer = setTimeout(_wsConnectInner, delay);
}

/* ── HTTP Polling Fallback (when WebSocket fails) ── */
let _pollTimer = null;
let _pollSessionId = null;

function _startPollFallback() {
  if (!_wsSessionId) return;
  _pollSessionId = _wsSessionId;
  console.log("[POLL] starting fallback polling for session", _pollSessionId);
  _pollOnce();
}

function _pollOnce() {
  if (!_pollSessionId || !token) return;
  fetch("/api/sessions/" + _pollSessionId, { headers: { Authorization: "Bearer " + token } })
    .then(r => r.json())
    .then(s => {
      if (!s || s.error) return;
      // Update stats
      if (s.status === "done" || s.status === "stopped") {
        if (_wsCallbacks.onDone) _wsCallbacks.onDone(s.status);
        return;
      }
      // Sync state from server
      const msg = {
        type: "init",
        status: s.status,
        checked: s.cards_checked || 0,
        live: s.live || 0,
        charged: s.charged || 0,
        dead: s.dead || 0,
        results: (s.results || []).slice(-50),
        cards: s.cards || [],
      };
      if (_wsCallbacks.onInit) _wsCallbacks.onInit(msg);
      // Continue polling
      _pollTimer = setTimeout(_pollOnce, 2000);
    })
    .catch(e => {
      console.warn("[POLL] error:", e);
      _pollTimer = setTimeout(_pollOnce, 5000);
    });
}

function _stopPollFallback() {
  if (_pollTimer) { clearTimeout(_pollTimer); _pollTimer = null; }
  _pollSessionId = null;
}

function wsDisconnect() {
  if (_wsReconnectTimer) { clearTimeout(_wsReconnectTimer); _wsReconnectTimer = null; }
  _stopPollFallback();
  if (_ws) {
    if (_ws._pingInterval) clearInterval(_ws._pingInterval);
    try { _ws.close(); } catch (_) {}
    _ws = null;
  }
  _wsSessionId = null;
  _wsReconnectAttempts = 0;
}

const $view = document.getElementById("view");
const $who = document.getElementById("who");
const $sidebar = document.getElementById("sidebar");
const $sidebarOverlay = document.getElementById("sidebarOverlay");
const $hamburger = document.getElementById("hamburgerBtn");
const $topbarTitle = document.getElementById("topbarTitle");
const $logout = document.getElementById("logout");

const ROUTES = ["dashboard", "shopify", "proxy", "sites", "settings"];
const TITLES = { dashboard: "Overview", shopify: "Checkers", proxy: "Proxy Checker", sites: "Sites Manager", settings: "Settings" };

function currentRoute() {
  const seg = location.pathname.replace(/\/+$/, "").split("/").pop();
  return ROUTES.includes(seg) ? seg : "dashboard";
}

function currentQuery() {
  return location.search;
}

function markNav(route) {
  document.querySelectorAll(".sidebar-link[data-route]").forEach((b) => {
    b.classList.toggle("active", b.dataset.route === route);
  });
  $topbarTitle.textContent = TITLES[route] || "Overview";
}

/* ── Sidebar ── */
function openSidebar() {
  $sidebar.classList.add("open");
  $sidebarOverlay.classList.add("open");
}
function closeSidebar() {
  $sidebar.classList.remove("open");
  $sidebarOverlay.classList.remove("open");
}

$hamburger.addEventListener("click", () => {
  $sidebar.classList.contains("open") ? closeSidebar() : openSidebar();
});
$sidebarOverlay.addEventListener("click", closeSidebar);

$sidebar.querySelectorAll(".sidebar-link[data-route]").forEach((link) => {
  link.addEventListener("click", () => {
    navigate(link.dataset.route);
    closeSidebar();
  });
});

/* ── Load session results into Shopify view ── */
async function loadSessionIntoView(sid) {
  const cardArea = document.getElementById("cardArea");
  const resultsBody = document.getElementById("resultsBody");
  const resultsEmpty = document.getElementById("resultsEmpty");
  const resCount = document.getElementById("resCount");
  const sChecked = document.getElementById("sChecked");
  const sLive = document.getElementById("sLive");
  const sCharged = document.getElementById("sCharged");
  const sDead = document.getElementById("sDead");

  try {
    const res = await fetch("/api/sessions/" + sid, { headers: { Authorization: "Bearer " + token } });
    const s = await res.json();

    // Update stats
    if (sChecked) sChecked.textContent = s.cards_checked || 0;
    if (sLive) sLive.textContent = s.live || 0;
    if (sCharged) sCharged.textContent = s.charged || 0;
    if (sDead) sDead.textContent = s.dead || 0;

    // Restore the card list into the textarea (session stores the raw cards)
    const cards = s.cards || [];
    if (cardArea) {
      const joined = cards.join("\n");
      cardArea.value = joined;
      const cc = document.getElementById("cardCount");
      if (cc) cc.textContent = cards.length + (cards.length === 1 ? " card" : " cards");
    }
    if (resCount) resCount.textContent = (s.results || []).length + " entries";

    // Load results into table
    const results = s.results || [];
    if (resultsEmpty) resultsEmpty.style.display = results.length ? "none" : "flex";
    if (resultsBody) resultsBody.innerHTML = "";

    results.forEach(r => {
      const st = (r.status || "").toUpperCase();
      let cls = "pending";
      if (st === "CHARGED") cls = "charged";
      else if (st === "APPROVED") cls = "live";
      else if (st === "DEAD") cls = "dead";
      const tr = document.createElement("tr");
      tr.innerHTML =
        '<td class="col-card">' + esc(r.card || "") + '</td>' +
        '<td class="col-resp">' + esc(r.response || "") + '</td>' +
        '<td class="col-status"><span class="sx-status-badge ' + cls + '">' + esc(st) + '</span></td>';
      resultsBody.prepend(tr);
    });
    if (resCount) resCount.textContent = results.length + " entries";

    // If session is still running, connect via WebSocket for real-time updates
    if (s.status === "running") {
      checkRunning = true;
      currentSessionId = sid;
      const sBtn = document.getElementById("startBtn");
      if (sBtn) {
        sBtn.classList.remove("sx-btn-orange");
        sBtn.classList.add("sx-btn-red");
        sBtn.innerHTML = '<svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="6" width="12" height="12" rx="2"/></svg> Stop';
      }
      let lastCount = results.length;
      // Remove checked cards from textarea in real-time
      const checkedCards = new Set((s.results || []).map(r => r.card));
      if (cardArea) {
        const remaining = cards.filter(c => !checkedCards.has(c));
        cardArea.value = remaining.join("\n");
        const cc2 = document.getElementById("cardCount");
        if (cc2) cc2.textContent = remaining.length + (remaining.length === 1 ? " card" : " cards");
      }
      // WebSocket callbacks
      _wsCallbacks.onResult = function (msg) {
        if (resultsEmpty) resultsEmpty.style.display = "none";
        const st2 = (msg.status || "").toUpperCase();
        let cls2 = "pending";
        if (st2 === "CHARGED") cls2 = "charged";
        else if (st2 === "APPROVED") cls2 = "live";
        else if (st2 === "DEAD") cls2 = "dead";
        const tr2 = document.createElement("tr");
        tr2.innerHTML =
          '<td class="col-card">' + esc(msg.card || "") + '</td>' +
          '<td class="col-resp">' + esc(msg.response || "") + '</td>' +
          '<td class="col-status"><span class="sx-status-badge ' + cls2 + '">' + esc(st2) + '</span></td>';
        resultsBody.prepend(tr2);
        lastCount++;
        checkedCount++;
        if (st2 === "CHARGED") chargedCount++;
        else if (st2 === "APPROVED") liveCount++;
        else if (st2 === "DEAD") deadCount++;
        const sStat = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
        sStat("sChecked", checkedCount);
        sStat("sLive", liveCount);
        sStat("sCharged", chargedCount);
        sStat("sDead", deadCount);
        if (resCount) resCount.textContent = lastCount + " entries";
        // Remove checked card from textarea
        if (cardArea) {
          const lines = cardArea.value.split("\n").filter(l => l.trim() && l.trim() !== msg.card);
          cardArea.value = lines.join("\n");
          const cc2 = document.getElementById("cardCount");
          if (cc2) cc2.textContent = lines.length + (lines.length === 1 ? " card" : " cards");
        }
      };
      _wsCallbacks.onInit = function (msg) {
        // If session already finished, stop UI immediately
        if (msg.status === "done" || msg.status === "stopped") {
          finishBatch();
          wsDisconnect();
          return;
        }
        // Re-sync state from server (after reconnect)
        if (msg.checked !== undefined) checkedCount = msg.checked;
        if (msg.live !== undefined) liveCount = msg.live;
        if (msg.charged !== undefined) chargedCount = msg.charged;
        if (msg.dead !== undefined) deadCount = msg.dead;
        const sStat = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
        sStat("sChecked", checkedCount);
        sStat("sLive", liveCount);
        sStat("sCharged", chargedCount);
        sStat("sDead", deadCount);
        // Re-render recent results
        if (msg.results && msg.results.length) {
          if (resultsEmpty) resultsEmpty.style.display = "none";
          resultsBody.innerHTML = "";
          msg.results.forEach(function (r) {
            const rst = (r.status || "").toUpperCase();
            let rcls = "pending";
            if (rst === "CHARGED") rcls = "charged";
            else if (rst === "APPROVED") rcls = "live";
            else if (rst === "DEAD") rcls = "dead";
            const tr = document.createElement("tr");
            tr.innerHTML =
              '<td class="col-card">' + esc(r.card || "") + '</td>' +
              '<td class="col-resp">' + esc(r.response || "") + '</td>' +
              '<td class="col-status"><span class="sx-status-badge ' + rcls + '">' + esc(rst) + '</span></td>';
            resultsBody.prepend(tr);
          });
          lastCount = msg.results.length;
          if (resCount) resCount.textContent = lastCount + " entries";
        }
        // Update card textarea — remove checked cards
        if (cardArea && msg.cards) {
          const checkedSet = new Set((msg.results || []).map(function (r) { return r.card; }));
          const remaining = msg.cards.filter(function (c) { return !checkedSet.has(c); });
          cardArea.value = remaining.join("\n");
          const cc2 = document.getElementById("cardCount");
          if (cc2) cc2.textContent = remaining.length + (remaining.length === 1 ? " card" : " cards");
        }
      };
      _wsCallbacks.onDone = function (status) {
        finishBatch();
        wsDisconnect();
      };
      _wsCallbacks.onStatus = null;
      wsConnect(sid);
    }
  } catch (e) {
    console.error("Load session error:", e);
  }
}

/* ── Views ── */
function bindViewHandlers() {
  // Reset stale checking state when view is (re)loaded
  checkRunning = false;
  currentSessionId = null;

  $view.querySelectorAll("[data-route]").forEach((el) => {
    el.addEventListener("click", () => navigate(el.dataset.route));
  });

  /* File import */
  const fileInput = document.getElementById("fileInput");
  const cardArea = document.getElementById("cardArea");
  const cardCount = document.getElementById("cardCount");

  if (fileInput && cardArea) {
    fileInput.addEventListener("change", (e) => {
      const file = e.target.files[0];
      if (!file) return;
      const reader = new FileReader();
      reader.onload = (ev) => {
        const text = ev.target.result.trim();
        if (cardArea.value.trim()) {
          cardArea.value += "\n" + text;
        } else {
          cardArea.value = text;
        }
        updateCardCount();
      };
      reader.readAsText(file);
      fileInput.value = "";
    });
  }

  /* Card count live update */
  if (cardArea && cardCount) {
    cardArea.addEventListener("input", updateCardCount);
  }

  /* Clear button */
  const clearBtn = document.getElementById("clearBtn");
  if (clearBtn && cardArea) {
    clearBtn.addEventListener("click", () => {
      if (checkRunning) stopCheck();
      cardArea.value = "";
      updateCardCount();
      resetStats();
    });
  }

  /* Start / Stop button */
  const startBtn = document.getElementById("startBtn");
  if (startBtn) {
    startBtn.addEventListener("click", () => {
      if (checkRunning) {
        stopCheck();
      } else {
        startCheck();
      }
    });
  }

  /* Export results */
  const exportBtn = document.getElementById("exportBtn");
  if (exportBtn) {
    exportBtn.addEventListener("click", exportResults);
  }

  /* ── Amount range auto-detect from sites ── */
  const minAmountEl = document.getElementById("minAmount");
  const maxAmountEl = document.getElementById("maxAmount");
  const gwSiteCountEl = document.getElementById("gwSiteCount");
  const gwAmountBadge = document.getElementById("gwAmountBadge");
  const amountSiteCountEl = document.getElementById("amountSiteCount");
  const refreshAmountsBtn = document.getElementById("refreshAmounts");
  const saveAmountsBtn = document.getElementById("saveAmounts");
  const confirmGatewayBtn = document.getElementById("confirmGateway");
  const gatewayModal = document.getElementById("gatewayModal");

  const AMOUNT_STORAGE_KEY = "webex_amount_range";

  function updateGwBadge() {
    const minV = minAmountEl ? parseFloat(minAmountEl.value) || 0 : 0;
    const maxV = maxAmountEl ? parseFloat(maxAmountEl.value) || 9999 : 9999;
    if (gwAmountBadge) gwAmountBadge.textContent = "$" + minV + "–$" + maxV;
  }

  function loadSavedAmounts() {
    try {
      const saved = JSON.parse(localStorage.getItem(AMOUNT_STORAGE_KEY));
      if (saved && saved.min !== undefined) {
        if (minAmountEl) minAmountEl.value = saved.min;
        if (maxAmountEl) maxAmountEl.value = saved.max;
        updateGwBadge();
        return true;
      }
    } catch (e) {}
    return false;
  }

  function saveAmounts() {
    const minV = minAmountEl ? minAmountEl.value : "";
    const maxV = maxAmountEl ? maxAmountEl.value : "";
    localStorage.setItem(AMOUNT_STORAGE_KEY, JSON.stringify({ min: minV, max: maxV }));
    updateGwBadge();
    // Visual feedback
    if (saveAmountsBtn) {
      saveAmountsBtn.textContent = "✓ Saved";
      saveAmountsBtn.style.background = "#22c55e";
      setTimeout(() => {
        saveAmountsBtn.innerHTML = '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/></svg> Save';
        saveAmountsBtn.style.background = "#34d399";
      }, 1500);
    }
    updateAmountSiteCount();
  }

  async function loadAmountStats(forceInputs) {
    try {
      const res = await fetch("/api/sites/stats", {
        headers: { Authorization: "Bearer " + token }
      });
      const data = await res.json();
      const count = data.count || 0;
      const source = data.source || "db";
      if (gwSiteCountEl) {
        gwSiteCountEl.textContent = count + " sites " + (source === "auto" ? "(auto-loaded)" : "stored");
      }
      if (forceInputs) {
        if (minAmountEl) minAmountEl.value = data.min || "0.50";
        if (maxAmountEl) maxAmountEl.value = data.max || "10.00";
        localStorage.setItem(AMOUNT_STORAGE_KEY, JSON.stringify({ min: minAmountEl.value, max: maxAmountEl.value }));
        updateGwBadge();
      }
      updateAmountSiteCount();
    } catch (e) {
      if (gwSiteCountEl) gwSiteCountEl.textContent = "sites unavailable";
    }
  }

  async function updateAmountSiteCount() {
    if (!amountSiteCountEl) return;
    const minV = minAmountEl ? parseFloat(minAmountEl.value) || 0 : 0;
    const maxV = maxAmountEl ? parseFloat(maxAmountEl.value) || 9999 : 9999;
    try {
      const res = await fetch("/api/sites", {
        headers: { Authorization: "Bearer " + token }
      });
      const data = await res.json();
      const sites = data.sites || [];
      const count = sites.filter(s => {
        const a = parseFloat(s.amount || "0");
        return a >= minV && a <= maxV;
      }).length;
      amountSiteCountEl.textContent = count + " sites in range";
    } catch (e) {
      amountSiteCountEl.textContent = "—";
    }
  }

  if (minAmountEl) minAmountEl.addEventListener("input", updateAmountSiteCount);
  if (maxAmountEl) maxAmountEl.addEventListener("input", updateAmountSiteCount);
  if (refreshAmountsBtn) {
    refreshAmountsBtn.addEventListener("click", () => {
      loadAmountStats(true);
    });
  }
  if (saveAmountsBtn) {
    saveAmountsBtn.addEventListener("click", saveAmounts);
  }
  if (confirmGatewayBtn) {
    confirmGatewayBtn.addEventListener("click", () => {
      saveAmounts();
      if (gatewayModal) gatewayModal.classList.remove("open");
    });
  }

  const editGatewayBtn = document.getElementById("editGateway");
  const amountEditPanel = document.getElementById("amountEditPanel");
  if (editGatewayBtn && amountEditPanel) {
    editGatewayBtn.addEventListener("click", () => {
      const isOpen = amountEditPanel.style.display !== "none";
      amountEditPanel.style.display = isOpen ? "none" : "block";
    });
  }

  // On load: always fetch DB stats (for site count + badge), use saved values for inputs if they exist
  if (minAmountEl || maxAmountEl) {
    if (!loadSavedAmounts()) {
      loadAmountStats(true);
    } else {
      loadAmountStats(false);
    }
  }

  /* ── Proxy page handlers ── */
  const proxyArea = document.getElementById("proxyArea");
  const proxyCount = document.getElementById("proxyCount");
  const proxyFileInput = document.getElementById("proxyFileInput");

  if (proxyFileInput && proxyArea) {
    proxyFileInput.addEventListener("change", (e) => {
      const file = e.target.files[0];
      if (!file) return;
      const reader = new FileReader();
      reader.onload = (ev) => {
        const text = ev.target.result.trim();
        proxyArea.value = proxyArea.value.trim() ? proxyArea.value + "\n" + text : text;
        updateProxyCount();
      };
      reader.readAsText(file);
      proxyFileInput.value = "";
    });
  }

  if (proxyArea && proxyCount) {
    proxyArea.addEventListener("input", updateProxyCount);
  }

  const proxyClearBtn = document.getElementById("proxyClearBtn");
  if (proxyClearBtn && proxyArea) {
    proxyClearBtn.addEventListener("click", () => {
      proxyArea.value = "";
      updateProxyCount();
      resetProxyStats();
    });
  }

  const proxyCheckBtn = document.getElementById("proxyCheckBtn");
  if (proxyCheckBtn) {
    proxyCheckBtn.addEventListener("click", checkProxies);
  }

  const proxyExportBtn = document.getElementById("proxyExportBtn");
  if (proxyExportBtn) {
    proxyExportBtn.addEventListener("click", exportSavedProxies);
  }

  const proxyDeleteAllBtn = document.getElementById("proxyDeleteAllBtn");
  if (proxyDeleteAllBtn) {
    proxyDeleteAllBtn.addEventListener("click", async () => {
      await fetch("/api/proxies/delete", {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: "Bearer " + token },
        body: JSON.stringify({}),
      });
      loadSavedProxies();
    });
  }

  /* ── Sites page handlers ── */
  const siteArea = document.getElementById("siteArea");
  const addSitesBtn = document.getElementById("addSitesBtn");
  const clearSitesBtn = document.getElementById("clearSitesBtn");
  const siteFileInput = document.getElementById("siteFileInput");
  const siteFilter = document.getElementById("siteFilter");
  const checkAllBtn = document.getElementById("checkAllBtn");
  const exportSitesBtn = document.getElementById("exportSitesBtn");
  const sitesBody = document.getElementById("sitesBody");
  const sitesEmpty = document.getElementById("sitesEmpty");
  const siteCountEl = document.getElementById("siteCount");
  const sitesPagination = document.getElementById("sitesPagination");
  const sitesPrev = document.getElementById("sitesPrev");
  const sitesNext = document.getElementById("sitesNext");
  const sitesPageInfo = document.getElementById("sitesPageInfo");
  const sTotal = document.getElementById("sTotalSites");
  const sValid = document.getElementById("sValidSites");
  const sInvalid = document.getElementById("sInvalidSites");
  const sErrors = document.getElementById("sErrorSites");

  if (siteArea) {
    let allSites = [];
    let sitesPage = 1;
    const SITES_PER_PAGE = 20;

    function updateSitesStats() {
      const total = allSites.length;
      const valid = allSites.filter(s => s.last_check === "valid").length;
      const invalid = allSites.filter(s => s.last_check === "invalid").length;
      const errors = allSites.filter(s => s.last_check === "error").length;
      if (sTotal) sTotal.textContent = total;
      if (sValid) sValid.textContent = valid;
      if (sInvalid) sInvalid.textContent = invalid;
      if (sErrors) sErrors.textContent = errors;
      if (siteCountEl) siteCountEl.textContent = total + " sites";
    }

    function renderSites() {
      const filter = (siteFilter?.value || "").toLowerCase();
      let filtered = allSites;
      if (filter) {
        filtered = allSites.filter(s => (s.url || "").toLowerCase().includes(filter));
      }

      const totalPages = Math.max(1, Math.ceil(filtered.length / SITES_PER_PAGE));
      sitesPage = Math.min(sitesPage, totalPages);
      const start = (sitesPage - 1) * SITES_PER_PAGE;
      const pageItems = filtered.slice(start, start + SITES_PER_PAGE);

      if (!pageItems.length) {
        if (sitesBody) sitesBody.innerHTML = "";
        if (sitesEmpty) sitesEmpty.style.display = "flex";
        if (sitesPagination) sitesPagination.style.display = "none";
        return;
      }
      if (sitesEmpty) sitesEmpty.style.display = "none";

      let html = "";
      pageItems.forEach((site, i) => {
        const idx = start + i + 1;
        const url = esc(site.url || "N/A");
        const amount = esc(site.amount || "0.00");
        const gateway = esc(site.gateway || "Shopify");
        const lastCheck = site.last_check || "N/A";
        const checkedAt = site.checked_at ? new Date(site.checked_at * 1000).toLocaleString() : "Never";

        let statusBadge = "";
        if (lastCheck === "valid") statusBadge = '<span class="sx-status-badge live">VALID</span>';
        else if (lastCheck === "invalid") statusBadge = '<span class="sx-status-badge dead">INVALID</span>';
        else if (lastCheck === "error") statusBadge = '<span class="sx-status-badge pending">ERROR</span>';
        else statusBadge = '<span class="sx-status-badge pending">N/A</span>';

        html += '<tr class="site-row" data-url="' + url + '">' +
          '<td class="col-status">' + idx + '</td>' +
          '<td class="col-card">' + url + '</td>' +
          '<td>$' + amount + '</td>' +
          '<td>' + gateway + '</td>' +
          '<td class="col-status">' + statusBadge + '</td>' +
          '<td style="font-size:11px;color:var(--muted)">' + esc(checkedAt) + '</td>' +
          '<td class="col-status">' +
            '<button class="sx-btn-outline sm site-check-btn" data-url="' + url + '" title="Check">⚡</button> ' +
            '<button class="sx-btn-outline sm site-remove-btn" style="color:#ef4444;border-color:rgba(239,68,68,0.4)" data-url="' + url + '" title="Remove">✕</button>' +
          '</td>' +
        '</tr>';
      });

      if (sitesBody) sitesBody.innerHTML = html;

      // Pagination
      if (sitesPagination) {
        if (totalPages > 1) {
          sitesPagination.style.display = "flex";
          if (sitesPageInfo) sitesPageInfo.textContent = "Page " + sitesPage + " / " + totalPages;
          if (sitesPrev) sitesPrev.disabled = sitesPage <= 1;
          if (sitesNext) sitesNext.disabled = sitesPage >= totalPages;
        } else {
          sitesPagination.style.display = "none";
        }
      }

      // Bind row action buttons
      sitesBody.querySelectorAll(".site-check-btn").forEach(btn => {
        btn.addEventListener("click", async () => {
          const u = btn.dataset.url;
          btn.disabled = true;
          btn.textContent = "…";
          try {
            await fetch("/api/sites/check", {
              method: "POST",
              headers: { "Content-Type": "application/json", Authorization: "Bearer " + token },
              body: JSON.stringify({ url: u }),
            });
            await loadSites();
          } catch (e) { console.error("Check site error:", e); }
          btn.disabled = false;
          btn.textContent = "⚡";
        });
      });

      sitesBody.querySelectorAll(".site-remove-btn").forEach(btn => {
        btn.addEventListener("click", async () => {
          const u = btn.dataset.url;
          if (!confirm("Remove " + u + "?")) return;
          try {
            await fetch("/api/sites/remove", {
              method: "POST",
              headers: { "Content-Type": "application/json", Authorization: "Bearer " + token },
              body: JSON.stringify({ url: u }),
            });
            await loadSites();
          } catch (e) { console.error("Remove site error:", e); }
        });
      });
    }

    async function loadSites() {
      try {
        const res = await fetch("/api/sites", {
          headers: { Authorization: "Bearer " + token }
        });
        const data = await res.json();
        allSites = data.sites || [];
        updateSitesStats();
        renderSites();
      } catch (e) {
        console.error("Load sites error:", e);
      }
    }

    // Add sites
    if (addSitesBtn) {
      addSitesBtn.addEventListener("click", async () => {
        const raw = siteArea?.value?.trim();
        if (!raw) return;
        addSitesBtn.disabled = true;
        addSitesBtn.textContent = "Adding...";
        try {
          await fetch("/api/sites/add", {
            method: "POST",
            headers: { "Content-Type": "application/json", Authorization: "Bearer " + token },
            body: JSON.stringify({ urls: raw }),
          });
          siteArea.value = "";
          await loadSites();
        } catch (e) { console.error("Add sites error:", e); }
        addSitesBtn.disabled = false;
        addSitesBtn.innerHTML = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="16"/><line x1="8" y1="12" x2="16" y2="12"/></svg> Add Sites';
      });
    }

    // Clear textarea
    if (clearSitesBtn) {
      clearSitesBtn.addEventListener("click", () => {
        siteArea.value = "";
      });
    }

    // File import
    if (siteFileInput) {
      siteFileInput.addEventListener("change", (e) => {
        const file = e.target.files[0];
        if (!file) return;
        const reader = new FileReader();
        reader.onload = (ev) => {
          const text = ev.target.result.trim();
          siteArea.value = siteArea.value.trim() ? siteArea.value + "\n" + text : text;
        };
        reader.readAsText(file);
        siteFileInput.value = "";
      });
    }

    // Filter
    if (siteFilter) {
      siteFilter.addEventListener("input", () => {
        sitesPage = 1;
        renderSites();
      });
    }

    // Check All
    if (checkAllBtn) {
      checkAllBtn.addEventListener("click", async () => {
        checkAllBtn.disabled = true;
        checkAllBtn.textContent = "Checking...";
        try {
          await fetch("/api/sites/check-all", {
            method: "POST",
            headers: { "Content-Type": "application/json", Authorization: "Bearer " + token },
            body: JSON.stringify({}),
          });
          await loadSites();
        } catch (e) { console.error("Check all error:", e); }
        checkAllBtn.disabled = false;
        checkAllBtn.innerHTML = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="13 17 18 12 13 7"/><polyline points="6 17 11 12 6 7"/></svg> Check All';
      });
    }

    // Export
    if (exportSitesBtn) {
      exportSitesBtn.addEventListener("click", async () => {
        try {
          const res = await fetch("/api/sites/export", {
            headers: { Authorization: "Bearer " + token }
          });
          const data = await res.json();
          const blob = new Blob([data.text || ""], { type: "text/plain" });
          const a = document.createElement("a");
          a.href = URL.createObjectURL(blob);
          a.download = "sites_" + Date.now() + ".txt";
          a.click();
          URL.revokeObjectURL(a.href);
        } catch (e) { console.error("Export error:", e); }
      });
    }

    // Pagination
    if (sitesPrev) {
      sitesPrev.addEventListener("click", () => {
        if (sitesPage > 1) { sitesPage--; renderSites(); }
      });
    }
    if (sitesNext) {
      sitesNext.addEventListener("click", () => {
        sitesPage++; renderSites();
      });
    }

    // Initial load
    loadSites();
  }

  /* ── Sessions list (dashboard) ── */
  const sessionsList = document.getElementById("sessionsList");
  const sessionsEmpty = document.getElementById("sessionsEmpty");
  const sessionCountEl = document.getElementById("sessionCount");
  async function loadSessions() {
    if (!sessionsList) return;
    try {
      const res = await fetch("/api/sessions", { headers: { Authorization: "Bearer " + token } });
      const data = await res.json();
      const sessions = data.sessions || [];
      if (sessionCountEl) sessionCountEl.textContent = sessions.length + " sessions";
      if (!sessions.length) {
        if (sessionsEmpty) sessionsEmpty.style.display = "flex";
        return;
      }
      if (sessionsEmpty) sessionsEmpty.style.display = "none";
      // Remove old rows (keep empty div)
      sessionsList.querySelectorAll(".session-row").forEach(r => r.remove());

      sessions.forEach(s => {
        const sid = s.session_id || "";
        const status = s.status || "unknown";
        const checked = s.cards_checked || 0;
        const total = s.cards_count || 0;
        const live = s.live || 0;
        const charged = s.charged || 0;
        const dead = s.dead || 0;
        const started = s.started_at ? new Date(s.started_at * 1000).toLocaleString() : "—";
        const duration = s.finished_at && s.started_at
          ? Math.round((s.finished_at - s.started_at)) + "s"
          : (status === "running" ? "live" : "—");

        let statusBadge = "";
        if (status === "running") statusBadge = '<span class="sx-status-badge live">RUNNING</span>';
        else if (status === "done") statusBadge = '<span class="sx-status-badge charged">DONE</span>';
        else if (status === "stopped") statusBadge = '<span class="sx-status-badge pending">STOPPED</span>';
        else statusBadge = '<span class="sx-status-badge dead">' + esc(status.toUpperCase()) + '</span>';

        const row = document.createElement("div");
        row.className = "session-row";
        row.style.cssText = "display:flex;align-items:center;gap:10px;padding:10px 12px;background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.06);border-radius:8px;cursor:pointer;transition:background 0.15s";
        row.onmouseenter = () => row.style.background = "rgba(255,255,255,0.06)";
        row.onmouseleave = () => row.style.background = "rgba(255,255,255,0.03)";
        row.innerHTML =
          '<div style="flex:1;min-width:0">' +
            '<div style="display:flex;align-items:center;gap:6px;margin-bottom:4px">' +
              '<span style="font-family:monospace;font-size:11px;color:#a78bfa">' + esc(sid.slice(0, 8)) + '</span>' +
              statusBadge +
            '</div>' +
            '<div style="font-size:11px;color:var(--muted)">' +
              checked + '/' + total + ' checked · ' +
              '<span style="color:#34d399">' + live + ' live</span> · ' +
              '<span style="color:#f59e0b">' + charged + ' charged</span> · ' +
              '<span style="color:#ef4444">' + dead + ' dead</span>' +
            '</div>' +
            '<div style="font-size:10px;color:var(--muted);margin-top:2px">' + started + ' · ' + duration + '</div>' +
          '</div>' +
          '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#666" stroke-width="2"><polyline points="9 18 15 12 9 6"/></svg>';

        row.addEventListener("click", () => openSession(sid));
        sessionsList.appendChild(row);
      });
    } catch (e) {
      console.error("Load sessions error:", e);
    }
  }

  function openSession(sid) {
    // Open a preview modal instead of navigating directly
    const modal = document.getElementById("sessModal");
    if (!modal) { navigate("shopify?session_id=" + sid); return; }

    const setId = document.getElementById("spvId");
    const setStatus = document.getElementById("spvStatus");
    const setChecked = document.getElementById("spvChecked");
    const setLive = document.getElementById("spvLive");
    const setCharged = document.getElementById("spvCharged");
    const setDead = document.getElementById("spvDead");
    const setCards = document.getElementById("spvCards");
    const setSites = document.getElementById("spvSites");
    const setProxies = document.getElementById("spvProxies");
    const setThreads = document.getElementById("spvThreads");
    const setRange = document.getElementById("spvRange");
    const setTime = document.getElementById("spvTime");
    const resBody = document.getElementById("spvResults");
    const resEmpty = document.getElementById("spvEmpty");

    modal.classList.add("open");
    if (setId) setId.textContent = sid;
    if (setStatus) { setStatus.textContent = "…"; setStatus.className = "sx-status-badge pending"; }
    if (setChecked) setChecked.textContent = "…";
    if (setLive) setLive.textContent = "…";
    if (setCharged) setCharged.textContent = "…";
    if (setDead) setDead.textContent = "…";
    if (resBody) resBody.innerHTML = "";
    if (resEmpty) resEmpty.style.display = "flex";

    // Load session details
    fetch("/api/sessions/" + sid, { headers: { Authorization: "Bearer " + token } })
      .then(r => r.json())
      .then(s => {
        const st = (s.status || "unknown").toUpperCase();
        let cls = "pending";
        if (st === "RUNNING") cls = "live";
        else if (st === "DONE") cls = "charged";
        else if (st === "STOPPED") cls = "pending";
        if (setStatus) { setStatus.textContent = st; setStatus.className = "sx-status-badge " + cls; }
        if (setChecked) setChecked.textContent = s.cards_checked || 0;
        if (setLive) setLive.textContent = s.live || 0;
        if (setCharged) setCharged.textContent = s.charged || 0;
        if (setDead) setDead.textContent = s.dead || 0;
        if (setCards) setCards.textContent = (s.cards_checked || 0) + "/" + (s.cards_count || 0);
        if (setSites) setSites.textContent = s.sites_count || 0;
        if (setProxies) setProxies.textContent = s.proxies_count || 0;
        if (setThreads) setThreads.textContent = s.threads || 0;
        if (setRange) setRange.textContent = "$" + (s.min_amount || 0) + "–$" + (s.max_amount || 0);
        if (setTime) setTime.textContent = s.started_at ? new Date(s.started_at * 1000).toLocaleString() : "—";

        const results = s.results || [];
        if (resBody) resBody.innerHTML = "";
        if (resEmpty) resEmpty.style.display = results.length ? "none" : "flex";
        results.forEach(r => {
          const rst = (r.status || "").toUpperCase();
          let rcls = "pending";
          if (rst === "CHARGED") rcls = "charged";
          else if (rst === "APPROVED") rcls = "live";
          else if (rst === "DEAD") rcls = "dead";
          const tr = document.createElement("tr");
          tr.innerHTML =
            '<td class="col-card">' + esc(r.card || "") + '</td>' +
            '<td class="col-resp">' + esc(r.response || "") + '</td>' +
            '<td class="col-status"><span class="sx-status-badge ' + rcls + '">' + esc(rst) + '</span></td>';
          if (resBody) resBody.prepend(tr);
        });

        // View button → open full session page
        const vBtn = document.getElementById("spvViewBtn");
        if (vBtn) {
          vBtn.onclick = () => {
            modal.classList.remove("open");
            navigate("shopify?session_id=" + sid);
          };
        }
        // Stop button → stop session, then refresh preview
        const sBtn = document.getElementById("spvStopBtn");
        if (sBtn) {
          sBtn.disabled = st !== "RUNNING";
          sBtn.style.opacity = st !== "RUNNING" ? "0.4" : "1";
          sBtn.onclick = async () => {
            if (!confirm("Stop session " + sid.slice(0, 8) + "?")) return;
            try {
              await fetch("/api/sessions/stop", {
                method: "POST",
                headers: { "Content-Type": "application/json", Authorization: "Bearer " + token },
                body: JSON.stringify({ session_id: sid }),
              });
            } catch (_) {}
            openSession(sid); // refresh preview
            loadSessions();   // refresh list
          };
        }
      })
      .catch(() => {
        if (setStatus) { setStatus.textContent = "ERROR"; setStatus.className = "sx-status-badge dead"; }
      });
  }

  // Load sessions on dashboard view
  if (sessionsList) loadSessions();

  /* ── Session ID in URL — load session results into Shopify view ── */
  const urlParams = new URLSearchParams(location.search);
  const sessionIdParam = urlParams.get("session_id") || localStorage.getItem("webex_active_session");
  // Only load session if explicitly in URL or localStorage has active session
  if (sessionIdParam && cardArea) {
    loadSessionIntoView(sessionIdParam);
  }

  if (proxyArea) {
    updateProxyCount();
    loadSavedProxies();
  }
}

/* ── Checking engine ── */
let checkRunning = false;
let stopRequested = false;
let currentSessionId = null;
let checkedCount = 0;
let liveCount = 0;
let chargedCount = 0;
let deadCount = 0;

function resetStats() {
  checkedCount = 0; liveCount = 0; chargedCount = 0; deadCount = 0;
  const s = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
  s("sChecked", "0"); s("sLive", "0"); s("sCharged", "0"); s("sDead", "0");
  const body = document.getElementById("resultsBody");
  if (body) body.innerHTML = "";
  const empty = document.getElementById("resultsEmpty");
  if (empty) empty.style.display = "flex";
  const resCount = document.getElementById("resCount");
  if (resCount) resCount.textContent = "0 entries";
  const wrap = document.getElementById("progressWrap");
  if (wrap) wrap.style.display = "none";
}

function stopCheck() {
  stopRequested = true;
  localStorage.removeItem("webex_active_session");
  wsDisconnect();
  _wsCallbacks.onResult = null;
  _wsCallbacks.onInit = null;
  _wsCallbacks.onDone = null;
  _wsCallbacks.onStatus = null;
  // Mark session as stopped if user manually stopped
  if (currentSessionId) {
    try { fetch("/api/sessions/stop", { method: "POST", headers: { "Content-Type": "application/json", Authorization: "Bearer " + token }, body: JSON.stringify({ session_id: currentSessionId }) }); } catch (_) {}
    currentSessionId = null;
  }
  const startBtn = document.getElementById("startBtn");
  if (startBtn) {
    startBtn.classList.remove("sx-btn-red");
    startBtn.classList.add("sx-btn-orange");
    startBtn.innerHTML = '<svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><polygon points="5 3 19 12 5 21 5 3"/></svg> Start Check';
  }
}

async function startCheck() {
  const cardArea = document.getElementById("cardArea");
  if (!cardArea) return;
  const cards = cardArea.value.trim().split("\n").map((l) => l.trim()).filter(Boolean);
  if (!cards.length) return;

  const minEl = document.getElementById("minAmount");
  const maxEl = document.getElementById("maxAmount");
  const minAmt = minEl ? parseFloat(minEl.value) || 0 : 0;
  const maxAmt = maxEl ? parseFloat(maxEl.value) || 9999 : 9999;

  // Fetch check info from backend
  let info;
  try {
    const res = await fetch("/api/check/info", {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: "Bearer " + token },
      body: JSON.stringify({ min_amount: minAmt, max_amount: maxAmt, cards_count: cards.length }),
    });
    info = await res.json();
  } catch (e) {
    info = { sites_count: 0, proxies_count: 0, threads: 5, session_id: "ERR" };
  }

  // Fill confirmation modal
  const startModal = document.getElementById("startModal");
  const sid = document.getElementById("sessionIdDisplay");
  if (sid) sid.textContent = info.session_id;
  const cs = document.getElementById("confirmSites");
  if (cs) cs.textContent = info.sites_count;
  const cp = document.getElementById("confirmProxies");
  if (cp) cp.textContent = info.proxies_count;
  const ct = document.getElementById("confirmThreads");
  if (ct) ct.textContent = info.threads;
  const cc = document.getElementById("confirmCards");
  if (cc) cc.textContent = cards.length;
  if (startModal) startModal.classList.add("open");

  // Wait for user confirm or cancel
  const confirmed = await new Promise((resolve) => {
    const cBtn = document.getElementById("confirmStartBtn");
    const onYes = () => { cleanup(); resolve(true); };
    const onNo = () => { cleanup(); resolve(false); };
    const cleanup = () => {
      if (cBtn) cBtn.removeEventListener("click", onYes);
      if (startModal) startModal.removeEventListener("click", onBg);
      if (startModal) startModal.classList.remove("open");
    };
    const onBg = (e) => { if (e.target === startModal) { cleanup(); resolve(false); } };
    if (cBtn) cBtn.addEventListener("click", onYes);
    if (startModal) startModal.addEventListener("click", onBg);
  });

  if (!confirmed) return;

  // Start the background job on the server — checking continues even if
  // this page/browser is closed. Poll /api/sessions/{id} for live results.
  let started;
  try {
    const res = await fetch("/api/check/start", {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: "Bearer " + token },
      body: JSON.stringify({
        cards: cards,
        min_amount: minAmt,
        max_amount: maxAmt,
        session_id: info.session_id || "",
      }),
    });
    started = await res.json();
    if (!res.ok) throw new Error(started.error || "Start failed");
  } catch (err) {
    alert("Failed to start check: " + err.message);
    return;
  }

  // Update URL without reload, then dynamically switch to session view
  history.replaceState({}, "", "/shopify?session_id=" + started.session_id);
  // Save session_id to localStorage for recovery on page reload/back
  localStorage.setItem("webex_active_session", started.session_id);

  // Reset stats
  checkRunning = true;
  currentSessionId = started.session_id;
  const s = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
  s("sChecked", "0"); s("sLive", "0"); s("sCharged", "0"); s("sDead", "0");

  // Clear results table, show empty state
  const resultsBody = document.getElementById("resultsBody");
  const resultsEmpty = document.getElementById("resultsEmpty");
  const resCount = document.getElementById("resCount");
  if (resultsBody) resultsBody.innerHTML = "";
  if (resultsEmpty) resultsEmpty.style.display = "flex";
  if (resCount) resCount.textContent = "0 entries";

  // Change Start button to Stop
  const startBtn = document.getElementById("startBtn");
  if (startBtn) {
    startBtn.classList.remove("sx-btn-orange");
    startBtn.classList.add("sx-btn-red");
    startBtn.innerHTML = '<svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="6" width="12" height="12" rx="2"/></svg> Stop';
  }

  // Connect via WebSocket for real-time results (no polling)
  let lastCount = 0;
  _wsCallbacks.onResult = function (msg) {
    if (resultsEmpty) resultsEmpty.style.display = "none";
    const st2 = (msg.status || "").toUpperCase();
    let cls2 = "pending";
    if (st2 === "CHARGED") cls2 = "charged";
    else if (st2 === "APPROVED") cls2 = "live";
    else if (st2 === "DEAD") cls2 = "dead";
    const tr2 = document.createElement("tr");
    tr2.innerHTML =
      '<td class="col-card">' + esc(msg.card || "") + '</td>' +
      '<td class="col-resp">' + esc(msg.response || "") + '</td>' +
      '<td class="col-status"><span class="sx-status-badge ' + cls2 + '">' + esc(st2) + '</span></td>';
    resultsBody.prepend(tr2);
    lastCount++;
    checkedCount++;
    if (st2 === "CHARGED") chargedCount++;
    else if (st2 === "APPROVED") liveCount++;
    else if (st2 === "DEAD") deadCount++;
    s("sChecked", checkedCount);
    s("sLive", liveCount);
    s("sCharged", chargedCount);
    s("sDead", deadCount);
    if (resCount) resCount.textContent = lastCount + " entries";
    // Remove checked card from textarea in real-time
    if (cardArea) {
      const lines = cardArea.value.split("\n").filter(l => l.trim() && l.trim() !== msg.card);
      cardArea.value = lines.join("\n");
      const cc2 = document.getElementById("cardCount");
      if (cc2) cc2.textContent = lines.length + (lines.length === 1 ? " card" : " cards");
    }
  };
  _wsCallbacks.onInit = function (msg) {
    // If session already finished, stop UI immediately
    if (msg.status === "done" || msg.status === "stopped") {
      finishBatch();
      wsDisconnect();
      return;
    }
    // Re-sync after reconnect
    if (msg.checked !== undefined) checkedCount = msg.checked;
    if (msg.live !== undefined) liveCount = msg.live;
    if (msg.charged !== undefined) chargedCount = msg.charged;
    if (msg.dead !== undefined) deadCount = msg.dead;
    s("sChecked", checkedCount);
    s("sLive", liveCount);
    s("sCharged", chargedCount);
    s("sDead", deadCount);
    if (msg.results && msg.results.length) {
      if (resultsEmpty) resultsEmpty.style.display = "none";
      resultsBody.innerHTML = "";
      msg.results.forEach(function (r) {
        const rst = (r.status || "").toUpperCase();
        let rcls = "pending";
        if (rst === "CHARGED") rcls = "charged";
        else if (rst === "APPROVED") rcls = "live";
        else if (rst === "DEAD") rcls = "dead";
        const tr = document.createElement("tr");
        tr.innerHTML =
          '<td class="col-card">' + esc(r.card || "") + '</td>' +
          '<td class="col-resp">' + esc(r.response || "") + '</td>' +
          '<td class="col-status"><span class="sx-status-badge ' + rcls + '">' + esc(rst) + '</span></td>';
        resultsBody.prepend(tr);
      });
      lastCount = msg.results.length;
      if (resCount) resCount.textContent = lastCount + " entries";
    }
    // Update card textarea — remove already checked cards
    if (cardArea && msg.cards) {
      const checkedSet = new Set((msg.results || []).map(r => r.card));
      const remaining = msg.cards.filter(c => !checkedSet.has(c));
      cardArea.value = remaining.join("\n");
      const cc2 = document.getElementById("cardCount");
      if (cc2) cc2.textContent = remaining.length + (remaining.length === 1 ? " card" : " cards");
    }
  };
  _wsCallbacks.onDone = function (status) {
    finishBatch();
    wsDisconnect();
  };
  _wsCallbacks.onStatus = null;
  wsConnect(started.session_id);
}

/* Reset UI after a batch completes — never auto-closes the session */
function finishBatch() {
  checkRunning = false;
  currentSessionId = null;
  localStorage.removeItem("webex_active_session");
  wsDisconnect();
  _wsCallbacks.onResult = null;
  _wsCallbacks.onInit = null;
  _wsCallbacks.onDone = null;
  _wsCallbacks.onStatus = null;
  const startBtn = document.getElementById("startBtn");
  if (startBtn) {
    startBtn.classList.remove("sx-btn-red");
    startBtn.classList.add("sx-btn-orange");
    startBtn.innerHTML = '<svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><polygon points="5 3 19 12 5 21 5 3"/></svg> Start Check';
  }
  const wrap = document.getElementById("progressWrap");
  if (wrap) wrap.style.display = "none";
}

function updateProgress(done, total) {
  const pCurrent = document.getElementById("pCurrent");
  const pPercent = document.getElementById("pPercent");
  const pFill = document.getElementById("pFill");
  if (!pCurrent || !pPercent || !pFill) return;
  pCurrent.textContent = done;
  const pct = total ? Math.round((done / total) * 100) : 0;
  pPercent.textContent = pct + "%";
  pFill.style.width = pct + "%";
}

function onResult(card, data) {
  checkedCount++;
  const status = (data.status || "ERROR").toUpperCase();

  let badgeClass = "pending";
  if (status === "CHARGED") { badgeClass = "charged"; chargedCount++; }
  else if (status === "APPROVED") { badgeClass = "live"; liveCount++; }
  else if (status === "DEAD") { badgeClass = "dead"; deadCount++; }
  else badgeClass = "pending";

  const s = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
  s("sChecked", checkedCount);
  s("sLive", liveCount);
  s("sCharged", chargedCount);
  s("sDead", deadCount);

  const body = document.getElementById("resultsBody");
  const empty = document.getElementById("resultsEmpty");
  if (body && empty) {
    empty.style.display = "none";
    const tr = document.createElement("tr");
    tr.innerHTML =
      '<td class="col-card">' + esc(card) + "</td>" +
      '<td class="col-resp">' + esc(data.response || "N/A") + "</td>" +
      '<td class="col-status"><span class="sx-status-badge ' + badgeClass + '">' + esc(status) + "</span></td>";
    body.prepend(tr);
    const resCount = document.getElementById("resCount");
    if (resCount) resCount.textContent = body.children.length + " entries";
  }
}

function esc(str) {
  return String(str ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function exportResults() {
  const rows = document.querySelectorAll("#resultsBody tr");
  if (!rows.length) return;
  let txt = "CARD | RESPONSE | STATUS\n";
  rows.forEach((tr) => {
    const tds = tr.querySelectorAll("td");
    txt += (tds[0]?.textContent || "") + " | " + (tds[1]?.textContent || "") + " | " + (tds[2]?.textContent || "") + "\n";
  });
  const blob = new Blob([txt], { type: "text/plain" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "results_" + Date.now() + ".txt";
  a.click();
  URL.revokeObjectURL(a.href);
}

/* ── Proxy engine ── */
function updateProxyCount() {
  const area = document.getElementById("proxyArea");
  const count = document.getElementById("proxyCount");
  if (!area || !count) return;
  const n = area.value.trim().split("\n").filter((l) => l.trim()).length;
  count.textContent = n + " prox" + (n !== 1 ? "ies" : "y");
}

function resetProxyStats() {
  const s = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
  s("pTotalChecked", "0"); s("pLive", "0"); s("pDead", "0");
  const wrap = document.getElementById("proxyProgressWrap");
  if (wrap) wrap.style.display = "none";
}

async function checkProxies() {
  const area = document.getElementById("proxyArea");
  if (!area) return;
  const lines = area.value.trim().split("\n").map((l) => l.trim()).filter(Boolean);
  if (!lines.length) return;

  const timeoutInput = document.getElementById("proxyTimeout");
  const timeout = parseInt(timeoutInput?.value || "10", 10) || 10;

  const btn = document.getElementById("proxyCheckBtn");
  if (btn) btn.disabled = true;

  resetProxyStats();
  const wrap = document.getElementById("proxyProgressWrap");
  if (wrap) wrap.style.display = "block";
  const pTotal = document.getElementById("ppTotal");
  if (pTotal) pTotal.textContent = lines.length;

  const s = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };

  try {
    const res = await fetch("/api/proxies/check", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: "Bearer " + token,
      },
      body: JSON.stringify({ proxies: lines.join("\n"), timeout }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Check failed");

    s("pTotalChecked", data.total);
    s("pLive", data.live);
    s("pDead", data.dead);
    s("pSaved", data.saved);
    s("ppCurrent", data.total);
    s("ppPercent", "100%");
    const fill = document.getElementById("ppFill");
    if (fill) fill.style.width = "100%";

    await loadSavedProxies();
  } catch (err) {
    s("ppPercent", "error");
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function loadSavedProxies() {
  const body = document.getElementById("proxyBody");
  const empty = document.getElementById("proxyEmpty");
  const count = document.getElementById("pSavedCount");
  const stat = document.getElementById("pSaved");
  if (!body) return;
  try {
    const res = await fetch("/api/proxies", {
      headers: { Authorization: "Bearer " + token },
    });
    const data = await res.json();
    const proxies = data.proxies || [];
    if (count) count.textContent = proxies.length;
    if (stat) stat.textContent = proxies.length;
    body.innerHTML = "";
    if (empty) empty.style.display = proxies.length ? "none" : "flex";
    proxies.forEach((p) => {
      const tr = document.createElement("tr");
      tr.innerHTML =
        '<td class="col-card">' + esc(p) + "</td>" +
        '<td class="col-status"><span class="sx-status-badge live">SAVED</span></td>';
      body.appendChild(tr);
    });
  } catch (_) {}
}

function exportSavedProxies() {
  const rows = document.querySelectorAll("#proxyBody tr");
  if (!rows.length) return;
  let txt = "";
  rows.forEach((tr) => {
    const tds = tr.querySelectorAll("td");
    txt += (tds[0]?.textContent || "") + "\n";
  });
  const blob = new Blob([txt], { type: "text/plain" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "proxies_" + Date.now() + ".txt";
  a.click();
  URL.revokeObjectURL(a.href);
}

function updateCardCount() {
  const cardArea = document.getElementById("cardArea");
  const cardCount = document.getElementById("cardCount");
  if (!cardArea || !cardCount) return;
  const lines = cardArea.value.trim().split("\n").filter((l) => l.trim());
  cardCount.textContent = lines.length + " card" + (lines.length !== 1 ? "s" : "");
}

async function loadView(route) {
  markNav(route);
  $view.innerHTML = '<p class="hint">Loading…</p>';
  try {
    const res = await fetch(`/static/views/${route}.html?v=7`);
    if (!res.ok) throw new Error("View not found");
    $view.innerHTML = await res.text();
    bindViewHandlers();
  } catch (err) {
    $view.innerHTML = `<p class="status err">${err.message}</p>`;
  }
}

function navigate(route) {
  const [base, query] = route.split("?");
  if (!ROUTES.includes(base)) base = "dashboard";
  const url = "/" + base + (query ? "?" + query : "");
  history.pushState({}, "", url);
  loadView(base);
}

window.addEventListener("popstate", () => loadView(currentRoute()));

/* ── Ping ── */
async function pingServer() {
  const el = document.getElementById("sidebarPing");
  if (!el) return;
  try {
    const t0 = performance.now();
    await fetch("/api/ping");
    el.textContent = Math.round(performance.now() - t0) + "ms";
  } catch (_) {
    el.textContent = "—";
  }
}

/* ── Boot ── */
async function boot() {
  if (!token) {
    location.replace("/");
    return;
  }
  try {
    const res = await fetch("/api/me", {
      headers: { Authorization: "Bearer " + token },
    });
    if (!res.ok) throw new Error("unauthorized");
    const { user } = await res.json();
    const name = [user.first_name, user.username ? "@" + user.username : ""]
      .filter(Boolean)
      .join(" · ");
    $who.textContent = name;
    loadView(currentRoute());
    pingServer();
    setInterval(pingServer, 30000);
  } catch (_) {
    localStorage.removeItem("webex_token");
    localStorage.removeItem("webex_last_session");
    location.replace("/");
  }
}

$logout.addEventListener("click", async () => {
  try {
    await fetch("/api/logout", {
      method: "POST",
      headers: { Authorization: "Bearer " + token },
    });
  } catch (_) {}
  localStorage.removeItem("webex_token");
  localStorage.removeItem("webex_last_session");
  location.replace("/");
});

/* ══════════════════════════════════════════════════════════════════════
   THEME SYSTEM — VS Code inspired themes with real-time switching
   ══════════════════════════════════════════════════════════════════════ */

const THEMES = {
  "dark-plus": {
    name: "Dark+",
    bg: "#1e1e1e", bg2: "#252526", card: "rgba(37,37,38,0.92)",
    cardSolid: "#2d2d2d", border: "rgba(255,255,255,0.10)", border2: "rgba(255,255,255,0.18)",
    fg: "#d4d4d4", muted: "#808080", ring: "#007acc",
    purple: "#c586c0", amber: "#dcdcaa", green: "#6a9955", red: "#f44747",
    blue: "#569cd6", orange: "#ce9178",
  },
  "monokai": {
    name: "Monokai Pro",
    bg: "#2d2a2e", bg2: "#333035", card: "rgba(51,48,53,0.92)",
    cardSolid: "#403e41", border: "rgba(255,255,255,0.10)", border2: "rgba(255,255,255,0.18)",
    fg: "#fcfcfa", muted: "#939293", ring: "#78dce8",
    purple: "#ab9df2", amber: "#ffd866", green: "#a9dc76", red: "#ff6188",
    blue: "#78dce8", orange: "#fc9867",
  },
  "dracula": {
    name: "Dracula",
    bg: "#282a36", bg2: "#2e303e", card: "rgba(46,48,62,0.92)",
    cardSolid: "#343746", border: "rgba(255,255,255,0.10)", border2: "rgba(255,255,255,0.18)",
    fg: "#f8f8f2", muted: "#6272a4", ring: "#bd93f9",
    purple: "#bd93f9", amber: "#f1fa8c", green: "#50fa7b", red: "#ff5555",
    blue: "#8be9fd", orange: "#ffb86c",
  },
  "one-dark": {
    name: "One Dark Pro",
    bg: "#282c34", bg2: "#2c313a", card: "rgba(44,49,58,0.92)",
    cardSolid: "#353b45", border: "rgba(255,255,255,0.10)", border2: "rgba(255,255,255,0.18)",
    fg: "#abb2bf", muted: "#5c6370", ring: "#61afef",
    purple: "#c678dd", amber: "#e5c07b", green: "#98c379", red: "#e06c75",
    blue: "#61afef", orange: "#d19a66",
  },
  "nord": {
    name: "Nord",
    bg: "#2e3440", bg2: "#3b4252", card: "rgba(59,66,82,0.92)",
    cardSolid: "#434c5e", border: "rgba(255,255,255,0.10)", border2: "rgba(255,255,255,0.18)",
    fg: "#eceff4", muted: "#616e88", ring: "#88c0d0",
    purple: "#b48ead", amber: "#ebcb8b", green: "#a3be8c", red: "#bf616a",
    blue: "#81a1c1", orange: "#d08770",
  },
  "github-dark": {
    name: "GitHub Dark",
    bg: "#0d1117", bg2: "#161b22", card: "rgba(22,27,34,0.92)",
    cardSolid: "#21262d", border: "rgba(255,255,255,0.10)", border2: "rgba(255,255,255,0.18)",
    fg: "#c9d1d9", muted: "#8b949e", ring: "#58a6ff",
    purple: "#d2a8ff", amber: "#e3b341", green: "#3fb950", red: "#f85149",
    blue: "#58a6ff", orange: "#d29922",
  },
  "tokyo-night": {
    name: "Tokyo Night",
    bg: "#1a1b26", bg2: "#1f2335", card: "rgba(31,35,53,0.92)",
    cardSolid: "#24283b", border: "rgba(255,255,255,0.10)", border2: "rgba(255,255,255,0.18)",
    fg: "#c0caf5", muted: "#565f89", ring: "#7aa2f7",
    purple: "#bb9af7", amber: "#e0af68", green: "#9ece6a", red: "#f7768e",
    blue: "#7aa2f7", orange: "#ff9e64",
  },
  "ocean-dark": {
    name: "Ocean Dark",
    bg: "#0b1021", bg2: "#101729", card: "rgba(16,23,41,0.92)",
    cardSolid: "#1a2340", border: "rgba(148,163,184,0.12)", border2: "rgba(148,163,184,0.20)",
    fg: "#e2e8f0", muted: "#7c8db5", ring: "#38bdf8",
    purple: "#a78bfa", amber: "#fbbf24", green: "#34d399", red: "#f87171",
    blue: "#38bdf8", orange: "#fb923c",
  },
  "midnight-blue": {
    name: "Midnight Blue",
    bg: "#0a0e1a", bg2: "#0f1628", card: "rgba(15,22,40,0.92)",
    cardSolid: "#1a2744", border: "rgba(100,130,200,0.12)", border2: "rgba(100,130,200,0.20)",
    fg: "#d6e4ff", muted: "#7b8db8", ring: "#6c9eff",
    purple: "#b392f0", amber: "#f6c85f", green: "#7ec699", red: "#ff7b72",
    blue: "#6c9eff", orange: "#f0883e",
  },
  "pure-black": {
    name: "OLED Black",
    bg: "#000000", bg2: "#0a0a0a", card: "rgba(10,10,10,0.95)",
    cardSolid: "#141414", border: "rgba(255,255,255,0.08)", border2: "rgba(255,255,255,0.15)",
    fg: "#e4e4e7", muted: "#71717a", ring: "#a78bfa",
    purple: "#a78bfa", amber: "#fbbf24", green: "#4ade80", red: "#fb7185",
    blue: "#60a5fa", orange: "#fb923c",
  },
};

const ACCENT_COLORS = {
  purple:  { label: "Purple",  color: "#a78bfa", ring: "#a78bfa" },
  blue:    { label: "Blue",    color: "#60a5fa", ring: "#60a5fa" },
  green:   { label: "Green",   color: "#4ade80", ring: "#4ade80" },
  red:     { label: "Red",     color: "#fb7185", ring: "#fb7185" },
  amber:   { label: "Amber",   color: "#fbbf24", ring: "#fbbf24" },
  orange:  { label: "Orange",  color: "#fb923c", ring: "#fb923c" },
  cyan:    { label: "Cyan",    color: "#22d3ee", ring: "#22d3ee" },
  pink:    { label: "Pink",    color: "#f472b6", ring: "#f472b6" },
};

let _currentTheme = localStorage.getItem("webex_theme") || "ocean-dark";
let _currentAccent = localStorage.getItem("webex_accent") || "purple";
let _currentFontSize = parseInt(localStorage.getItem("webex_fontsize") || "14");
let _currentRadius = parseInt(localStorage.getItem("webex_radius") || "10");

function applyTheme(themeId) {
  const t = THEMES[themeId];
  if (!t) return;
  _currentTheme = themeId;
  localStorage.setItem("webex_theme", themeId);
  const r = document.documentElement.style;
  r.setProperty("--bg", t.bg);
  r.setProperty("--bg2", t.bg2);
  r.setProperty("--card", t.card);
  r.setProperty("--card-solid", t.cardSolid);
  r.setProperty("--border", t.border);
  r.setProperty("--border2", t.border2);
  r.setProperty("--fg", t.fg);
  r.setProperty("--muted", t.muted);
  r.setProperty("--ring", t.ring);
  r.setProperty("--purple", t.purple);
  r.setProperty("--amber", t.amber);
  r.setProperty("--green", t.green);
  r.setProperty("--red", t.red);
  r.setProperty("--blue", t.blue);
  r.setProperty("--orange", t.orange);
  applyAccent(_currentAccent);
  // Update badge
  const lbl = document.getElementById("currentThemeLabel");
  if (lbl) lbl.textContent = t.name;
  // Highlight active theme card
  document.querySelectorAll(".theme-card").forEach(c => {
    c.classList.toggle("active", c.dataset.theme === themeId);
  });
}

function applyAccent(accentId) {
  const a = ACCENT_COLORS[accentId];
  if (!a) return;
  _currentAccent = accentId;
  localStorage.setItem("webex_accent", accentId);
  document.documentElement.style.setProperty("--accent", a.color);
  document.querySelectorAll(".accent-dot").forEach(d => {
    d.classList.toggle("active", d.dataset.accent === accentId);
  });
}

function applyFontSize(size) {
  _currentFontSize = size;
  localStorage.setItem("webex_fontsize", size);
  document.documentElement.style.fontSize = size + "px";
  const lbl = document.getElementById("fontSizeLabel");
  if (lbl) lbl.textContent = size + "px";
}

function applyRadius(radius) {
  _currentRadius = radius;
  localStorage.setItem("webex_radius", radius);
  document.documentElement.style.setProperty("--radius", radius + "px");
  document.querySelectorAll(".border-opt").forEach(b => {
    b.classList.toggle("active", parseInt(b.dataset.radius) === radius);
  });
}

function initSettingsView() {
  // Render theme grid
  const grid = document.getElementById("themeGrid");
  if (grid) {
    grid.innerHTML = "";
    for (const [id, t] of Object.entries(THEMES)) {
      const card = document.createElement("div");
      card.className = "theme-card" + (id === _currentTheme ? " active" : "");
      card.dataset.theme = id;
      card.innerHTML = `
        <div class="theme-preview" style="background:${t.bg};border:1px solid ${t.border2}">
          <div style="background:${t.bg2};height:6px;border-radius:3px;margin-bottom:4px"></div>
          <div style="display:flex;gap:3px">
            <div style="width:8px;height:8px;border-radius:50%;background:${t.purple}"></div>
            <div style="width:8px;height:8px;border-radius:50%;background:${t.green}"></div>
            <div style="width:8px;height:8px;border-radius:50%;background:${t.blue}"></div>
            <div style="width:8px;height:8px;border-radius:50%;background:${t.amber}"></div>
          </div>
        </div>
        <span class="theme-name">${t.name}</span>`;
      card.addEventListener("click", () => applyTheme(id));
      grid.appendChild(card);
    }
  }

  // Render accent grid
  const accentGrid = document.getElementById("accentGrid");
  if (accentGrid) {
    accentGrid.innerHTML = "";
    for (const [id, a] of Object.entries(ACCENT_COLORS)) {
      const dot = document.createElement("div");
      dot.className = "accent-dot" + (id === _currentAccent ? " active" : "");
      dot.dataset.accent = id;
      dot.style.background = a.color;
      dot.title = a.label;
      dot.addEventListener("click", () => applyAccent(id));
      accentGrid.appendChild(dot);
    }
  }

  // Font size buttons
  document.querySelectorAll("[data-fontsize]").forEach(btn => {
    btn.addEventListener("click", () => applyFontSize(parseInt(btn.dataset.fontsize)));
  });

  // Border radius buttons
  document.querySelectorAll(".border-opt").forEach(btn => {
    btn.addEventListener("click", () => applyRadius(parseInt(btn.dataset.radius)));
  });

  // Reset button
  const resetBtn = document.getElementById("resetThemeBtn");
  if (resetBtn) {
    resetBtn.addEventListener("click", () => {
      localStorage.removeItem("webex_theme");
      localStorage.removeItem("webex_accent");
      localStorage.removeItem("webex_fontsize");
      localStorage.removeItem("webex_radius");
      _currentTheme = "ocean-dark";
      _currentAccent = "purple";
      _currentFontSize = 14;
      _currentRadius = 10;
      applyTheme("ocean-dark");
      applyAccent("purple");
      applyFontSize(14);
      applyRadius(10);
    });
  }

  // Set initial values
  applyFontSize(_currentFontSize);
  applyRadius(_currentRadius);
}

// Apply saved theme on boot (before view loads)
applyTheme(_currentTheme);

// Hook into bindViewHandlers to init settings when settings view loads
const _origBindView = bindViewHandlers;
bindViewHandlers = function() {
  _origBindView();
  if (currentRoute() === "settings") {
    initSettingsView();
  }
};

boot();
