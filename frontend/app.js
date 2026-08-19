"use strict";

const $status = document.getElementById("status");
const $footer = document.getElementById("footer-text");
const $dot = document.querySelector(".dot");
const $box = document.getElementById("widget-box");
const $title = document.getElementById("title");
const $tabTg = document.getElementById("tab-tg");
const $tabOwner = document.getElementById("tab-owner");
const $panelTg = document.getElementById("panel-tg");
const $panelOwner = document.getElementById("panel-owner");
const $devKey = document.getElementById("dev-key");
const $devLogin = document.getElementById("dev-login");

function setStatus(text, kind) {
  $status.textContent = text;
  $status.className = "status" + (kind ? " " + kind : "");
}

function setFooter(text, on) {
  $footer.textContent = text;
  $dot.classList.toggle("on", !!on);
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
    ...opts,
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error || "Request failed");
  return body;
}

function openSession(data) {
  localStorage.setItem("webex_token", data.token);
  setStatus("Access granted", "ok");
  setFooter("Redirecting", true);
  setTimeout(() => location.replace("/dashboard"), 500);
}

function switchTab(active) {
  const tg = active === "tg";
  $tabTg.classList.toggle("active", tg);
  $tabOwner.classList.toggle("active", !tg);
  $tabTg.setAttribute("aria-selected", tg);
  $tabOwner.setAttribute("aria-selected", !tg);
  $panelTg.hidden = !tg;
  $panelOwner.hidden = tg;
}

$tabTg.addEventListener("click", () => switchTab("tg"));
$tabOwner.addEventListener("click", () => switchTab("owner"));

async function onTelegramAuth(user) {
  setStatus("Verifying…", "busy");
  setFooter("Auth check", true);
  try {
    openSession(await api("/api/login", { method: "POST", body: JSON.stringify(user) }));
  } catch (err) {
    setStatus(err.message, "err");
    setFooter("Auth failed", false);
  }
}

window.onTelegramAuth = onTelegramAuth;

async function onDevLogin() {
  const devKey = $devKey.value.trim();
  if (!devKey) {
    setStatus("Enter dev key", "err");
    return;
  }
  setStatus("Verifying…", "busy");
  try {
    openSession(await api("/api/dev-login", { method: "POST", body: JSON.stringify({ dev_key: devKey }) }));
  } catch (err) {
    setStatus(err.message, "err");
    setFooter("Auth failed", false);
  }
}

$devLogin.addEventListener("click", onDevLogin);
$devKey.addEventListener("keydown", (e) => {
  if (e.key === "Enter") onDevLogin();
});

function mountWidget(botUsername) {
  const s = document.createElement("script");
  s.async = true;
  s.src = "https://telegram.org/js/telegram-widget.js?22";
  s.setAttribute("data-telegram-login", botUsername);
  s.setAttribute("data-size", "large");
  s.setAttribute("data-radius", "12");
  s.setAttribute("data-userpic", "true");
  s.setAttribute("data-onauth", "onTelegramAuth(user)");
  s.setAttribute("data-request-access", "write");
  $box.innerHTML = "";
  $box.appendChild(s);
}

async function boot() {
  try {
    const cfg = await api("/api/config");
    if (cfg.bot_name) $title.textContent = cfg.bot_name;
    if (!cfg.bot_username) {
      setStatus("Bot username not set in config.json", "err");
      setFooter("Config missing", false);
      return;
    }
    if (cfg.owner_tab) $tabOwner.hidden = false;
    mountWidget(cfg.bot_username);
    setFooter("Connected", true);
  } catch (err) {
    setStatus(err.message, "err");
    setFooter("Server offline", false);
  }
}

boot();
