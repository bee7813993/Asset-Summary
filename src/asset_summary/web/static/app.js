"use strict";

// ============================================================
// Asset Summary — フロントエンド（vanilla JS・ビルドレス）
// Crypto-Summary のフロント構造を踏襲（ハッシュルーター・fetchJSON・
// fmtMoney/fmtJpy・マスクモード・テーマ切替・i18n・Chart.js 4.4.1）。
// localStorage キーは as_* を使用。
// ============================================================

// ---- テーマ初期化 ----
(function initPrefs() {
  if (localStorage.getItem("as_theme") === "light") {
    document.documentElement.classList.add("light");
  }
})();

const CURRENCY_SYMBOL = { USD: "$", JPY: "¥", EUR: "€", GBP: "£" };
const FALLBACK_PALETTE = [
  "#2f81f7", "#3fb950", "#39c5cf", "#a371f7", "#8957e5",
  "#6cb6ff", "#e3b341", "#f0883e", "#db61a2", "#8b949e", "#6e7681",
];

const DASH_TOP_ACCOUNTS = 5;
const DASH_TOP_HOLDINGS = 8;

let maskAmounts = localStorage.getItem("as_mask") === "1";

// /api/meta の内容（asset_classes の色・ラベルなど）
let META = null;
let CLASS_META = {}; // id -> {label_ja, label_en, color}

// チャートインスタンス
let allocChart = null;
let _histChart = null;
let _classHistChart = null;
let _acctHistChart = null;
let _pfHistChart = null;
let _priceChart = null;

// レンジタブ状態（localStorage 記憶）
let _dashRange = localStorage.getItem("as_dash_range") || "90d";
let _classRange = localStorage.getItem("as_class_range") || "90d";
let _acctRange = localStorage.getItem("as_acct_range") || "90d";
let _pfRange = localStorage.getItem("as_pf_range") || "90d";
let _secRange = localStorage.getItem("as_sec_range") || "1y";

// 詳細ページの現在対象
let _classDetailId = null;
let _acctDetailName = null;
let _secDetailId = null;

// Crypto-Summary 連携（コイン別サブビュー）
let _csAssetSym = null;
let _csAssetRange = localStorage.getItem("as_cs_range") || "90d";
let _csAssetChart = null;
let _csCoinIcons = null;    // {SYM: url} — 起動時に一度だけ取得

// キャッシュ
let _lastSummary = null;      // /api/summary の最新結果
let _securities = [];         // /api/securities（管理・設定ページ用）
let _reIndexOptions = null;   // /api/re-index/options（地域・種別の語彙と最新月）
let _accounts = [];           // /api/accounts
let _accountIdByName = {};    // 表示名 -> id

// 保有一覧のフィルタ状態
let _holdingsFilter = "";
// 保有テーブルのソート状態（既定は評価額の大きい順＝APIの並び）。
// 保有一覧・クラス詳細・口座詳細・構成銘柄で同じ操作感にするため、
// テーブルごとに同じ形の状態を持たせて共通の関数で扱う。
function _loadSortState(storageKey) {
  try {
    const saved = JSON.parse(localStorage.getItem(storageKey) || "null");
    if (saved && typeof saved.key === "string") return { ...saved, storageKey };
  } catch (_) { /* 壊れていたら既定に戻す */ }
  return { key: "value", dir: "desc", storageKey };
}
let _holdingsSort = _loadSortState("as_holdings_sort");
let _classHoldingsSort = _loadSortState("as_class_holdings_sort");
let _acctHoldingsSort = _loadSortState("as_acct_holdings_sort");
let _pfSort = _loadSortState("as_pf_holdings_sort");
// 取得済みの詳細データ（ソート切替で再取得しないよう保持する）
let _classHoldings = null;
let _acctHoldings = null;
let _pfDetailOpts = null;
// 推移グラフのスコープ（"tag:3" / "portfolio:1"）
let _pfHistScope = null;
let _holdingsSearch = "";

// 取込プレビュー状態
let _importPreview = null;

// 投信 自動連携（設定ページ）の判定結果
// /api/fund-links/suggest の suggestions に UI 状態（_checked, _selRef）を持たせて保持
let _autolinkSuggestions = null;

// ---- ユーティリティ ----

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function fmtMoney(value, currency) {
  if (value === null || value === undefined || value === "") return "—";
  if (maskAmounts) return (CURRENCY_SYMBOL[currency] || "") + "●●●●●";
  const sym = CURRENCY_SYMBOL[currency] || "";
  const n = Number(value);
  if (!isFinite(n)) return "—";
  const digits = currency === "JPY" ? 0 : 2;
  return sym + n.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

// 単価用: JPYでも小数を保持（基準価額・平均取得単価など）
function fmtPrice(value, currency) {
  if (value === null || value === undefined || value === "") return "—";
  if (maskAmounts) return (CURRENCY_SYMBOL[currency] || "") + "●●●●●";
  const n = Number(value);
  if (!isFinite(n)) return "—";
  const maxDigits = currency === "JPY" ? 2 : 4;
  return (CURRENCY_SYMBOL[currency] || "") +
    n.toLocaleString(undefined, { maximumFractionDigits: maxDigits });
}

// JPY 専用: 億・万・円 のサブ表示
function fmtJpy(value) {
  if (maskAmounts) return "¥●●●●●";
  const n = Math.round(Number(value));
  if (!isFinite(n)) return "";
  if (n === 0) return "0円";
  const neg = n < 0;
  const abs = Math.abs(n);
  const oku = Math.floor(abs / 100_000_000);
  const manPart = abs % 100_000_000;
  const man = Math.floor(manPart / 10_000);
  const yen = manPart % 10_000;
  let str = "";
  if (oku > 0) str += oku + "億";
  if (man > 0) str += man + "万";
  if (yen > 0 || str === "") str += yen + "円";
  else str += "円";
  return (neg ? "−" : "") + str;
}

// 軸ラベル用の短縮表記。スマホの狭い描画域で「¥74,500,000」級のラベルが
// プロット幅を食い潰すのを防ぐ（JPYは億/万、他通貨はcompact表記）。
function fmtMoneyShort(value, currency) {
  const n = Number(value);
  if (maskAmounts || value === null || value === undefined || value === "" || !isFinite(n)) {
    return fmtMoney(value, currency);
  }
  const sign = n < 0 ? "−" : "";
  const abs = Math.abs(n);
  if (currency === "JPY") {
    if (abs >= 100_000_000) {
      const oku = abs / 100_000_000;
      return sign + "¥" + (oku >= 10 ? Math.round(oku).toLocaleString() : oku.toFixed(1)) + "億";
    }
    if (abs >= 10_000) return sign + "¥" + Math.round(abs / 10_000).toLocaleString() + "万";
    return fmtMoney(value, currency);
  }
  if (abs >= 10_000) {
    return sign + (CURRENCY_SYMBOL[currency] || "") +
      abs.toLocaleString(undefined, { notation: "compact", maximumFractionDigits: 1 });
  }
  return fmtMoney(value, currency);
}

function fmtAmount(value) {
  if (value === null || value === undefined || value === "") return "—";
  if (maskAmounts) return "●●●●●";
  const n = Number(value);
  if (!isFinite(n)) return "—";
  return n.toLocaleString(undefined, { maximumFractionDigits: 4 });
}

// 損益額: 符号付き。JPYは「+123,456円」形式、他通貨は「+$1,234.56」。
function fmtSignedMoney(value, currency) {
  if (value === null || value === undefined || value === "") return "—";
  const n = Number(value);
  if (!isFinite(n)) return "—";
  if (maskAmounts) return "●●●●●";
  const sign = n > 0 ? "+" : n < 0 ? "−" : "";
  const abs = Math.abs(n);
  if (currency === "JPY") {
    return sign + abs.toLocaleString(undefined, { maximumFractionDigits: 0 }) + "円";
  }
  return sign + (CURRENCY_SYMBOL[currency] || "") +
    abs.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

// 損益率: %は小数2桁文字列（マスク時も表示可）
function fmtSignedPct(pct) {
  if (pct === null || pct === undefined || pct === "") return "—";
  const n = Number(pct);
  if (!isFinite(n)) return "—";
  const sign = n > 0 ? "+" : n < 0 ? "−" : "";
  return sign + Math.abs(n).toFixed(2) + "%";
}

function plClass(value) {
  const n = Number(value);
  if (!isFinite(n) || n === 0) return "";
  return n > 0 ? "pl-pos" : "pl-neg";
}

function plAmountHtml(value, currency) {
  if (value === null || value === undefined || value === "") return '<span class="muted">—</span>';
  return `<span class="${plClass(value)}">${fmtSignedMoney(value, currency)}</span>`;
}

function plPctHtml(pct) {
  if (pct === null || pct === undefined || pct === "") return '<span class="muted">—</span>';
  return `<span class="${plClass(pct)}">${fmtSignedPct(pct)}</span>`;
}

// カード表示用: 現在値と平均取得単価を1項目にまとめた文字列。
// 「¥27,771（¥18,344）」形式。どちらも無い銘柄（現金・年金など）は「—」だけ
// 出し、カードの行構成が銘柄によって変わらないようにする。
function priceWithCostText(h, currency) {
  if (h.price == null && h.avg_cost == null) return "—";
  const cur = h.currency || currency;
  return `${fmtPrice(h.price, cur)}（${fmtPrice(h.avg_cost, cur)}）`;
}

// 評価額セル（保有テーブル用）。金額は途中で折り返さないが、後続のバッジ
// （目安・参考値）は狭ければ次の行へ落とせるよう、行全体は折り返し可能にする。
function valueWithPlCellHtml(h, currency, badgesHtml) {
  return `<span class="cell-stack"><span class="value-line">` +
    `<span class="value-num">${fmtMoney(h.value, currency)}</span>${badgesHtml || ""}</span></span>`;
}

// 評価損益セル: スマホでは損益率を2行目（.pl-pct-sub）に出す（前日比セルと
// 同じ2行スタイル）。広い画面では損益率は独立した列で出すのでサブ行は隠れる。
function plCellHtml(h, currency) {
  if (h.pl === null || h.pl === undefined || h.pl === "") {
    return '<span class="muted">—</span>';
  }
  const sub = h.pl_pct != null
    ? `<span class="cell-sub pl-pct-sub">${fmtSignedPct(h.pl_pct)}</span>` : "";
  return `<span class="cell-stack ${plClass(h.pl)}">` +
    `<span>${fmtSignedMoney(h.pl, currency)}</span>${sub}</span>`;
}

// 前日比セル: 金額（上）と%（下）の2行。Yahooファイナンスの「前日差」と同じ見せ方。
// 前日終値の基準日は title で補う（投信はT+1公表なので基準日が1日ずれる）。
function dayChangeCellHtml(row, currency) {
  const amount = row && row.day_change;
  if (amount === null || amount === undefined || amount === "") {
    return '<span class="muted">—</span>';
  }
  const asOf = row.day_change_as_of;
  const title = asOf ? ` title="${escapeHtml(t("label.dayChangeAsOf", { date: asOf }))}"` : "";
  return `<span class="cell-stack ${plClass(amount)}"${title}>` +
    `<span>${fmtSignedMoney(amount, currency)}</span>` +
    `<span class="cell-sub">${fmtSignedPct(row.day_change_pct)}</span></span>`;
}

// 評価額セル: スマホでは構成比を2行目（.weight-sub）に出す。広い画面では
// 構成比は独立した列（.weight-col）で出すので、サブ行は CSS で隠される。
// タグ別・資産クラス・口座別で同じ見せ方に揃えるための共通セル。
function valueWeightCellHtml(value, currency, weightHtml) {
  const sub = weightHtml
    ? `<span class="cell-sub weight-sub">${weightHtml}</span>` : "";
  return `<span class="cell-stack"><span>${fmtMoney(value, currency)}</span>${sub}</span>`;
}

// 金額（上）と割合（下）の2行セル。計上額（計上率）などに使う。
function amountRatioCellHtml(amount, pct, currency) {
  if (amount === null || amount === undefined || amount === "") {
    return '<span class="muted">—</span>';
  }
  const sub = pct === null || pct === undefined || pct === ""
    ? "" : `<span class="cell-sub">${Number(pct)}%</span>`;
  return `<span class="cell-stack"><span>${fmtMoney(amount, currency)}</span>${sub}</span>`;
}

/**
 * 保有行の合計。API の _totals_block と同じ規約で集計する
 * （損益は原価の判る行だけ、前日比は前日値の判る行だけ・欠けたら partial）。
 */
function aggregateRows(rows) {
  let value = 0, cost = 0, pl = 0, hasPl = false;
  let dayChange = 0, prevValue = 0, hasDay = false, partial = false;
  (rows || []).forEach((r) => {
    if (r.value != null) value += Number(r.value);
    if (r.cost != null && r.costed_value != null) {
      cost += Number(r.cost);
      pl += Number(r.pl || 0);
      hasPl = true;
    }
    if (r.day_change != null) {
      dayChange += Number(r.day_change);
      prevValue += Number(r.prev_value || 0);
      hasDay = true;
    } else if (r.value != null) {
      partial = true;
    }
  });
  return {
    total_value: value,
    total_cost: hasPl ? cost : null,
    total_pl: hasPl ? pl : null,
    total_pl_pct: hasPl && cost ? (pl / cost) * 100 : null,
    total_day_change: hasDay ? dayChange : null,
    total_day_change_pct: hasDay && prevValue ? (dayChange / prevValue) * 100 : null,
    // 「一部を除く」は数字を出しているときだけ意味がある
    day_change_partial: hasDay && partial,
  };
}

function fmtDate(iso) {
  if (!iso) return "-";
  return new Date(iso).toLocaleString("ja-JP", {
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit",
  });
}

// ---- クラスメタ ----

function classLabel(classId) {
  const m = CLASS_META[classId];
  if (!m) return classId || "";
  return _lang === "en" ? (m.label_en || m.label_ja || classId) : (m.label_ja || classId);
}

function classColor(classId) {
  const m = CLASS_META[classId];
  if (m && m.color) return m.color;
  const ids = Object.keys(CLASS_META);
  const i = ids.indexOf(classId);
  return FALLBACK_PALETTE[(i >= 0 ? i : 0) % FALLBACK_PALETTE.length];
}

function classBadgeHtml(classId) {
  const color = classColor(classId);
  return `<span class="class-badge" style="background:${color}22;color:${color};border:1px solid ${color}55">${escapeHtml(classLabel(classId))}</span>`;
}

// ---- Crypto-Summary 連携ヘルパー ----

function csMeta() {
  return (META && META.crypto_summary) || { enabled: false, url: null };
}

function csBadgeHtml() {
  // スマホでは短い表記（cs.badgeShort）に CSS で切り替える。長い表記は折り返せず
  // 銘柄列の最小幅の床になり、表が画面幅を超えるため（列幅の計算はバッジの
  // 縮小・省略を考慮しない）
  return `<span class="ref-badge cs-badge" title="${escapeHtml(t("cs.badgeTitle"))}">` +
    `<span class="cs-badge-full">${escapeHtml(t("cs.badge"))}</span>` +
    `<span class="cs-badge-short">${escapeHtml(t("cs.badgeShort"))}</span></span>`;
}

async function _ensureCsCoinIcons() {
  if (_csCoinIcons !== null || !csMeta().enabled) return;
  try {
    _csCoinIcons = await fetchJSON("/api/crypto-summary/coin-icons");
  } catch (_) {
    _csCoinIcons = {};
  }
}

function csCoinIconHtml(sym) {
  const url = _csCoinIcons && _csCoinIcons[String(sym || "").toUpperCase()];
  if (!url) return "";
  return `<img class="coin-icon" src="${escapeHtml(url)}" alt="" loading="lazy">`;
}

// ---- API ヘルパー ----

/** アプリの公開位置を基準に URL を解決する（サブパス配信への対応）。
 *
 * サーバーが index.html に <base href="/asset/"> を差し込むので、先頭の / を
 * 落として document.baseURI 基準で解決すれば、ルート直下でもサブパスでも
 * 同じコードで正しい URL になる。base はパスだけなので、スキームとホストは
 * 今開いているページのものが使われる（https のページから http を読みに
 * いってしまう事故が起きない）。
 */
function apiUrl(path) {
  return new URL(String(path).replace(/^\/+/, ""), document.baseURI).toString();
}

async function fetchJSON(url) {
  const res = await fetch(apiUrl(url));
  if (!res.ok) {
    if (res.status === 401) _showLoginScreen();  // セッション切れ → ログイン画面
    let detail = null;
    try { detail = (await res.json()).detail; } catch (_) { /* ignore */ }
    throw new Error(detail || `HTTP ${res.status}`);
  }
  return res.json();
}

async function apiCall(url, method, body) {
  const opts = { method, headers: {} };
  if (body !== undefined && body !== null) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(apiUrl(url), opts);
  let data = null;
  try { data = await res.json(); } catch (_) { /* no body */ }
  if (!res.ok) {
    const err = new Error((data && data.detail) || `HTTP ${res.status}`);
    err.status = res.status;
    err.data = data;
    throw err;
  }
  return data;
}

function currentCurrency() {
  return document.getElementById("currency").value;
}

function todayISO() {
  const d = new Date();
  const p = (x) => String(x).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

function showResult(id, ok, message) {
  const el = document.getElementById(id);
  if (!el) return;
  el.className = "settings-result " + (ok ? "ok" : "err");
  el.textContent = message;
  el.classList.remove("hidden");
}

function hideResult(id) {
  const el = document.getElementById(id);
  if (el) el.classList.add("hidden");
}

/**
 * モバイルで折りたたまれた列の詳細を展開するトグルセルを行末に追加する。
 * details: [{label: string, value: string (HTML可)}]
 */
function _appendDetailToggle(tr, details) {
  const td = document.createElement("td");
  td.className = "detail-toggle-cell";
  const btn = document.createElement("button");
  btn.className = "detail-toggle-btn";
  btn.textContent = "▶";
  btn.setAttribute("aria-expanded", "false");
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    const expanded = btn.getAttribute("aria-expanded") === "true";
    btn.setAttribute("aria-expanded", String(!expanded));
    btn.textContent = expanded ? "▶" : "▼";
    const next = tr.nextElementSibling;
    if (next && next.classList.contains("detail-row")) {
      next.remove();
    } else {
      const dtr = document.createElement("tr");
      dtr.className = "detail-row";
      const dtd = document.createElement("td");
      dtd.colSpan = 99;
      dtd.innerHTML = `<dl>${details.map((d) =>
        `<dt>${d.label}</dt><dd>${d.value}</dd>`
      ).join("")}</dl>`;
      dtr.appendChild(dtd);
      tr.after(dtr);
    }
  });
  td.appendChild(btn);
  tr.appendChild(td);
}

function chartTheme() {
  const s = getComputedStyle(document.documentElement);
  const g = (v) => s.getPropertyValue(v).trim();
  return {
    tick: g("--text-dim"),
    grid: g("--border"),
    tooltipBg: g("--bg-elev"),
    tooltipBorder: g("--border"),
    tooltipTitle: g("--text"),
    tooltipBody: g("--text-dim"),
  };
}

// ---- テーマ・マスク・言語切替 ----

function _syncThemeBtn() {
  const btn = document.getElementById("theme-toggle");
  if (!btn) return;
  const isLight = document.documentElement.classList.contains("light");
  btn.textContent = isLight ? "🌙" : "☀";
  btn.title = isLight ? t("toggle.toDark") : t("toggle.toLight");
}

document.getElementById("theme-toggle").addEventListener("click", () => {
  const isLight = document.documentElement.classList.toggle("light");
  localStorage.setItem("as_theme", isLight ? "light" : "dark");
  _syncThemeBtn();
  router();
});

function _syncMaskBtn() {
  const btn = document.getElementById("mask-toggle");
  if (!btn) return;
  btn.textContent = maskAmounts ? "🔒" : "👁";
  btn.title = maskAmounts ? t("toggle.maskOff") : t("toggle.maskOn");
}

document.getElementById("mask-toggle").addEventListener("click", () => {
  maskAmounts = !maskAmounts;
  localStorage.setItem("as_mask", maskAmounts ? "1" : "0");
  _syncMaskBtn();
  router();
});

function _syncLangBtn() {
  const btn = document.getElementById("lang-toggle");
  if (!btn) return;
  btn.textContent = t("toggle.langBtn");
  btn.title = t("toggle.langTitle");
}

const _CURRENCY_LABELS = {
  ja: { USD: "USD　米ドル", JPY: "JPY　日本円", EUR: "EUR　ユーロ", GBP: "GBP　英ポンド" },
  en: { USD: "USD  US Dollar", JPY: "JPY  Japanese Yen", EUR: "EUR  Euro", GBP: "GBP  Pound Sterling" },
};
function _syncCurrencyLabels() {
  const labels = _CURRENCY_LABELS[_lang] || _CURRENCY_LABELS.ja;
  ["currency", "set-default-currency"].forEach((id) => {
    const sel = document.getElementById(id);
    if (!sel) return;
    [...sel.options].forEach((opt) => {
      opt.textContent = labels[opt.value] || opt.value;
    });
  });
}

document.getElementById("lang-toggle").addEventListener("click", () => {
  setLang(_lang === "ja" ? "en" : "ja");
  _syncThemeBtn();
  _syncMaskBtn();
  _syncLangBtn();
  _syncCurrencyLabels();
  router();
});

// ---- ハッシュルーター ----
//   #dashboard
//   #classes / #classes/detail?name=<class_id>
//   #accounts / #accounts/detail?name=<表示名>
//   #holdings / #holdings/detail?id=<security_id>
//   #import / #manage / #settings

const PAGES = ["dashboard", "classes", "accounts", "holdings", "portfolios", "import", "manage", "settings"];

function _encodeParams(obj) {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(obj || {})) {
    if (v != null && v !== "") p.set(k, v);
  }
  const s = p.toString();
  return s ? "?" + s : "";
}

function buildHash(page, sub, params) {
  let h = page;
  if (sub) h += "/" + sub;
  h += _encodeParams(params);
  return h;
}

function parseHash() {
  const raw = location.hash.replace(/^#/, "");
  if (!raw) return { page: "dashboard", sub: null, params: {} };
  const qIdx = raw.indexOf("?");
  const path = qIdx >= 0 ? raw.slice(0, qIdx) : raw;
  const query = qIdx >= 0 ? raw.slice(qIdx + 1) : "";
  const [page, sub] = path.split("/");
  const params = {};
  new URLSearchParams(query).forEach((v, k) => { params[k] = v; });
  return { page: PAGES.includes(page) ? page : "dashboard", sub: sub || null, params };
}

function router() {
  const { page, sub, params } = parseHash();
  activatePage(page);

  if (page === "dashboard") {
    loadDashboard();
  } else if (page === "classes") {
    if (sub === "crypto-asset" && params.sym) {
      showCsAssetDetail(params.sym);
    } else if (sub === "detail" && params.name) {
      showClassDetail(params.name);
    } else {
      showClassesList();
      loadClassesPage();
    }
  } else if (page === "accounts") {
    if (sub === "detail" && params.name) {
      showAccountDetail(params.name);
    } else {
      showAccountsList();
      loadAccountsPage();
    }
  } else if (page === "holdings") {
    if (sub === "detail" && params.id) {
      showSecurityDetail(params.id);
    } else {
      showHoldingsList();
      loadHoldingsPage();
    }
  } else if (page === "portfolios") {
    if (sub === "tag" && params.id) {
      showTagDetail(params.id);
    } else if (sub === "detail" && params.id) {
      showPortfolioDetail(params.id);
    } else {
      showPortfoliosList();
      loadPortfoliosPage();
    }
  } else if (page === "import") {
    loadImportPage();
  } else if (page === "manage") {
    loadManagePage();
  } else if (page === "settings") {
    loadSettingsPage();
  }
}

function navigate(page, sub = null, params = null) {
  history.pushState(null, "", "#" + buildHash(page, sub, params));
  router();
}

document.querySelectorAll(".nav-link[data-page]").forEach((a) => {
  a.addEventListener("click", (e) => {
    e.preventDefault();
    navigate(a.dataset.page);
  });
});

window.addEventListener("popstate", router);

function activatePage(name) {
  PAGES.forEach((p) => {
    const el = document.getElementById(`page-${p}`);
    if (el) el.classList.toggle("hidden", p !== name);
  });
  document.querySelectorAll(".nav-link[data-page]").forEach((a) => {
    a.classList.toggle("active", a.dataset.page === name);
  });
}

function getCurrentPage() {
  const active = document.querySelector(".nav-link.active[data-page]");
  return active ? active.dataset.page : "dashboard";
}

// data-nav ボタン（ダッシュボードのフッターリンク・空状態ガイド等）
document.querySelectorAll("[data-nav]").forEach((el) =>
  el.addEventListener("click", () => navigate(el.dataset.nav)));

// 本文中に動的生成されるハッシュリンク（警告帯・価格ソース未設定リンク等）も
// ルーター経由で遷移させる（pushState ルーターと挙動を統一）。
document.addEventListener("click", (e) => {
  const a = e.target.closest ? e.target.closest('a[href^="#"]') : null;
  if (!a || a.classList.contains("nav-link")) return;
  e.preventDefault();
  history.pushState(null, "", a.getAttribute("href"));
  router();
});

// ---- 共通描画: 警告帯 ----

function renderWarningsInto(el, warnings, extraHtml) {
  if (!el) return;
  const parts = [];
  (warnings || []).forEach((w) => parts.push("⚠ " + escapeHtml(w)));
  if (extraHtml) parts.push(extraHtml);
  if (parts.length === 0) {
    el.classList.add("hidden");
    el.innerHTML = "";
    return;
  }
  el.innerHTML = parts.join("<br>");
  el.classList.remove("hidden");
}

/**
 * 詳細画面のヒーロー（評価額・円サブ表示・評価損益・前日比）。
 * クラス・口座・タグ・Myポートフォリオのどの詳細でも同じ見た目にするための共通描画。
 * data は _totals_block / タグ集計の封筒（total_value / total_pl / total_day_change …）。
 */
function renderDetailHero(prefix, data, currency) {
  const totalEl = document.getElementById(`${prefix}-total`);
  if (!totalEl) return;
  totalEl.textContent = fmtMoney(data && data.total_value, currency);
  const jpyEl = document.getElementById(`${prefix}-jpy`);
  if (jpyEl) {
    jpyEl.textContent =
      currency === "JPY" && data && data.total_value != null ? fmtJpy(data.total_value) : "";
  }
  const plEl = document.getElementById(`${prefix}-pl`);
  if (plEl) {
    const pl = data && data.total_pl;
    plEl.className = "hero-pl";
    plEl.innerHTML = pl == null ? "" :
      `<span class="${plClass(pl)}">${fmtSignedMoney(pl, currency)}` +
      (data.total_pl_pct != null ? ` (${fmtSignedPct(data.total_pl_pct)})` : "") + "</span>";
  }
  renderHeroDayChange(`${prefix}-day`, data, currency, `${prefix}-note`);
}

/** 詳細ヒーローを空にする（読み込み中に前の対象の数字を見せないため）。 */
function clearDetailHero(prefix) {
  renderDetailHero(prefix, null, currentCurrency());
}

/**
 * ヒーローの前日比行。値が無ければ空にする（「—」は出さない）。
 * partial のときは「一部を除く」注記を noteId に出す。
 */
function renderHeroDayChange(elId, data, currency, noteId) {
  const el = document.getElementById(elId);
  if (!el) return;
  const amount = data && data.total_day_change;
  if (amount === null || amount === undefined || amount === "") {
    el.innerHTML = "";
  } else {
    el.innerHTML =
      `<span class="hero-day-label">${escapeHtml(t("th.dayChange"))}</span>` +
      `<span class="${plClass(amount)}">${fmtSignedMoney(amount, currency)}` +
      (data.total_day_change_pct != null
        ? ` (${fmtSignedPct(data.total_day_change_pct)})` : "") + `</span>`;
  }
  const note = noteId && document.getElementById(noteId);
  if (note) {
    const show = !!(data && data.day_change_partial);
    note.textContent = show ? t("label.dayChangePartial") : "";
    note.classList.toggle("hidden", !show);
  }
}

// ---- 共通描画: 保有テーブル ----
// holdings: /api/summary の holdings 形状（id, account, name, code, asset_class,
// quantity, avg_cost, price, value, day_change, pl, pl_pct, has_price, in_total, ...）

// 銘柄・口座・数量・平均取得単価・現在値・評価額・前日比・評価損益・損益率。
// 空表示の colspan 計算に使う（末尾の展開トグル列と extraCols は別に足す）。
const HOLDINGS_BASE_COLS = 9;

function renderHoldingsRows(tbody, holdings, currency, opts = {}) {
  // extraCols: [{render(h) -> HTML, detailLabel}] を末尾（展開トグルの手前）に足す。
  // タグ・Myポートフォリオの「計上額」のような、その画面固有の列を差し込む口。
  const extraCols = opts.extraCols || [];
  tbody.innerHTML = "";
  if (!holdings || holdings.length === 0) {
    const cols = HOLDINGS_BASE_COLS + extraCols.length + 1;
    tbody.innerHTML = `<tr><td colspan="${cols}" class="muted">${t("label.noHoldings")}</td></tr>`;
    return;
  }
  holdings.forEach((h) => {
    const isCS = h.origin === "crypto_summary";
    const tr = document.createElement("tr");
    tr.className = "clickable" + (h.in_total === false ? " row-dim" : "");
    const refBadge = h.has_price === false && h.value != null
      ? `<span class="ref-badge">${t("label.reference")}</span>` : "";
    // CS 行は name=code(シンボル) なのでコード列は出さずバッジで出所を示す
    const codeHtml = h.code && !isCS
      ? `<span class="asset-code">${escapeHtml(h.code)}</span>` : "";
    const excludedBadge = h.in_total === false
      ? `<span class="ref-badge" title="${escapeHtml(t("label.excluded"))}">${t("label.excludedBadge")}</span>` : "";
    // 参考値(has_price=false)とは別の主張: 価格はあるが公的指数で延長した模型値
    const estBadge = h.estimated
      ? `<span class="ref-badge" title="${escapeHtml(t("label.estimatedTitle"))}">${t("label.estimated")}</span>` : "";
    // 銘柄単位に合算した行は口座が「A 他N件」表示になる。内訳は title で補う
    const accts = Array.isArray(h.accounts) ? h.accounts : [];
    const acctTitle = accts.length > 1
      ? accts.map((a) => `${a.account}: ${fmtAmount(a.quantity)}`).join("\n")
      : "";
    const acctAttr = acctTitle ? ` title="${escapeHtml(acctTitle)}"` : "";
    // スマホ縦持ち用の数量サブ表示（CSSで切替。横持ち・PCは数量の列で出す）。
    // 現金・不動産など数量が常に1の行では出さない（ノイズになるだけのため）
    const qtySub = h.quantity != null && Number(h.quantity) !== 1
      ? `<span class="qty-sub">${escapeHtml(t("th.quantity"))} ${fmtAmount(h.quantity)}</span>`
      : "";
    // 価格変動しない資産（現金・ポイント・年金 = 価格取得が不要）は、カードでは
    // 現在値〜評価損益の行を出さない（全部「—」の行が並ぶだけのため）。
    // price_source_status を持たない行（Crypto-Summary等）は値の有無で判定する
    const isStatic = h.price_source_status === "not_required" ||
      (h.price == null && h.avg_cost == null && h.pl == null && h.day_change == null);
    // data-label はスマホ縦持ちのカード型表示でセルの見出しになる（CSSの
    // ::before content: attr(data-label) で描画。表として出る幅では未使用。
    // 付いていないセルはカードに出ない）
    const qtyLabel = h.quantity != null && !(isStatic && Number(h.quantity) === 1)
      ? ` data-label="${escapeHtml(t("th.quantity"))}"` : "";
    const fluctLabel = (key) => (isStatic ? "" : ` data-label="${escapeHtml(key)}"`);
    tr.innerHTML = `
      <td>
        <span class="asset-name">
          ${classBadgeHtml(h.asset_class)}
          ${isCS ? csCoinIconHtml(h.code) : ""}
          <span class="asset-label">${escapeHtml(h.name)}</span>
          ${codeHtml}${isCS ? csBadgeHtml() : ""}${excludedBadge}
        </span>${qtySub}
      </td>
      <td${acctAttr}>${escapeHtml(h.account || "")}</td>
      <td class="num"${qtyLabel}>${fmtAmount(h.quantity)}</td>
      <td class="num">${fmtPrice(h.avg_cost, h.currency || currency)}</td>
      <td class="num"${fluctLabel(`${t("th.currentPrice")}（${t("th.avgCost")}）`)}>
        <span class="price-plain">${fmtPrice(h.price, h.currency || currency)}</span>
        <span class="price-merged">${priceWithCostText(h, currency)}</span>
      </td>
      <td class="num" data-label="${escapeHtml(t("th.value"))}">${valueWithPlCellHtml(h, currency, refBadge + estBadge)}</td>
      <td class="num"${fluctLabel(t("th.dayChange"))}>${dayChangeCellHtml(h, currency)}</td>
      <td class="num"${fluctLabel(t("th.pl"))}>${plCellHtml(h, currency)}</td>
      <td class="num">${plPctHtml(h.pl_pct)}</td>
      ${extraCols.map((c) => `<td class="${c.cellClass || "num"}" data-label="${escapeHtml(c.detailLabel || "")}">${c.render(h)}</td>`).join("")}
    `;
    tr.addEventListener("click", () => {
      if (isCS) navigate("classes", "crypto-asset", { sym: h.code });
      else navigate("holdings", "detail", { id: h.id });
    });
    _appendDetailToggle(tr, [
      { label: t("th.account"), value: escapeHtml(h.account || "") },
      { label: t("th.quantity"), value: fmtAmount(h.quantity) },
      { label: t("th.avgCost"), value: fmtPrice(h.avg_cost, h.currency || currency) },
      { label: t("th.currentPrice"), value: fmtPrice(h.price, h.currency || currency) },
      { label: t("th.dayChange"), value: dayChangeCellHtml(h, currency) },
      ...extraCols.map((c) => ({ label: c.detailLabel, value: c.render(h) })),
    ]);
    tbody.appendChild(tr);
  });
}

// ---- 推移グラフ（グラデーション折れ線） ----

function renderHistoryChart(canvasId, points, currency, existingChart) {
  const canvas = document.getElementById(canvasId);
  if (!canvas) return null;
  if (existingChart) existingChart.destroy();

  const emptyEl = canvas.parentElement.querySelector(".history-empty");

  if (!points || points.length < 2) {
    canvas.style.display = "none";
    if (emptyEl) emptyEl.classList.remove("hidden");
    return null;
  }
  canvas.style.display = "";
  if (emptyEl) emptyEl.classList.add("hidden");

  const labels = points.map((p) => p.t);
  const values = points.map((p) => Number(p.value));

  // 狭い描画域（スマホ縦持ちなど）では日付を "MM-DD" に短縮し、本数も減らす。
  // フル表記のままだと "2026-08-17" が隣とくっついて読めない。完全な日付は
  // ツールチップで見られる。非表示中は clientWidth が 0 になるので画面幅で代用。
  const wrapW = canvas.parentElement ? canvas.parentElement.clientWidth : 0;
  const narrow = (wrapW > 0 ? wrapW : window.innerWidth) < 480;

  const th = chartTheme();
  const datasets = [{
    label: "value",
    data: values,
    borderColor: "#2f81f7",
    backgroundColor(ctx) {
      const area = ctx.chart.chartArea;
      if (!area) return "rgba(47,129,247,0.15)";
      const g = ctx.chart.ctx.createLinearGradient(0, area.top, 0, area.bottom);
      g.addColorStop(0, "rgba(47,129,247,0.25)");
      g.addColorStop(1, "rgba(47,129,247,0)");
      return g;
    },
    fill: true,
    tension: 0.3,
    pointRadius: 0,
    pointHoverRadius: 4,
    borderWidth: 2,
  }];
  // 取得コスト線は引かない: 取得原価を持たない資産（現金・ポイント・年金など）も
  // 評価額には乗るので、合計取得コストは常に過少で評価額と比べられない。
  // 併せて縦軸が評価額だけで決まり、変動が潰れずに見えるようになる。

  return new Chart(canvas, {
    type: "line",
    data: { labels, datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      scales: {
        x: {
          ticks: {
            color: th.tick,
            font: { size: 11 },
            maxTicksLimit: narrow ? 4 : 8,
            maxRotation: 0,
            callback(v) {
              const label = String(this.getLabelForValue(v));
              return narrow ? label.replace(/^\d{4}-/, "") : label;
            },
          },
          grid: { color: th.grid },
          border: { display: false },
        },
        y: {
          ticks: {
            color: th.tick,
            font: { size: 11 },
            callback(v) { return narrow ? fmtMoneyShort(v, currency) : fmtMoney(v, currency); },
          },
          grid: { color: th.grid },
          border: { display: false },
        },
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: th.tooltipBg,
          borderColor: th.tooltipBorder,
          borderWidth: 1,
          titleColor: th.tooltipTitle,
          bodyColor: th.tooltipBody,
          padding: 10,
          callbacks: {
            title: ([item]) => (item ? item.label : ""),
            label: (item) => "  " + fmtMoney(item.parsed.y, currency),
          },
        },
      },
    },
  });
}

function _setRangeActive(tabsId, range) {
  const tabs = document.getElementById(tabsId);
  if (!tabs) return;
  tabs.querySelectorAll(".range-tab").forEach((btn) =>
    btn.classList.toggle("active", btn.dataset.range === range));
}

async function _fetchHistAndRender(scope, range, canvasId, loadingId, unpricedId, getRef, setRef) {
  const currency = currentCurrency();
  const loading = document.getElementById(loadingId);
  const unpricedEl = document.getElementById(unpricedId);
  if (loading) loading.classList.remove("hidden");
  try {
    const data = await fetchJSON(
      `/api/portfolio-history?scope=${encodeURIComponent(scope)}&range=${range}&currency=${currency}`
    );
    setRef(renderHistoryChart(canvasId, data.points, currency, getRef()));
    if (unpricedEl) {
      const notes = [];
      if (data.is_partial) notes.push(t("label.historyPartial"));
      if (data.unpriced && data.unpriced.length) {
        notes.push(t("label.unpricedAssets") + data.unpriced.map(escapeHtml).join(", "));
      }
      // 手動評価待ちは「取得中」ではない。待っても出ないので入力を促す
      if (data.needs_valuation && data.needs_valuation.length) {
        notes.push(t("label.needsValuation") + data.needs_valuation.map(escapeHtml).join(", "));
      }
      if (notes.length) {
        unpricedEl.innerHTML = notes.join("<br>");
        unpricedEl.classList.remove("hidden");
      } else {
        unpricedEl.classList.add("hidden");
      }
    }
  } catch (e) {
    console.warn("[asset-summary] portfolio history:", e);
    setRef(renderHistoryChart(canvasId, [], currency, getRef()));
  } finally {
    if (loading) loading.classList.add("hidden");
  }
}

function loadDashHistoryChart(range) {
  _dashRange = range || _dashRange;
  localStorage.setItem("as_dash_range", _dashRange);
  _setRangeActive("dash-range-tabs", _dashRange);
  return _fetchHistAndRender(
    "total", _dashRange,
    "history-chart", "history-loading", "history-unpriced",
    () => _histChart, (c) => { _histChart = c; }
  );
}

function loadClassHistoryChart(classId, range) {
  if (classId != null) _classDetailId = classId;
  if (range != null) _classRange = range;
  if (!_classDetailId) return;
  localStorage.setItem("as_class_range", _classRange);
  _setRangeActive("class-range-tabs", _classRange);
  return _fetchHistAndRender(
    `class:${_classDetailId}`, _classRange,
    "class-history-chart", "class-history-loading", "class-history-unpriced",
    () => _classHistChart, (c) => { _classHistChart = c; }
  );
}

function loadAcctHistoryChart(name, range) {
  if (name != null) _acctDetailName = name;
  if (range != null) _acctRange = range;
  if (!_acctDetailName) return;
  localStorage.setItem("as_acct_range", _acctRange);
  _setRangeActive("acct-range-tabs", _acctRange);
  return _fetchHistAndRender(
    `account:${_acctDetailName}`, _acctRange,
    "acct-history-chart", "acct-history-loading", "acct-history-unpriced",
    () => _acctHistChart, (c) => { _acctHistChart = c; }
  );
}

function loadPfHistoryChart(scope, range) {
  if (scope != null) _pfHistScope = scope;
  if (range != null) _pfRange = range;
  if (!_pfHistScope) return;
  localStorage.setItem("as_pf_range", _pfRange);
  _setRangeActive("pf-range-tabs", _pfRange);
  return _fetchHistAndRender(
    _pfHistScope, _pfRange,
    "pf-history-chart", "pf-history-loading", "pf-history-unpriced",
    () => _pfHistChart, (c) => { _pfHistChart = c; }
  );
}

// ---- ドーナツチャート（クラス別構成・中央テキスト） ----

// 構成比5%未満のスライスが2つ以上あるときだけ「その他」1枚にまとめる
// （1つだけなら情報が減るだけなのでまとめない）。チャート表示のみの加工で、
// 内訳の表は全行を出す。sourceIndexes は元スライスの添字（表の行→スライスの
// ホバー同期に使う）。
const DONUT_OTHERS_PCT = 5;

function groupDonutSlices(slices, total) {
  const sum = Number(total) || slices.reduce((a, s) => a + (Number(s.value) || 0), 0);
  const withIdx = slices.map((s, i) => ({ ...s, sourceIndexes: [i] }));
  if (sum <= 0) return withIdx;
  const isSmall = (s) => (Number(s.value) / sum) * 100 < DONUT_OTHERS_PCT;
  const small = withIdx.filter(isSmall);
  if (small.length < 2) return withIdx;
  return [...withIdx.filter((s) => !isSmall(s)), {
    id: "__others__",
    isOthers: true,
    label: t("chart.others"),
    value: small.reduce((a, s) => a + Number(s.value), 0),
    color: "#6e7681",
    sourceIndexes: small.flatMap((s) => s.sourceIndexes),
  }];
}

/** 行番号→まとめ後のスライス番号の対応表（その他に畳まれた行はその他を指す）。 */
function _rowToSliceMap(groupedSlices) {
  const map = [];
  groupedSlices.forEach((s, gi) => (s.sourceIndexes || []).forEach((si) => { map[si] = gi; }));
  return map;
}

// ホバーが無い（タップ操作の）端末か。スライスのタップは画面遷移にせず、
// 中央テキストへの内訳表示だけにする（遷移は表の行から行う）
function _isTouchOnly() {
  return !!(window.matchMedia && window.matchMedia("(hover: none)").matches);
}

/**
 * ドーナツの余白＝スライスの外側ラベルの置き場所。輪の上下に帯を作る。
 * 左右ではなく上下に置くのは、ラベルが枠の幅をまるごと使えて名前を省略せずに
 * 済み、そのぶん輪を大きくできるため（左右に置くと輪の直径を左右のラベル幅の
 * 分だけ削ることになる）。輪の大きさは残った短辺で決まる。
 */
const DONUT_LABEL_BAND = 46;

function _donutPadding() {
  return { top: DONUT_LABEL_BAND, bottom: DONUT_LABEL_BAND, left: 4, right: 4 };
}

// スライス上に名前と構成比を直接描く（スマホはホバーで内訳を見られないため）。
// 帯に名前が入らないスライスは、ドーナツの外に「名前／%」を出して引き出し線で結ぶ。
// 出す・出さないは描画のたびに実寸（外半径・帯幅・左右の余白）から決める。
// 作成時のコンテナ寸法で決めると、その後のレイアウト確定やリサイズで輪が小さく
// なったときにラベルだけ残り、輪や中央テキストに重なってしまう。
const sliceLabelPlugin = {
  id: "sliceLabels",
  afterDatasetsDraw(chart) {
    const meta = chart.getDatasetMeta(0);
    const arcs = (meta && meta.data) || [];
    if (!arcs.length) return;
    const R = arcs[0].outerRadius || 0;
    const band = R - (arcs[0].innerRadius || 0);
    // 小さいドーナツ（PCの半幅カード・タブレット等）は文字が輪や中央テキストに
    // 被るので描かない（PCはホバーで内訳を見られる）
    if (R < 80 || band < 16) return;

    const data = chart.data.datasets[0].data;
    const sum = data.reduce((a, v) => a + (Number(v) || 0), 0);
    if (sum <= 0) return;

    const { ctx } = chart;
    const FONT = "-apple-system, 'Noto Sans JP', sans-serif";
    // 文字色はスライスごとに変えず、中央テキストと同じ「テーマの文字色＋フチ取り」
    // で統一する（どの塗り色の上でも同じ見た目で読める）
    const th = chartTheme();
    const isLight = document.documentElement.classList.contains("light");
    const outlineColor = isLight ? "rgba(255,255,255,0.9)" : "rgba(0,0,0,0.55)";
    const cx = arcs[0].x;
    // 外側ラベルは輪の上下に置く（枠の幅をまるごと使えるので名前を省略せずに済む）。
    // 上下の余白が足りなければ外には出さない（描画側で%だけにする）
    const cy = arcs[0].y;
    const vertRoom = Math.min(cy, chart.height - cy) - R - 10;
    const canCallout = vertRoom >= 26;

    ctx.save();
    ctx.textBaseline = "middle";
    ctx.lineJoin = "round";
    const drawOutlined = (text, x, y) => {
      ctx.lineWidth = 3;
      ctx.strokeStyle = outlineColor;
      ctx.strokeText(text, x, y);
      ctx.fillStyle = th.tooltipTitle;
      ctx.fillText(text, x, y);
    };
    const ellipsize = (text, max) => {
      if (ctx.measureText(text).width <= max) return text;
      let s = text;
      while (s.length > 1 && ctx.measureText(s + "…").width > max) s = s.slice(0, -1);
      return s + "…";
    };

    // 名前が帯に入るスライスは内側に描き、入らないものは外側ラベル行きにする
    const callouts = { top: [], bottom: [] };
    arcs.forEach((arc, i) => {
      const angle = arc.endAngle - arc.startAngle;
      if (!isFinite(angle) || angle <= 0.08) return;
      const name = String(chart.data.labels[i] ?? "");
      const pct = ((Number(data[i]) / sum) * 100).toFixed(1) + "%";
      const mid = (arc.startAngle + arc.endAngle) / 2;
      const rMid = (arc.innerRadius + arc.outerRadius) / 2;
      const chord = 2 * rMid * Math.sin(Math.min(angle, Math.PI) / 2);
      const y = arc.y + Math.sin(mid) * rMid;
      ctx.textAlign = "center";
      ctx.font = `600 11px ${FONT}`;
      const nameW = ctx.measureText(name).width;
      if (band >= 26 && nameW <= chord * 1.15) {
        ctx.font = `11px ${FONT}`;
        // キャンバスの端で文字が切れないよう、描画位置を内側へ寄せる
        const halfW = Math.max(nameW, ctx.measureText(pct).width) / 2;
        const x = Math.min(
          Math.max(arc.x + Math.cos(mid) * rMid, halfW + 2),
          chart.width - halfW - 2
        );
        ctx.font = `600 11px ${FONT}`;
        drawOutlined(name, x, y - 7);
        ctx.font = `11px ${FONT}`;
        drawOutlined(pct, x, y + 8);
        return;
      }
      if (canCallout) {
        callouts[Math.sin(mid) >= 0 ? "bottom" : "top"].push({ arc, mid, name, pct });
        return;
      }
      // 外に出す余白も無いときは%だけ（それも入らなければ何も描かない。表で見る）
      ctx.font = `11px ${FONT}`;
      const x = arc.x + Math.cos(mid) * rMid;
      if (ctx.measureText(pct).width <= chord) drawOutlined(pct, x, y);
    });

    // 上下の外側ラベル（2カラム表示。輪の左右には余白が無いが上下は空いている）。
    // 「名前 %」の1行で、輪の上／下に段を作って並べる。同じ段で重ならないよう
    // 横位置を確保し、置けない分は描かない（表で見る）
    for (const side of ["top", "bottom"]) {
      const dir = side === "top" ? -1 : 1;
      const list = callouts[side].sort((a, b) =>
        (a.arc.x + Math.cos(a.mid) * R) - (b.arc.x + Math.cos(b.mid) * R));
      const rows = [];
      list.forEach((c) => {
        ctx.font = `600 10px ${FONT}`;
        const text = ellipsize(`${c.name} ${c.pct}`, chart.width - 8);
        const w = ctx.measureText(text).width;
        const ax = c.arc.x + Math.cos(c.mid) * (R + 6);
        let tx = Math.min(Math.max(ax, w / 2 + 3), chart.width - w / 2 - 3);
        // 空いている段（上端／下端から順）に置く
        let row = 0;
        while (row < 3 && rows.some((r) => r.row === row && Math.abs(r.tx - tx) < (r.w + w) / 2 + 6)) {
          row += 1;
        }
        if (row >= 3) return;
        const ly = side === "top" ? 8 + row * 13 : chart.height - 8 - row * 13;
        if (Math.abs(ly - cy) < R + 6) return;  // 輪に重なるなら諦める
        rows.push({ row, tx, w });

        ctx.lineWidth = 1;
        ctx.strokeStyle = th.tick;
        ctx.beginPath();
        ctx.moveTo(c.arc.x + Math.cos(c.mid) * R, c.arc.y + Math.sin(c.mid) * R);
        ctx.lineTo(ax, c.arc.y + Math.sin(c.mid) * (R + 6));
        ctx.lineTo(tx, ly - dir * 6);
        ctx.stroke();

        ctx.textAlign = "center";
        drawOutlined(text, tx, ly);
      });
    }
    ctx.restore();
  },
};

const centerTextPlugin = {
  id: "centerText",
  afterDraw(chart) {
    const { ctx, chartArea } = chart;
    if (!chartArea) return;
    const cx = (chartArea.left + chartArea.right) / 2;
    const cy = (chartArea.top + chartArea.bottom) / 2;
    const cur = chart.$currency;
    const total = chart.$total || 0;
    const FONT = "-apple-system, 'Noto Sans JP', sans-serif";

    let title, sub;
    const idx = chart.$activeIndex;
    if (idx != null && chart.data.labels[idx] != null) {
      const val = chart.data.datasets[0].data[idx];
      const pct = total > 0 ? ((val / total) * 100).toFixed(1) + "%" : "";
      title = chart.data.labels[idx];
      sub = fmtMoney(val, cur) + (pct ? `  (${pct})` : "");
    } else {
      title = t("label.total");
      sub = fmtMoney(total, cur);
    }

    ctx.save();
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    const th = chartTheme();
    const isLight = document.documentElement.classList.contains("light");
    const outlineColor = isLight ? "rgba(255,255,255,0.9)" : "rgba(0,0,0,0.55)";

    function drawOutlinedText(text, x, y, fillColor, lineW) {
      ctx.lineWidth = lineW;
      ctx.lineJoin = "round";
      ctx.strokeStyle = outlineColor;
      ctx.strokeText(text, x, y);
      ctx.fillStyle = fillColor;
      ctx.fillText(text, x, y);
    }

    // 穴に収まる大きさまで字を落とす（小さいドーナツで輪に被らないように）
    const hole = ((chart.getDatasetMeta(0).data[0] || {}).innerRadius || 0) * 1.75;
    const maxW = hole > 0 ? hole : chartArea.right - chartArea.left;
    let subSize = 16;
    ctx.font = `700 ${subSize}px ${FONT}`;
    while (subSize > 9 && ctx.measureText(sub).width > maxW) {
      subSize -= 1;
      ctx.font = `700 ${subSize}px ${FONT}`;
    }
    const titleSize = Math.max(9, subSize - 2);
    const gap = subSize >= 14 ? 6 : 3;
    let top = cy - (titleSize + 3 + gap + subSize + 3) / 2;

    ctx.font = `600 ${titleSize}px ${FONT}`;
    drawOutlinedText(title, cx, top, th.tooltipTitle, 4);
    top += titleSize + 3 + gap;

    ctx.font = `700 ${subSize}px ${FONT}`;
    drawOutlinedText(sub, cx, top, th.tooltipTitle, 4);

    ctx.restore();
  },
};

function renderAllocChart(slices, currency, total) {
  const ctx = document.getElementById("alloc-chart");
  if (!ctx) return;
  if (allocChart) allocChart.destroy();
  allocChart = null;
  if (slices.length === 0) {
    ctx.getContext("2d").clearRect(0, 0, ctx.width, ctx.height);
    return;
  }

  const gSlices = groupDonutSlices(slices, total);
  allocChart = new Chart(ctx, {
    type: "doughnut",
    data: {
      labels: gSlices.map((s) => s.label),
      datasets: [{
        data: gSlices.map((s) => s.value),
        backgroundColor: gSlices.map((s) => s.color),
        borderWidth: 0,
        hoverOffset: 6,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      cutout: "68%",
      layout: { padding: _donutPadding() },
      // リサイズやレイアウト確定で寸法が変わっても余白を取り直す
      onResize(c) { c.options.layout.padding = _donutPadding(); },
      plugins: {
        legend: { display: false },
        tooltip: { enabled: false },
      },
      onHover(evt, elements) {
        const idx = elements.length ? elements[0].index : null;
        if (allocChart && allocChart.$activeIndex !== idx) {
          allocChart.$activeIndex = idx;
          allocChart.draw();
        }
      },
      onClick(evt, elements) {
        // タップ端末では遷移しない（中央テキストに内訳が出るだけにする）
        if (!elements.length || _isTouchOnly()) return;
        const s = (allocChart.$slices || [])[elements[0].index];
        if (s && !s.isOthers && s.id) navigate("classes", "detail", { name: s.id });
      },
    },
    plugins: [sliceLabelPlugin, centerTextPlugin],
  });
  allocChart.$currency = currency;
  allocChart.$total = total;
  allocChart.$activeIndex = null;
  allocChart.$slices = gSlices;
  allocChart.$rowToSlice = _rowToSliceMap(gSlices);
}

function setChartActive(idx) {
  if (!allocChart) return;
  const g = idx == null ? null : (allocChart.$rowToSlice || [])[idx];
  const next = g == null ? null : g;
  if (allocChart.$activeIndex !== next) {
    allocChart.$activeIndex = next;
    allocChart.draw();
  }
}

// ---- ダッシュボード ----

async function loadDashboard() {
  const currency = currentCurrency();
  const btn = document.getElementById("refresh");
  btn.classList.add("spin");
  document.getElementById("total-sub").textContent = t("label.loading");
  try {
    const data = await fetchJSON(`/api/summary?currency=${currency}`);
    _lastSummary = data;
    renderDashboard(data);
  } catch (e) {
    document.getElementById("total-sub").textContent = t("status.loadError") + e.message;
  } finally {
    btn.classList.remove("spin");
  }
  applyDashboardLayout();
  renderDashIncludeRow();
  loadDashHistoryChart(_dashRange);
  loadDashPortfolios();
  loadDashTags();
}

// ======================================================================
// ダッシュボードの表示項目（並び順・表示切替）
// ======================================================================

const DASH_WIDGET_LABELS = {
  history: "dash.valueHistory",
  classes: "dash.classAlloc",
  tags: "dash.tagAlloc",
  portfolios: "nav.portfolios",
  accounts: "dash.topAccounts",
  holdings: "dash.topHoldings",
};

function _layout() {
  const saved = (META && META.settings && META.settings.dashboard_layout) || [];
  return saved.length ? saved : Object.keys(DASH_WIDGET_LABELS).map(
    (id) => ({ id, visible: id !== "tags" })
  );
}

// 横半分に収まるウィジェット（隣り合う2つを横並びにする）。
// それ以外（推移グラフ・保有一覧など横に広い表）は常に全幅。
const HALF_WIDTH_WIDGETS = new Set(["classes", "tags", "accounts", "portfolios"]);

/** 設定に従ってウィジェットを並べ替え・表示切替し、横幅を割り当てる。 */
function applyDashboardLayout() {
  const host = document.getElementById("dash-widgets");
  if (!host) return;
  const byId = {};
  host.querySelectorAll("[data-widget]").forEach((el) => { byId[el.dataset.widget] = el; });

  const visible = [];
  _layout().forEach((item) => {
    const el = byId[item.id];
    if (!el) return;
    el.classList.toggle("hidden", !item.visible);
    host.appendChild(el);   // 設定順に末尾へ動かすことで並べ替える
    if (item.visible) visible.push(item.id);
  });

  // 半幅ウィジェットは連続する2つでペアを組む。相手がいなければ全幅に伸ばす
  // （1枚だけのグリッドが横半分で止まって見えるのを防ぐ）。
  let i = 0;
  while (i < visible.length) {
    const el = byId[visible[i]];
    const isHalf = HALF_WIDTH_WIDGETS.has(visible[i]);
    const nextIsHalf = i + 1 < visible.length && HALF_WIDTH_WIDGETS.has(visible[i + 1]);
    if (isHalf && nextIsHalf) {
      el.classList.remove("span-2");
      byId[visible[i + 1]].classList.remove("span-2");
      i += 2;
    } else {
      el.classList.add("span-2");
      i += 1;
    }
  }
}

function renderDashLayoutPanel() {
  const wrap = document.getElementById("dash-layout-list");
  if (!wrap) return;
  wrap.innerHTML = "";
  const layout = _layout();
  layout.forEach((item, idx) => {
    const row = document.createElement("div");
    row.className = "layout-row";

    const label = document.createElement("label");
    label.className = "layout-label";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = !!item.visible;
    cb.addEventListener("change", () => {
      layout[idx].visible = cb.checked;
      saveDashboardLayout(layout);
    });
    label.appendChild(cb);
    const name = document.createElement("span");
    name.textContent = t(DASH_WIDGET_LABELS[item.id] || item.id);
    label.appendChild(name);
    row.appendChild(label);

    const actions = document.createElement("div");
    actions.className = "layout-actions";
    const up = document.createElement("button");
    up.className = "btn-link";
    up.textContent = "↑";
    up.disabled = idx === 0;
    up.addEventListener("click", () => {
      [layout[idx - 1], layout[idx]] = [layout[idx], layout[idx - 1]];
      saveDashboardLayout(layout);
    });
    const down = document.createElement("button");
    down.className = "btn-link";
    down.textContent = "↓";
    down.disabled = idx === layout.length - 1;
    down.addEventListener("click", () => {
      [layout[idx + 1], layout[idx]] = [layout[idx], layout[idx + 1]];
      saveDashboardLayout(layout);
    });
    actions.appendChild(up);
    actions.appendChild(down);
    row.appendChild(actions);
    wrap.appendChild(row);
  });
}

async function saveDashboardLayout(layout) {
  if (!META) META = {};
  if (!META.settings) META.settings = {};
  META.settings.dashboard_layout = layout;
  applyDashboardLayout();
  renderDashLayoutPanel();
  try {
    await apiCall("/api/settings", "PUT", { dashboard_layout: layout });
    hideResult("dash-layout-result");
  } catch (e) {
    showResult("dash-layout-result", false, e.message);
  }
}

// ---- 総資産に含める資産クラスの切替 ----

function renderDashIncludeRow() {
  const el = document.getElementById("dash-include-row");
  if (!el || !_lastSummary) return;
  const includes = (META && META.settings && META.settings.include_classes) || {};

  // 設定で明示されていればそれに従う。未設定なら「保有があるクラス」を自動表示。
  // どちらの場合も、総資産から外しているクラスは操作子を残す（戻せなくなるため）。
  const chosen = (META && META.settings && META.settings.dashboard_chip_classes) || null;
  const present = new Set((_lastSummary.classes || []).map((c) => c.class));
  const ids = ((META && META.asset_classes) || [])
    .map((c) => c.id)
    .filter((id) => (chosen ? chosen.includes(id) : present.has(id)) || includes[id] === false);
  if (!ids.length) {
    el.classList.add("hidden");
    return;
  }
  el.innerHTML = `<span class="include-label">${escapeHtml(t("dash.includeLabel"))}</span>`;
  ids.forEach((id) => {
    const on = includes[id] !== false;
    const chip = document.createElement("label");
    chip.className = "include-chip" + (on ? " on" : "");
    chip.style.setProperty("--tag-color", classColor(id));
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = on;
    cb.addEventListener("change", () => setClassIncluded(id, cb.checked));
    chip.appendChild(cb);
    const span = document.createElement("span");
    span.textContent = classLabel(id);
    chip.appendChild(span);
    el.appendChild(chip);
  });
  el.classList.remove("hidden");
}

async function setClassIncluded(classId, included) {
  if (!META) META = {};
  if (!META.settings) META.settings = {};
  if (!META.settings.include_classes) META.settings.include_classes = {};
  META.settings.include_classes[classId] = included;
  try {
    await apiCall("/api/settings", "PUT", {
      include_classes: { [classId]: included },
    });
  } catch (e) {
    console.warn("[asset-summary] include class:", e);
  }
  loadDashboard();          // 総額・構成比が変わるので読み直す
  renderSettingsIncludes();  // 設定ページ側のチェックも同期
}

// ---- ダッシュボードの Myポートフォリオ / タグ別 ----

async function loadDashPortfolios() {
  const item = _layout().find((w) => w.id === "portfolios");
  if (!item || !item.visible) return;
  const tbody = document.querySelector("#dash-portfolios-table tbody");
  const emptyEl = document.getElementById("dash-portfolios-empty");
  if (!tbody) return;
  const currency = currentCurrency();
  let list = [];
  try {
    list = (await fetchJSON(`/api/portfolios?currency=${currency}`)).portfolios || [];
  } catch (e) {
    console.warn("[asset-summary] dash portfolios:", e);
  }
  tbody.innerHTML = "";
  emptyEl.classList.toggle("hidden", list.length > 0);
  document.querySelector("#dash-portfolios-table").classList.toggle("hidden", !list.length);
  list.forEach((p) => {
    const tr = document.createElement("tr");
    tr.className = "clickable";
    tr.innerHTML =
      `<td>${escapeHtml(p.name)}</td>` +
      `<td class="num">${p.holding_count}</td>` +
      `<td class="num">${fmtMoney(p.value, currency)}</td>` +
      `<td class="num">${dayChangeCellHtml(p, currency)}</td>` +
      `<td class="num">${plAmountHtml(p.pl, currency)}</td>` +
      `<td class="num">${plPctHtml(p.pl_pct)}</td>` +
      `<td class="chev">›</td>`;
    tr.addEventListener("click", () => navigate("portfolios", "detail", { id: p.id }));
    tbody.appendChild(tr);
  });
}

async function loadDashTags() {
  const item = _layout().find((w) => w.id === "tags");
  if (!item || !item.visible) return;
  const currency = currentCurrency();
  try {
    const d = await fetchJSON(`/api/tag-summary?currency=${currency}`);
    _dashTagChart = _renderTagChartAndTable(
      d.by_tag, d.total_value, currency,
      "dash-tag-chart", "dash-tag-table", _dashTagChart, { clickable: true }
    );
  } catch (e) {
    console.warn("[asset-summary] dash tags:", e);
  }
}

function renderDashboard(data) {
  const cur = data.currency;
  const total = Number(data.total_value) || 0;

  // ヒーロー
  document.getElementById("total-value").textContent = fmtMoney(data.total_value, cur);
  const jpyEl = document.getElementById("total-jpy");
  if (cur === "JPY") {
    jpyEl.textContent = fmtJpy(data.total_value);
    jpyEl.classList.remove("hidden");
  } else {
    jpyEl.classList.add("hidden");
  }

  const plEl = document.getElementById("total-pl");
  if (data.total_pl != null) {
    plEl.innerHTML = `<span class="${plClass(data.total_pl)}">${fmtSignedMoney(data.total_pl, cur)}` +
      (data.total_pl_pct != null ? ` (${fmtSignedPct(data.total_pl_pct)})` : "") + `</span>`;
  } else {
    plEl.innerHTML = "";
  }
  renderHeroDayChange("total-day", data, cur);

  const noteEl = document.getElementById("pl-note");
  const notes = [];
  if (Number(data.pl_excluded_count) > 0) {
    notes.push(t("dash.plExcluded", { count: data.pl_excluded_count }));
  }
  if (data.day_change_partial) notes.push(t("label.dayChangePartial"));
  noteEl.textContent = notes.join(" / ");
  noteEl.classList.toggle("hidden", notes.length === 0);

  document.getElementById("total-sub").textContent =
    t("dash.holdingsSummary", { count: data.holding_count, priced: data.priced_count });
  document.getElementById("generated").textContent =
    t("status.updatedAt", { time: new Date(data.generated_at).toLocaleString() });

  // 警告帯: API警告 + 価格未取得 + 価格ソース未設定
  const warnEl = document.getElementById("warnings");
  const extras = [];
  if (data.unpriced && data.unpriced.length) {
    extras.push("⚠ " + t("label.unpricedAssets") + data.unpriced.map(escapeHtml).join(", "));
  }
  const unlinkedCount = (data.holdings || []).filter(
    (h) => h.price_source_status === "unlinked"
  ).length;
  if (unlinkedCount > 0) {
    extras.push(
      "⚠ " + escapeHtml(t("dash.unlinkedWarn", { count: unlinkedCount })) +
      ` <a href="#settings">${t("dash.unlinkedLink")}</a>`
    );
  }
  const cs = data.crypto_summary;
  if (cs && cs.configured && cs.connected === false) {
    extras.push(
      "⚠ " + escapeHtml(t("cs.unreachableWarn")) +
      ` <a href="#classes/detail?name=crypto">${t("cs.unreachableLink")}</a>`
    );
  }
  renderWarningsInto(warnEl, data.warnings, extras.join("<br>"));

  // 空状態ガイド
  const emptyGuide = document.getElementById("empty-guide");
  emptyGuide.classList.toggle("hidden", (data.holdings || []).length > 0);

  // ドーナツ + クラステーブル（総資産に含めるクラスのみ）
  const classRows = (data.classes || []).filter((c) => c.in_total !== false && Number(c.value) > 0);
  const slices = classRows.map((c) => ({
    id: c.class,
    label: classLabel(c.class),
    value: Number(c.value),
    color: c.color || classColor(c.class),
  }));
  renderAllocChart(slices, cur, total);

  const ctbody = document.querySelector("#class-table tbody");
  ctbody.innerHTML = "";
  classRows.forEach((c, i) => {
    const tr = document.createElement("tr");
    tr.className = "clickable";
    const weightStr = c.weight != null ? Number(c.weight).toFixed(1) + "%" : "";
    tr.innerHTML = `
      <td><span class="asset-name"><span class="swatch" style="background:${c.color || classColor(c.class)}"></span><span class="asset-label">${escapeHtml(classLabel(c.class))}</span></span></td>
      <td class="num">${valueWeightCellHtml(c.value, cur, weightStr)}</td>
      <td class="num">${dayChangeCellHtml(c, cur)}</td>
      <td class="num weight-col">${weightStr || '<span class="muted">—</span>'}</td>
    `;
    tr.addEventListener("click", () => navigate("classes", "detail", { name: c.class }));
    tr.addEventListener("mouseenter", () => setChartActive(i));
    tr.addEventListener("mouseleave", () => setChartActive(null));
    ctbody.appendChild(tr);
  });
  if (classRows.length === 0) {
    ctbody.innerHTML = `<tr><td colspan="4" class="muted">${t("label.noData")}</td></tr>`;
  }

  // 口座別上位（holdings を口座でグルーピング）
  const acctMap = {};
  (data.holdings || []).forEach((h) => {
    if (h.in_total === false) return;
    (acctMap[h.account] || (acctMap[h.account] = [])).push(h);
  });
  // 集計は API の _totals_block と同じ規約（aggregateRows）で行う。口座別ページと
  // 同じ数字が出ないと「同じ口座なのに画面で額が違う」ことになるため。
  const accts = Object.entries(acctMap)
    .map(([name, rows]) => ({ account: name, count: rows.length, ...aggregateRows(rows) }))
    .sort((x, y) => y.total_value - x.total_value);
  const atbody = document.querySelector("#top-accounts-table tbody");
  atbody.innerHTML = "";
  accts.slice(0, DASH_TOP_ACCOUNTS).forEach((a) => {
    const tr = document.createElement("tr");
    tr.className = "clickable";
    const weightStr = total ? (a.total_value / total * 100).toFixed(1) + "%" : "";
    tr.innerHTML = `
      <td>${escapeHtml(a.account)} <span class="row-arrow">›</span></td>
      <td class="num">${a.count}</td>
      <td class="num">${valueWeightCellHtml(a.total_value, cur, weightStr)}</td>
      <td class="num">${dayChangeCellHtml({ day_change: a.total_day_change, day_change_pct: a.total_day_change_pct }, cur)}</td>
      <td class="num">${plAmountHtml(a.total_pl, cur)}</td>
      <td class="num">${plPctHtml(a.total_pl_pct)}</td>
      <td class="num weight-col">${weightStr || '<span class="muted">—</span>'}</td>
      <td></td>
    `;
    tr.addEventListener("click", () => navigate("accounts", "detail", { name: a.account }));
    atbody.appendChild(tr);
  });
  if (accts.length === 0) {
    atbody.innerHTML = `<tr><td colspan="8" class="muted">${t("label.noData")}</td></tr>`;
  }

  // 主な保有（評価額上位）
  const sorted = [...(data.holdings || [])].sort((x, y) => (Number(y.value) || 0) - (Number(x.value) || 0));
  renderHoldingsRows(
    document.querySelector("#top-holdings-table tbody"),
    sorted.slice(0, DASH_TOP_HOLDINGS), cur
  );
}

// ---- クラス別ページ ----

function showClassesList() {
  document.getElementById("classes-list-view").classList.remove("hidden");
  document.getElementById("class-detail-view").classList.add("hidden");
  document.getElementById("cs-asset-view").classList.add("hidden");
}

async function loadClassesPage() {
  const currency = currentCurrency();
  const tbody = document.querySelector("#classes-table tbody");
  tbody.innerHTML = `<tr><td colspan="8" class="loading">${t("label.loading")}</td></tr>`;
  let rows = null;
  try {
    const data = await fetchJSON(`/api/classes?currency=${currency}`);
    rows = data.classes || [];
  } catch (e) {
    // フォールバック: /api/summary の classes を使う
    try {
      const data = await fetchJSON(`/api/summary?currency=${currency}`);
      _lastSummary = data;
      rows = data.classes || [];
    } catch (e2) {
      tbody.innerHTML = `<tr><td colspan="8" class="muted">${t("status.error")}${escapeHtml(e2.message)}</td></tr>`;
      return;
    }
  }
  tbody.innerHTML = "";
  rows.forEach((c) => {
    const tr = document.createElement("tr");
    tr.className = "clickable" + (c.in_total === false ? " row-dim" : "");
    const weightSub = c.in_total === false
      ? `<span class="muted">${t("label.excluded")}</span>`
      : (c.weight != null ? Number(c.weight).toFixed(1) + "%" : "");
    const weight = weightSub || '<span class="muted">—</span>';
    tr.innerHTML = `
      <td>${classBadgeHtml(c.class)}</td>
      <td class="num">${c.holding_count != null ? c.holding_count : ""}</td>
      <td class="num">${valueWeightCellHtml(c.value, currency, weightSub)}</td>
      <td class="num">${dayChangeCellHtml(c, currency)}</td>
      <td class="num">${plAmountHtml(c.pl, currency)}</td>
      <td class="num">${plPctHtml(c.pl_pct)}</td>
      <td class="num weight-col">${weight}</td>
      <td><span class="row-arrow">›</span></td>
    `;
    tr.addEventListener("click", () => navigate("classes", "detail", { name: c.class }));
    tbody.appendChild(tr);
  });
  if (rows.length === 0) {
    tbody.innerHTML = `<tr><td colspan="8" class="muted">${t("label.noData")}</td></tr>`;
  }
}

async function showClassDetail(classId) {
  document.getElementById("classes-list-view").classList.add("hidden");
  document.getElementById("class-detail-view").classList.remove("hidden");
  document.getElementById("cs-asset-view").classList.add("hidden");
  document.getElementById("class-detail-name").innerHTML = classBadgeHtml(classId);
  if (_classDetailId !== classId) _classRange = localStorage.getItem("as_class_range") || "90d";
  loadClassHistoryChart(classId, _classRange);

  // 暗号資産クラスのみ: Crypto-Summary へのリンクアウト
  const csLink = document.getElementById("class-cs-link");
  const csUrl = csMeta().url;
  const showCsLink = classId === "crypto" && csMeta().enabled && csUrl;
  csLink.classList.toggle("hidden", !showCsLink);
  if (showCsLink) {
    csLink.href = csUrl + "/#dashboard";
    csLink.textContent = t("cs.openApp");
  }

  const currency = currentCurrency();
  const tbody = document.querySelector("#class-holdings-table tbody");
  const loading = document.getElementById("class-detail-loading");
  const warnEl = document.getElementById("class-detail-warnings");
  tbody.innerHTML = "";
  _classHoldings = null;
  clearDetailHero("class-detail");
  renderWarningsInto(warnEl, []);
  loading.classList.remove("hidden");
  try {
    _classHoldings = await fetchJSON(
      `/api/class-holdings?class=${encodeURIComponent(classId)}&currency=${currency}`
    );
    renderClassHoldings();
    const cs = _classHoldings.crypto_summary;
    if (cs && cs.configured && cs.connected === false) {
      renderWarningsInto(warnEl, [], "⚠ " + escapeHtml(t("cs.unreachableDetail")));
    }
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="10" class="muted">${t("status.error")}${escapeHtml(e.message)}</td></tr>`;
  } finally {
    loading.classList.add("hidden");
  }
}

/** 取得済みのクラス詳細を描き直す（ソート切替でも再取得しない）。 */
function renderClassHoldings() {
  const d = _classHoldings;
  if (!d) return;
  renderDetailHero("class-detail", d, d.currency);
  renderHoldingsRows(
    document.querySelector("#class-holdings-table tbody"),
    sortHoldingRows(d.holdings || [], _classHoldingsSort.key, _classHoldingsSort.dir),
    d.currency
  );
  _syncSortIndicators("class-holdings-table", _classHoldingsSort);
}

// ---- Crypto-Summary コイン別サブビュー ----

async function showCsAssetDetail(sym, range) {
  document.getElementById("classes-list-view").classList.add("hidden");
  document.getElementById("class-detail-view").classList.add("hidden");
  document.getElementById("cs-asset-view").classList.remove("hidden");

  sym = String(sym || "").toUpperCase();
  if (_csAssetSym !== sym && range == null) {
    _csAssetRange = localStorage.getItem("as_cs_range") || "90d";
  }
  if (range != null) _csAssetRange = range;
  _csAssetSym = sym;
  localStorage.setItem("as_cs_range", _csAssetRange);
  _setRangeActive("cs-asset-range-tabs", _csAssetRange);

  const currency = currentCurrency();
  const nameEl = document.getElementById("cs-asset-name");
  const metaEl = document.getElementById("cs-asset-meta");
  const warnEl = document.getElementById("cs-asset-warnings");
  const tbody = document.querySelector("#cs-asset-accounts-table tbody");
  const loading = document.getElementById("cs-asset-loading");

  nameEl.innerHTML = `${csCoinIconHtml(sym)}${escapeHtml(sym)}`;
  metaEl.innerHTML = `${classBadgeHtml("crypto")} ${csBadgeHtml()}`;
  renderWarningsInto(warnEl, []);

  // リンクアウト（CS のコイン詳細へ直接飛ぶ）
  const link = document.getElementById("cs-asset-link");
  const csUrl = csMeta().url;
  link.classList.toggle("hidden", !csUrl);
  if (csUrl) {
    link.href = `${csUrl}/#assets/detail?name=${encodeURIComponent(sym)}`;
    link.textContent = t("cs.openAsset");
  }

  ["cs-tile-balance", "cs-tile-price", "cs-tile-value", "cs-tile-day-change"].forEach((id) => {
    document.getElementById(id).textContent = "—";
  });
  tbody.innerHTML = "";
  loading.classList.remove("hidden");
  try {
    const data = await fetchJSON(
      `/api/crypto-summary/asset/${encodeURIComponent(sym)}?currency=${currency}&range=${_csAssetRange}`
    );
    document.getElementById("cs-tile-balance").textContent = fmtAmount(data.balance);
    document.getElementById("cs-tile-price").textContent = fmtPrice(data.price, currency);
    document.getElementById("cs-tile-value").textContent = fmtMoney(data.value, currency);
    document.getElementById("cs-tile-day-change").innerHTML = dayChangeCellHtml(data, currency);
    if (data.cs_generated_at || data.generated_at) {
      metaEl.innerHTML += ` <span class="muted">${escapeHtml(
        t("status.updatedAt", { time: fmtDate(data.cs_generated_at || data.generated_at) })
      )}</span>`;
    }

    _csAssetChart = renderHistoryChart(
      "cs-asset-history-chart", (data.history || {}).points || [], currency, _csAssetChart
    );

    (data.accounts || []).forEach((a) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${escapeHtml(a.account || "")}</td>
        <td class="num">${fmtAmount(a.balance)}</td>
        <td class="num">${fmtMoney(a.value, currency)}</td>
      `;
      tbody.appendChild(tr);
    });
    if (!(data.accounts || []).length) {
      tbody.innerHTML = `<tr><td colspan="3" class="muted">${t("label.noData")}</td></tr>`;
    }
    if (data.connected === false) {
      renderWarningsInto(warnEl, data.warnings, "⚠ " + escapeHtml(t("cs.unreachableDetail")));
    } else {
      renderWarningsInto(warnEl, data.warnings);
    }
  } catch (e) {
    _csAssetChart = renderHistoryChart("cs-asset-history-chart", [], currency, _csAssetChart);
    renderWarningsInto(warnEl, [], "⚠ " + escapeHtml(t("status.error")) + escapeHtml(e.message));
  } finally {
    loading.classList.add("hidden");
  }
}

// ---- 口座別ページ ----

function showAccountsList() {
  document.getElementById("accounts-list-view").classList.remove("hidden");
  document.getElementById("account-detail-view").classList.add("hidden");
}

async function _loadAccountsCache() {
  const data = await fetchJSON(`/api/accounts?currency=${currentCurrency()}`);
  _accounts = data.accounts || [];
  _accountIdByName = {};
  _accounts.forEach((a) => {
    _accountIdByName[a.display_name || a.name] = a.id;
  });
  _syncAccountDatalist();
  return _accounts;
}

// 種類ごとの保管先プリセット。既存の口座名に加えて候補に出すことで、
// 「自宅保管」のようなよくある置き場をメニューから選べるようにする。
const ACCOUNT_PRESETS = {
  "cash-account-list": ["自宅保管", "タンス預金", "手元現金"],
  "metal-account-list": ["自宅保管", "貸金庫", "田中貴金属", "三菱マテリアル"],
  "crypto-account-list": ["自宅ウォレット", "ハードウェアウォレット",
                          "GMOコイン", "bitFlyer", "Coincheck", "bitbank"],
};

function _fillDatalist(id, values) {
  const dl = document.getElementById(id);
  if (!dl) return;
  dl.innerHTML = "";
  Array.from(new Set(values)).forEach((v) => {
    const opt = document.createElement("option");
    opt.value = v;
    dl.appendChild(opt);
  });
}

function _syncAccountDatalist() {
  const names = _accounts.map((a) => a.display_name || a.name);
  _fillDatalist("account-name-list", names);
  // 種類別のリストは「既存の口座名 → プリセット」の順で出す
  Object.entries(ACCOUNT_PRESETS).forEach(([id, presets]) => {
    _fillDatalist(id, [...names, ...presets]);
  });
}

async function loadAccountsPage() {
  const currency = currentCurrency();
  const tbody = document.querySelector("#accounts-table tbody");
  tbody.innerHTML = `<tr><td colspan="8" class="loading">${t("label.loading")}</td></tr>`;
  try {
    const [, summary] = await Promise.all([
      _loadAccountsCache(),
      fetchJSON(`/api/summary?currency=${currency}`),
    ]);
    _lastSummary = summary;

    // 損益・前日比は /api/accounts の集計をそのまま使う（口座詳細のヘッダと
    // 同じ _totals_block なので、一覧と詳細で数字がずれない）
    const byName = {};
    (summary.holdings || []).forEach((h) => {
      (byName[h.account] || (byName[h.account] = [])).push(h);
    });
    const rows = _accounts.map((a) => {
      const name = a.display_name || a.name;
      return {
        id: a.id, name, count: a.holding_count,
        total_value: Number(a.value || 0),
        total_pl: a.pl, total_pl_pct: a.pl_pct,
        total_day_change: a.day_change, total_day_change_pct: a.day_change_pct,
      };
    });
    // Crypto-Summary の疑似口座のように、summary にしか現れない口座も拾う
    Object.keys(byName).forEach((name) => {
      if (!rows.some((r) => r.name === name)) {
        rows.push({ id: null, name, count: byName[name].length, ...aggregateRows(byName[name]) });
      }
    });
    rows.sort((x, y) => y.total_value - x.total_value);
    const total = rows.reduce((sum, r) => sum + (r.total_value || 0), 0);

    tbody.innerHTML = "";
    rows.forEach((r) => {
      const tr = document.createElement("tr");
      tr.className = "clickable";
      const weightStr = total ? (r.total_value / total * 100).toFixed(1) + "%" : "";
      tr.innerHTML = `
        <td>${escapeHtml(r.name)} <span class="row-arrow">›</span></td>
        <td class="num">${r.count}</td>
        <td class="num">${valueWeightCellHtml(r.total_value, currency, weightStr)}</td>
        <td class="num">${dayChangeCellHtml({ day_change: r.total_day_change, day_change_pct: r.total_day_change_pct }, currency)}</td>
        <td class="num">${plAmountHtml(r.total_pl, currency)}</td>
        <td class="num">${plPctHtml(r.total_pl_pct)}</td>
        <td class="num weight-col">${weightStr || '<span class="muted">—</span>'}</td>
        <td><span class="row-arrow">›</span></td>
      `;
      tr.addEventListener("click", () => navigate("accounts", "detail", { name: r.name }));
      tbody.appendChild(tr);
    });
    if (rows.length === 0) {
      tbody.innerHTML = `<tr><td colspan="8" class="muted">${t("label.noData")}</td></tr>`;
    }
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="8" class="muted">${t("status.error")}${escapeHtml(e.message)}</td></tr>`;
  }
}

async function showAccountDetail(name) {
  document.getElementById("accounts-list-view").classList.add("hidden");
  document.getElementById("account-detail-view").classList.remove("hidden");
  document.getElementById("account-detail-name").textContent = name;
  document.getElementById("account-edit-panel").classList.add("hidden");
  hideResult("account-edit-result");

  if (_acctDetailName !== name) _acctRange = localStorage.getItem("as_acct_range") || "90d";
  loadAcctHistoryChart(name, _acctRange);

  // 直リンク時に備えて口座キャッシュを確保（✎編集用の id 解決）
  if (_accounts.length === 0) {
    try { await _loadAccountsCache(); } catch (e) { /* 編集不可になるだけ */ }
  }

  const currency = currentCurrency();
  const tbody = document.querySelector("#account-holdings-table tbody");
  const loading = document.getElementById("account-detail-loading");
  tbody.innerHTML = "";
  _acctHoldings = null;
  clearDetailHero("account-detail");
  loading.classList.remove("hidden");
  try {
    _acctHoldings = await fetchJSON(
      `/api/account-holdings?account=${encodeURIComponent(name)}&currency=${currency}`
    );
    renderAcctHoldings();
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="10" class="muted">${t("status.error")}${escapeHtml(e.message)}</td></tr>`;
  } finally {
    loading.classList.add("hidden");
  }
}

/** 取得済みの口座詳細を描き直す（ソート切替でも再取得しない）。 */
function renderAcctHoldings() {
  const d = _acctHoldings;
  if (!d) return;
  renderDetailHero("account-detail", d, d.currency);
  renderHoldingsRows(
    document.querySelector("#account-holdings-table tbody"),
    sortHoldingRows(d.holdings || [], _acctHoldingsSort.key, _acctHoldingsSort.dir),
    d.currency
  );
  _syncSortIndicators("account-holdings-table", _acctHoldingsSort);
}

// 口座表示名の編集
document.getElementById("account-edit-btn").addEventListener("click", () => {
  const panel = document.getElementById("account-edit-panel");
  if (panel.classList.contains("hidden")) {
    document.getElementById("account-display-input").value = _acctDetailName || "";
    hideResult("account-edit-result");
    panel.classList.remove("hidden");
  } else {
    panel.classList.add("hidden");
  }
});

document.getElementById("account-display-cancel").addEventListener("click", () => {
  document.getElementById("account-edit-panel").classList.add("hidden");
});

document.getElementById("account-display-save").addEventListener("click", async () => {
  const newName = document.getElementById("account-display-input").value.trim();
  if (!newName) {
    showResult("account-edit-result", false, t("status.nameRequired"));
    return;
  }
  const id = _accountIdByName[_acctDetailName];
  if (id == null) {
    showResult("account-edit-result", false, t("status.settingsSaveFail", { error: "account id not found" }));
    return;
  }
  try {
    await apiCall(`/api/accounts/${id}`, "PUT", { display_name: newName });
    showResult("account-edit-result", true, t("status.settingsSaved"));
    await _loadAccountsCache();
    navigate("accounts", "detail", { name: newName });
  } catch (e) {
    showResult("account-edit-result", false, t("status.settingsSaveFail", { error: e.message }));
  }
});

// ---- 保有一覧ページ ----

function showHoldingsList() {
  document.getElementById("holdings-list-view").classList.remove("hidden");
  document.getElementById("security-detail-view").classList.add("hidden");
}

async function loadHoldingsPage() {
  const currency = currentCurrency();
  const tbody = document.querySelector("#holdings-table tbody");
  const cols = HOLDINGS_BASE_COLS + 1;
  tbody.innerHTML = `<tr><td colspan="${cols}" class="loading">${t("label.loading")}</td></tr>`;
  try {
    const data = await fetchJSON(`/api/summary?currency=${currency}`);
    _lastSummary = data;
    renderHoldingsFilterChips(data);
    renderHoldingsList();
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="${cols}" class="muted">${t("status.error")}${escapeHtml(e.message)}</td></tr>`;
  }
}

function renderHoldingsFilterChips(data) {
  const wrap = document.getElementById("holdings-filter-chips");
  wrap.innerHTML = "";
  const present = [];
  (data.classes || []).forEach((c) => {
    if (!present.includes(c.class)) present.push(c.class);
  });

  const mkChip = (id, label) => {
    const btn = document.createElement("button");
    btn.className = "filter-chip" + ((_holdingsFilter || "") === id ? " active" : "");
    btn.textContent = label;
    btn.addEventListener("click", () => {
      _holdingsFilter = id;
      wrap.querySelectorAll(".filter-chip").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      renderHoldingsList();
    });
    return btn;
  };
  wrap.appendChild(mkChip("", t("label.filterAll")));
  present.forEach((cid) => wrap.appendChild(mkChip(cid, classLabel(cid))));
}

// 保有一覧のソート。数値列は数値として、それ以外は日本語ロケールで比較する。
const _HOLDINGS_NUMERIC_COLS = new Set([
  "quantity", "avg_cost", "price", "value", "day_change", "pl", "pl_pct",
  "portfolio_value",
]);

function sortHoldingRows(rows, key, dir) {
  if (!key) return rows;
  const numeric = _HOLDINGS_NUMERIC_COLS.has(key);
  const sign = dir === "asc" ? 1 : -1;
  return rows.slice().sort((a, b) => {
    const av = a[key], bv = b[key];
    // 値なし（—）は方向によらず常に末尾へ
    const aNull = av === null || av === undefined || av === "";
    const bNull = bv === null || bv === undefined || bv === "";
    if (aNull !== bNull) return aNull ? 1 : -1;
    if (aNull && bNull) return 0;
    if (numeric) {
      const d = Number(av) - Number(bv);
      return d === 0 ? 0 : (d > 0 ? sign : -sign);
    }
    return String(av).localeCompare(String(bv), "ja") * sign;
  });
}

function _syncSortIndicators(tableId, state) {
  document.querySelectorAll(`#${tableId} thead th[data-sort]`).forEach((th) => {
    const active = th.dataset.sort === state.key;
    th.classList.toggle("sorted", active);
    th.classList.toggle("asc", active && state.dir === "asc");
    th.classList.toggle("desc", active && state.dir === "desc");
  });
}

/** 見出しクリックでソート（同じ列を再クリックで昇順/降順を反転）。 */
function wireSortableTable(tableId, state, rerender) {
  document.querySelectorAll(`#${tableId} thead th[data-sort]`).forEach((th) => {
    th.addEventListener("click", () => {
      const key = th.dataset.sort;
      if (state.key === key) {
        state.dir = state.dir === "asc" ? "desc" : "asc";
      } else {
        // 数値列は「大きい順」、文字列列は「あいうえお順」から始めるのが自然
        state.key = key;
        state.dir = _HOLDINGS_NUMERIC_COLS.has(key) ? "desc" : "asc";
      }
      localStorage.setItem(state.storageKey, JSON.stringify({ key: state.key, dir: state.dir }));
      rerender();
    });
  });
}

function renderHoldingsList() {
  if (!_lastSummary) return;
  const currency = _lastSummary.currency;
  const q = (_holdingsSearch || "").toLowerCase();
  // 保有一覧は銘柄単位（同じ銘柄を複数口座で持っていても1行）。口座別の内訳は
  // 各行の accounts と、口座ページ側で見る。
  let rows = _lastSummary.holdings_by_security || _lastSummary.holdings || [];
  if (_holdingsFilter) rows = rows.filter((h) => h.asset_class === _holdingsFilter);
  if (q) {
    rows = rows.filter((h) =>
      (h.name || "").toLowerCase().includes(q) ||
      (h.code || "").toLowerCase().includes(q) ||
      (h.account || "").toLowerCase().includes(q) ||
      (h.accounts || []).some((a) => (a.account || "").toLowerCase().includes(q)));
  }
  rows = sortHoldingRows(rows, _holdingsSort.key, _holdingsSort.dir);
  _syncSortIndicators("holdings-table", _holdingsSort);
  renderHoldingsRows(document.querySelector("#holdings-table tbody"), rows, currency);

  const unpricedEl = document.getElementById("holdings-unpriced");
  if (_lastSummary.unpriced && _lastSummary.unpriced.length) {
    unpricedEl.textContent = t("label.unpricedAssets") + _lastSummary.unpriced.join(", ");
    unpricedEl.classList.remove("hidden");
  } else {
    unpricedEl.classList.add("hidden");
  }
}

document.getElementById("holdings-search").addEventListener("input", (e) => {
  _holdingsSearch = e.target.value;
  renderHoldingsList();
});

wireSortableTable("holdings-table", _holdingsSort, renderHoldingsList);
wireSortableTable("class-holdings-table", _classHoldingsSort, () => renderClassHoldings());
wireSortableTable("account-holdings-table", _acctHoldingsSort, () => renderAcctHoldings());
wireSortableTable("pf-holdings-table", _pfSort, () => renderPortfolioDetail(_pfDetailOpts));

// ---- 銘柄詳細 ----

function renderPriceChart(priceHistory, avgCost, currency) {
  const canvas = document.getElementById("price-chart");
  if (!canvas) return;
  if (_priceChart) { _priceChart.destroy(); _priceChart = null; }

  const emptyEl = canvas.parentElement.querySelector(".history-empty");
  if (!priceHistory || priceHistory.length < 2) {
    canvas.style.display = "none";
    if (emptyEl) emptyEl.classList.remove("hidden");
    return;
  }
  canvas.style.display = "";
  if (emptyEl) emptyEl.classList.add("hidden");

  const labels = priceHistory.map((p) => p.t);
  const values = priceHistory.map((p) => Number(p.price));
  // 推移グラフと同じ理由でスマホ幅では日付目盛りを短縮・間引きする
  const wrapW = canvas.parentElement ? canvas.parentElement.clientWidth : 0;
  const narrow = (wrapW > 0 ? wrapW : window.innerWidth) < 480;
  const th = chartTheme();

  const datasets = [{
    label: "price",
    data: values,
    borderColor: "#2f81f7",
    backgroundColor(ctx) {
      const area = ctx.chart.chartArea;
      if (!area) return "rgba(47,129,247,0.15)";
      const g = ctx.chart.ctx.createLinearGradient(0, area.top, 0, area.bottom);
      g.addColorStop(0, "rgba(47,129,247,0.25)");
      g.addColorStop(1, "rgba(47,129,247,0)");
      return g;
    },
    fill: true,
    tension: 0.3,
    pointRadius: 0,
    pointHoverRadius: 4,
    borderWidth: 2,
  }];
  // 指数で延長した区間は破線にする。実際の査定に基づく区間と見分けが付かないと
  // 「目安」を実測として読んでしまう。
  const firstEstimated = priceHistory.findIndex((p) => p.estimated);
  if (firstEstimated > 0) {
    datasets[0].segment = {
      borderDash: (ctx) =>
        priceHistory[ctx.p1DataIndex] && priceHistory[ctx.p1DataIndex].estimated
          ? [6, 4]
          : undefined,
    };
  }
  // 平均取得単価の水平破線
  if (avgCost != null && avgCost !== "") {
    datasets.push({
      label: "avgCost",
      data: labels.map(() => Number(avgCost)),
      borderColor: "#e3b341",
      borderDash: [6, 4],
      fill: false,
      pointRadius: 0,
      pointHoverRadius: 0,
      borderWidth: 1.5,
    });
  }

  _priceChart = new Chart(canvas, {
    type: "line",
    data: { labels, datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      scales: {
        x: {
          ticks: {
            color: th.tick,
            font: { size: 11 },
            maxTicksLimit: narrow ? 4 : 8,
            maxRotation: 0,
            callback(v) {
              const label = String(this.getLabelForValue(v));
              return narrow ? label.replace(/^\d{4}-/, "") : label;
            },
          },
          grid: { color: th.grid },
          border: { display: false },
        },
        y: {
          ticks: {
            color: th.tick,
            font: { size: 11 },
            callback(v) { return fmtPrice(v, currency); },
          },
          grid: { color: th.grid },
          border: { display: false },
        },
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: th.tooltipBg,
          borderColor: th.tooltipBorder,
          borderWidth: 1,
          titleColor: th.tooltipTitle,
          bodyColor: th.tooltipBody,
          padding: 10,
          filter: (item) => item.datasetIndex === 0,
          callbacks: {
            title: ([item]) => (item ? item.label : ""),
            label: (item) => "  " + fmtPrice(item.parsed.y, currency),
          },
        },
      },
    },
  });
}

function priceSourceHtml(sec) {
  const status = sec.price_source_status;
  if (status === "linked") {
    return `<span>${escapeHtml(sec.price_source_type || "")}: <code>${escapeHtml(sec.price_source_ref || "")}</code></span>`;
  }
  if (status === "unlinked") {
    return `<a href="#settings" class="unlinked-warn">${t("label.unlinked")}</a>`;
  }
  if (status === "manual") {
    const ref = sec.price_source_ref || "";
    if (ref.startsWith("re_index:")) {
      const [region, type] = ref.slice("re_index:".length).split(":");
      const label = reIndexLabel(region, type);
      return `<span>${t("label.sourceManual")} ＋ ${escapeHtml(label)}</span>`;
    }
    return `<span>${t("label.sourceManual")}</span>`;
  }
  if (status === "not_required") {
    return `<span>${t("label.notRequired")}</span>`;
  }
  return `<span>${t("label.sourceNone")}</span>`;
}

function reIndexLabel(region, type) {
  const opts = _reIndexOptions;
  const find = (list, code) =>
    (list || []).find((x) => x.code === code)?.label || code || "";
  if (!opts) return `${region || ""}・${type || ""}`;
  return `${find(opts.regions, region)}・${find(opts.types, type)}`;
}

async function showSecurityDetail(id, range) {
  // 価格ソース行で地域・種別を日本語で出すために語彙を確保する（1回だけ取得）
  await ensureReIndexOptions();
  document.getElementById("holdings-list-view").classList.add("hidden");
  document.getElementById("security-detail-view").classList.remove("hidden");

  if (_secDetailId !== id) _secRange = localStorage.getItem("as_sec_range") || "1y";
  if (range != null) _secRange = range;
  _secDetailId = id;
  localStorage.setItem("as_sec_range", _secRange);
  _setRangeActive("sec-range-tabs", _secRange);

  const currency = currentCurrency();
  const loading = document.getElementById("security-detail-loading");
  loading.classList.remove("hidden");
  try {
    const data = await fetchJSON(
      `/api/security/${encodeURIComponent(id)}?currency=${currency}&range=${_secRange}`
    );
    const sec = data.security || {};

    document.getElementById("security-detail-name").textContent = sec.name || "";
    const meta = document.getElementById("security-detail-meta");
    const parts = [];
    if (sec.code) parts.push(`<code>${escapeHtml(sec.code)}</code>`);
    parts.push(classBadgeHtml(sec.asset_class));
    parts.push(priceSourceHtml(sec));
    meta.innerHTML = parts.join(" ");

    // 統計タイル
    const tiles = data.tiles || {};
    const secCur = sec.currency || currency;
    document.getElementById("tile-quantity").textContent = fmtAmount(tiles.quantity);
    document.getElementById("tile-avg-cost").textContent = fmtPrice(tiles.avg_cost, secCur);
    document.getElementById("tile-price").textContent = fmtPrice(tiles.price, secCur);
    document.getElementById("tile-value").innerHTML =
      escapeHtml(fmtMoney(tiles.value, currency)) +
      (tiles.estimated
        ? ` <span class="ref-badge" title="${escapeHtml(t("label.estimatedTitle"))}">${t("label.estimated")}</span>`
        : "");
    document.getElementById("tile-day-change").innerHTML = dayChangeCellHtml(tiles, currency);
    document.getElementById("tile-pl").innerHTML = plAmountHtml(tiles.pl, currency);
    document.getElementById("tile-pl-pct").innerHTML = plPctHtml(tiles.pl_pct);

    // 価格チャート（平均取得単価の水平破線つき）
    renderPriceChart(data.price_history || [], tiles.avg_cost, secCur);

    // 警告
    renderWarningsInto(document.getElementById("security-warnings"), data.warnings, "");

    // 口座別内訳
    const atbody = document.querySelector("#security-accounts-table tbody");
    atbody.innerHTML = "";
    (data.accounts || []).forEach((a) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${escapeHtml(a.account)}</td>
        <td class="num">${fmtAmount(a.quantity)}</td>
        <td class="num">${fmtPrice(a.avg_cost, secCur)}</td>
        <td class="num">${fmtMoney(a.value, currency)}</td>
        <td class="num">${dayChangeCellHtml(a, currency)}</td>
        <td class="num">${plAmountHtml(a.pl, currency)}</td>
        <td class="num">${plPctHtml(a.pl_pct)}</td>
      `;
      atbody.appendChild(tr);
    });
    if ((data.accounts || []).length === 0) {
      atbody.innerHTML = `<tr><td colspan="7" class="muted">${t("label.noData")}</td></tr>`;
    }

    // ロット一覧
    const ltbody = document.querySelector("#security-lots-table tbody");
    ltbody.innerHTML = "";
    (data.lots || []).forEach((l) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${escapeHtml(l.account)}</td>
        <td>${l.lot_seq != null ? l.lot_seq : ""}</td>
        <td>${escapeHtml(l.lot_label || "")}</td>
        <td class="num">${fmtAmount(l.quantity)}</td>
        <td class="num">${fmtPrice(l.avg_cost, secCur)}</td>
        <td>${escapeHtml(l.acquired_on || "—")}</td>
        <td>${escapeHtml(l.as_of || "")}</td>
      `;
      ltbody.appendChild(tr);
    });
    if ((data.lots || []).length === 0) {
      ltbody.innerHTML = `<tr><td colspan="7" class="muted">${t("label.noData")}</td></tr>`;
    }

    renderCostBasisCard(data, secCur, currency);
    await loadTransactionHistory(id, secCur, currency);
  } catch (e) {
    document.getElementById("security-detail-name").textContent = t("status.error") + e.message;
  } finally {
    loading.classList.add("hidden");
  }
}

// ---- 削除確認ダイアログ（汎用） ----

let _pendingDeleteAction = null;

function openConfirmDialog(title, msg, action, confirmLabel) {
  _pendingDeleteAction = action;
  document.getElementById("delete-dialog-title").textContent = title;
  document.getElementById("delete-dialog-msg").textContent = msg;
  // 既定は「削除」。連携解除など削除以外の確認にも使うためラベルを差し替え可能に
  document.getElementById("delete-confirm").textContent = confirmLabel || t("btn.delete");
  document.getElementById("delete-dialog").classList.remove("hidden");
}

document.getElementById("delete-confirm").addEventListener("click", async () => {
  document.getElementById("delete-dialog").classList.add("hidden");
  const action = _pendingDeleteAction;
  _pendingDeleteAction = null;
  if (!action) return;
  try {
    await action();
  } catch (e) {
    alert(t("status.deleteFail", { error: e.message }));
  }
});

document.getElementById("delete-cancel").addEventListener("click", () => {
  _pendingDeleteAction = null;
  document.getElementById("delete-dialog").classList.add("hidden");
});

document.getElementById("delete-dialog").addEventListener("click", (e) => {
  if (e.target === e.currentTarget) {
    _pendingDeleteAction = null;
    e.currentTarget.classList.add("hidden");
  }
});

// ---- PDF取込ページ ----

async function loadImportPage() {
  const asOf = document.getElementById("import-as-of");
  if (!asOf.value) asOf.value = todayISO();
  loadImportHistory();
  loadInboxStatus();
  // 取引履歴タブの口座入力（datalist）と銘柄ピッカーに使う
  try {
    await Promise.all([_loadAccountsCache(), _loadSecuritiesCache()]);
    _syncAccountDatalist();
  } catch (e) {
    /* 取込タブの補完が効かないだけなので、ページ自体は表示する */
  }
}

function fileToBase64(file) {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => {
      const s = String(r.result);
      resolve(s.slice(s.indexOf(",") + 1));
    };
    r.onerror = () => reject(new Error(r.error ? String(r.error) : "file read error"));
    r.readAsDataURL(file);
  });
}

function diffStatusInfo(status) {
  const s = String(status || "");
  if (s === "new") return { label: t("diff.new"), cls: "diff-new" };
  if (s === "qty_changed" || s === "quantity_changed") return { label: t("diff.qtyChanged"), cls: "diff-changed" };
  if (s === "cost_changed" || s === "avg_cost_changed" || s === "price_changed") return { label: t("diff.costChanged"), cls: "diff-changed" };
  if (s === "changed") return { label: t("diff.qtyChanged"), cls: "diff-changed" };
  if (s === "unchanged") return { label: t("diff.unchanged"), cls: "diff-unchanged" };
  if (s === "missing" || s === "gone" || s === "zero") return { label: t("diff.missing"), cls: "diff-missing" };
  return { label: s || "?", cls: "diff-unchanged" };
}

document.getElementById("import-parse-btn").addEventListener("click", async () => {
  hideResult("import-upload-result");
  hideResult("import-commit-result");
  const fileInput = document.getElementById("import-file");
  const file = fileInput.files && fileInput.files[0];
  if (!file) {
    showResult("import-upload-result", false, t("import.noFile"));
    return;
  }
  const btn = document.getElementById("import-parse-btn");
  btn.disabled = true;
  const origText = btn.textContent;
  btn.textContent = t("import.parsing");
  try {
    const content_b64 = await fileToBase64(file);
    const data = await apiCall("/api/import/pdf", "POST", {
      filename: file.name,
      content_b64,
    });
    _importPreview = data;
    if (data.suggested_as_of) {
      document.getElementById("import-as-of").value = data.suggested_as_of;
    }
    renderImportPreview(data);
  } catch (e) {
    if (e.status === 409 && e.data && e.data.existing_batch_id) {
      showResult("import-upload-result", false,
        t("import.duplicate", { id: e.data.existing_batch_id }));
    } else {
      showResult("import-upload-result", false, t("import.parseFail", { error: e.message }));
    }
  } finally {
    btn.disabled = false;
    btn.textContent = origText;
  }
});

function renderImportPreview(data) {
  const wrap = document.getElementById("import-preview");
  wrap.classList.remove("hidden");

  // 検算レポート要約
  const rep = data.report || {};
  const repEl = document.getElementById("import-report-summary");
  const repParts = [];
  repParts.push(rep.grand_total_ok
    ? `<span class="ok">${t("import.grandTotalOk")}</span>`
    : `<span class="ng">${t("import.grandTotalNg")}</span>`);
  // build_preview は report.warnings をそのまま data.warnings にも入れて返すため、
  // 素直に両方並べると全部が二重に出る。重複は落として1回だけ出す。
  const seenWarn = new Set();
  [...(rep.warnings || []), ...(data.warnings || [])].forEach((w) => {
    if (seenWarn.has(w)) return;
    seenWarn.add(w);
    repParts.push("⚠ " + escapeHtml(w));
  });
  // 「解析できなかった行が N 件」だけでは直しようがないので、実際の行を出す。
  // この行の金額は集計に入っていないので、検算NGの原因がここで突き合わせられる。
  const unparsed = rep.unparsed_lines || [];
  if (unparsed.length) {
    const items = unparsed.map((u) =>
      `<li><span class="unparsed-page">p${u.page}</span> ${escapeHtml(u.text)}</li>`
    ).join("");
    repParts.push(
      `<details class="import-unparsed">` +
      `<summary>${escapeHtml(t("import.unparsedShow"))}</summary>` +
      `<p class="unparsed-hint">${escapeHtml(t("import.unparsedHint"))}</p>` +
      `<ul>${items}</ul></details>`
    );
  }
  repEl.innerHTML = repParts.join("<br>");

  // セクションのNG情報（section名 → ok）
  const sectionOk = {};
  (rep.sections || []).forEach((s) => { sectionOk[s.name] = s.ok; });

  // セクションチップ
  const chipWrap = document.getElementById("import-section-chips");
  chipWrap.innerHTML = "";
  (data.sections || []).forEach((s) => {
    const label = document.createElement("label");
    const isCrypto = s.section === "crypto";
    label.className = "section-chip" + (s.default_include === false ? " chip-off" : "");
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = s.default_include !== false;
    cb.dataset.section = s.section;
    label.appendChild(cb);
    const txt = document.createElement("span");
    txt.innerHTML = `${escapeHtml(s.label || s.section)} <span class="chip-total">${t("import.itemCount", { count: s.count })}・${fmtMoney(s.total, "JPY")}</span>` +
      (isCrypto ? ` <span class="chip-note">${t("import.cryptoNote")}</span>` : "") +
      (sectionOk[s.label] === false || sectionOk[s.section] === false ? ` <span class="chip-note">${t("import.sectionNg")}</span>` : "");
    label.appendChild(txt);
    cb.addEventListener("change", () => {
      label.classList.toggle("chip-off", !cb.checked);
      _syncDiffRowState();
    });
    chipWrap.appendChild(label);
  });

  // diff テーブル
  const tbody = document.querySelector("#import-diff-table tbody");
  tbody.innerHTML = "";
  (data.diff || []).forEach((d) => {
    const info = diffStatusInfo(d.status);
    const isMissing = info.cls === "diff-missing";
    const tr = document.createElement("tr");
    tr.className = info.cls;
    tr.dataset.section = d.section || "";
    tr.dataset.key = d.key;
    tr.dataset.missing = isMissing ? "1" : "0";

    const qtyCell = (d.old_quantity != null && d.new_quantity != null &&
        String(d.old_quantity) !== String(d.new_quantity))
      ? `<span class="diff-old">${fmtAmount(d.old_quantity)}</span><span class="diff-arrow">→</span>${fmtAmount(d.new_quantity)}`
      : fmtAmount(d.new_quantity != null ? d.new_quantity : d.old_quantity);
    const costCell = (d.old_avg_cost != null && d.new_avg_cost != null &&
        String(d.old_avg_cost) !== String(d.new_avg_cost))
      ? `<span class="diff-old">${fmtAmount(d.old_avg_cost)}</span><span class="diff-arrow">→</span>${fmtAmount(d.new_avg_cost)}`
      : fmtAmount(d.new_avg_cost != null ? d.new_avg_cost : d.old_avg_cost);

    const conf = Number(d.confidence);
    const confHtml = isFinite(conf) && conf < 0.9
      ? ` <span class="confidence-warn">${t("import.lowConfidence", { pct: Math.round(conf * 100) })}</span>`
      : "";
    const lotHtml = d.lot_label ? ` <span class="asset-code">${escapeHtml(d.lot_label)}</span>` : "";
    const codeHtml = d.code ? ` <span class="asset-code">${escapeHtml(d.code)}</span>` : "";

    const checkCell = isMissing
      ? `<label class="zero-label"><input type="checkbox" class="diff-check" ${d.included !== false ? "checked" : ""}> ${t("import.zeroOut")}</label>`
      : `<input type="checkbox" class="diff-check" ${d.included !== false ? "checked" : ""}>`;

    tr.innerHTML = `
      <td>${checkCell}</td>
      <td><span class="diff-status">${info.label}</span></td>
      <td>${escapeHtml(d.name || "")}${codeHtml}${lotHtml}${confHtml}</td>
      <td>${escapeHtml(d.account || "")}</td>
      <td class="num">${qtyCell}</td>
      <td class="num">${costCell}</td>
      <td class="num">${fmtMoney(d.value, "JPY")}</td>
    `;
    tbody.appendChild(tr);
  });
  _syncDiffRowState();
}

// セクションチェック状態に応じて行をグレーアウト
function _syncDiffRowState() {
  const included = _includedSections();
  document.querySelectorAll("#import-diff-table tbody tr").forEach((tr) => {
    const on = included.has(tr.dataset.section);
    tr.classList.toggle("diff-excluded", !on);
    const cb = tr.querySelector(".diff-check");
    if (cb) cb.disabled = !on;
  });
}

function _includedSections() {
  const set = new Set();
  document.querySelectorAll("#import-section-chips input[type=checkbox]").forEach((cb) => {
    if (cb.checked) set.add(cb.dataset.section);
  });
  return set;
}

document.getElementById("import-cancel-btn").addEventListener("click", () => {
  _importPreview = null;
  document.getElementById("import-preview").classList.add("hidden");
});

document.getElementById("tx-parse-btn").addEventListener("click", async () => {
  hideResult("tx-upload-result");
  hideResult("tx-commit-result");
  await parseTxFile();
});

document.getElementById("tx-remap-btn").addEventListener("click", async () => {
  hideResult("tx-upload-result");
  await remapTxPreview();
});

document.getElementById("tx-commit-btn").addEventListener("click", async () => {
  hideResult("tx-commit-result");
  await commitTxBatch();
});

document.getElementById("tx-cancel-btn").addEventListener("click", () => {
  resetTxForm();
});

document.getElementById("tx-bulk-sold").addEventListener("click", () => {
  applyTxBulkChoice(TX_NEW_SECURITY, { onlyUnselected: true, soldOutOnly: true });
});

document.getElementById("tx-bulk-reset").addEventListener("click", () => {
  applyTxBulkChoice("", { onlyUnselected: false });
});

document.getElementById("tx-type-bulk-apply").addEventListener("click", () => {
  applyTxTypeBulk();
});

/** 取込直後の投信自動連携の結果。連携できなかったものは理由まで出す
 *  （投信協会へ届かなかったのか、該当が無かったのかで対処が変わるため）。 */
function autolinkNote(autolink, warnings) {
  const lines = [];
  (warnings || []).forEach((w) => lines.push("⚠ " + escapeHtml(w)));
  if (!autolink || !autolink.attempted) {
    return lines.length ? "<br>" + lines.join("<br>") : "";
  }
  const linked = (autolink.linked || []).length;
  if (linked) {
    lines.push(escapeHtml(t("import.autolinkLinked", { count: linked })));
    const merged = (autolink.merged || []).length;
    if (merged) lines.push(escapeHtml(t("import.autolinkMerged", { count: merged })));
  }
  const unresolved = autolink.unresolved || [];
  if (unresolved.length) {
    lines.push("⚠ " + escapeHtml(t("import.autolinkUnresolved", { count: unresolved.length })));
    unresolved.forEach((u) => {
      const key = "import.autolinkReason." + (u.reason || "");
      const why = t(key) === key ? (u.status || "") : t(key);
      lines.push("　・" + escapeHtml(u.name) + " — " + escapeHtml(why));
    });
  }
  return lines.length ? "<br>" + lines.join("<br>") : "";
}

document.getElementById("import-commit-btn").addEventListener("click", async () => {
  hideResult("import-commit-result");
  if (!_importPreview) return;
  const asOf = document.getElementById("import-as-of").value;
  if (!asOf) {
    showResult("import-commit-result", false, t("import.noAsOf"));
    return;
  }
  const includedSet = _includedSections();
  const includeCrypto = includedSet.has("crypto");
  const includeSections = [...includedSet].filter((s) => s !== "crypto");

  const excludeKeys = [];
  const zeroKeys = [];
  document.querySelectorAll("#import-diff-table tbody tr").forEach((tr) => {
    if (!includedSet.has(tr.dataset.section)) return;
    const cb = tr.querySelector(".diff-check");
    if (!cb) return;
    if (tr.dataset.missing === "1") {
      if (cb.checked) zeroKeys.push(tr.dataset.key);
    } else if (!cb.checked) {
      excludeKeys.push(tr.dataset.key);
    }
  });

  const btn = document.getElementById("import-commit-btn");
  btn.disabled = true;
  const origText = btn.textContent;
  btn.textContent = t("import.committing");
  try {
    const d = await apiCall(`/api/import/${encodeURIComponent(_importPreview.batch_id)}/commit`, "POST", {
      as_of: asOf,
      include_sections: includeSections,
      exclude_keys: excludeKeys,
      zero_keys: zeroKeys,
      include_crypto: includeCrypto,
    });
    const done = t("import.commitDone", {
      created: d.created, updated: d.updated, zeroed: d.zeroed,
      date: d.snapshot_date || asOf,
    });
    showResult("import-commit-result", true, done + autolinkNote(d.autolink, d.warnings));
    _importPreview = null;
    document.getElementById("import-preview").classList.add("hidden");
    document.getElementById("import-file").value = "";
    // 完了バナーはアップロードカード側にも出す
    showResult("import-upload-result", true, done + autolinkNote(d.autolink, d.warnings));
    loadImportHistory();
  } catch (e) {
    showResult("import-commit-result", false, t("import.commitFail", { error: e.message }));
  } finally {
    btn.disabled = false;
    btn.textContent = origText;
  }
});

async function loadImportHistory() {
  const tbody = document.querySelector("#import-history-table tbody");
  tbody.innerHTML = `<tr><td colspan="5" class="loading">${t("label.loading")}</td></tr>`;
  try {
    const data = await fetchJSON("/api/import/history");
    const imports = data.imports || [];
    tbody.innerHTML = "";
    imports.forEach((b) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td style="white-space:nowrap">${escapeHtml(fmtDate(b.created_at))}</td>
        <td>${escapeHtml(b.filename || "")}</td>
        <td>${escapeHtml(b.as_of_date || "")}</td>
        <td class="num">${b.row_count != null ? b.row_count : ""}</td>
        <td class="num"><button class="delete-btn" title="${t("btn.delete")}">✕ ${t("btn.delete")}</button></td>
      `;
      tr.querySelector(".delete-btn").addEventListener("click", () => {
        openConfirmDialog(
          t("import.deleteConfirmTitle"),
          t("import.deleteConfirmMsg", { name: b.filename || b.id, count: b.row_count }),
          async () => {
            await apiCall(`/api/import/batches/${encodeURIComponent(b.id)}`, "DELETE");
            loadImportHistory();
          }
        );
      });
      tbody.appendChild(tr);
    });
    if (imports.length === 0) {
      tbody.innerHTML = `<tr><td colspan="5" class="muted">${t("import.noHistory")}</td></tr>`;
    }
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="5" class="muted">${t("status.error")}${escapeHtml(e.message)}</td></tr>`;
  }
}

// ---- 取引履歴の取込（証券会社CSV / Excel / 貼り付け） ----
//
// 書式は自動判定するが、判定結果は必ず見せて直せるようにする。
// ヒューリスティックを黙って確定させないのが要点。

const TX_FIELDS = [
  "_", "trade_date", "settle_date", "security_name", "security_code", "tx_type",
  "quantity", "unit_price", "gross_amount", "net_amount", "fee", "tax",
  "account", "account_type", "currency", "exchange_rate", "note",
];

const TX_NEW_SECURITY = "__new__";   // 売却済みとして新規登録する印

let _txPreview = null;      // 直近のプレビュー応答
let _txSecurityMap = {};    // ファイル上の銘柄名 → security_id
let _txNewSecurities = new Set();   // 売却済みとして登録する銘柄名
let _txAutoLinked = new Set();      // 価格の裏取りで自動的に結びついた銘柄名
let _txTypeOverrides = {};          // dedup_key -> 利用者が指定した取引区分

// 取込対象の判定に使う値。判定エンジン（contracts.py）から payload で受け取る。
// 画面に直書きすると、しきい値を変えたときに片方だけ古いまま残る。
function txThreshold(key, fallback) {
  const th = (_txPreview && _txPreview.thresholds) || {};
  return typeof th[key] === "number" ? th[key] : fallback;
}
let _txHistoryOffset = 0;

function txTypeLabel(kind) {
  return t(`tx.type.${kind}`) || kind;
}

function coverageLabel(coverage) {
  const map = {
    full: "tx.coverageFull",
    partial: "tx.coveragePartial",
    partial_uncosted: "tx.coveragePartialUncosted",
    unreconciled: "tx.coverageUnreconciled",
  };
  return coverage ? t(map[coverage] || coverage) : "—";
}

function coverageBadgeHtml(coverage) {
  if (!coverage) return "—";
  const tone = coverage === "full" ? "ok" : coverage === "unreconciled" ? "ng" : "warn";
  return `<span class="chip chip-${tone}">${escapeHtml(coverageLabel(coverage))}</span>`;
}

document.querySelectorAll(".source-tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".source-tab").forEach((b) =>
      b.classList.toggle("active", b === btn));
    ["pdf", "tx"].forEach((name) => {
      const el = document.getElementById(`import-tab-${name}`);
      if (el) el.classList.toggle("hidden", name !== btn.dataset.source);
    });
  });
});

async function parseTxFile() {
  const account = document.getElementById("tx-account").value.trim();
  const file = document.getElementById("tx-file").files[0];
  const pasted = document.getElementById("tx-paste").value.trim();
  if (!account) return showResult("tx-upload-result", false, t("tx.noAccount"));
  if (!file && !pasted) return showResult("tx-upload-result", false, t("tx.noInput"));

  const body = { account_name: account };
  if (file) {
    body.filename = file.name;
    body.content_b64 = await fileToBase64(file);
  } else {
    body.filename = "paste";
    body.text = pasted;
  }
  try {
    _txSecurityMap = {};
    _txNewSecurities = new Set();
    _txAutoLinked = new Set();
    _txTypeOverrides = {};
    const data = await apiCall("/api/import/table", "POST", body);
    renderTxPreview(data);
    showResult("tx-upload-result", true, t("tx.parsed", {
      count: data.rows.length,
      confidence: Math.round((data.detection.confidence || 0) * 100),
    }));
  } catch (e) {
    if (e.status === 409) {
      showResult("tx-upload-result", false,
        t("tx.duplicate", { id: (e.data && e.data.existing_batch_id) || "" }));
    } else {
      showResult("tx-upload-result", false, t("tx.commitFail", { error: e.message }));
    }
  }
}

function renderTxPreview(data) {
  _txPreview = data;
  renderTxMapping(data.detection);
  renderTxUnmatched(data.unmatched_securities || [], data.auto_linked_securities || []);
  renderTxRows(data.rows || []);   // 銘柄の紐付け反映後の状態で描く
  document.getElementById("tx-mapping-card").classList.remove("hidden");
  document.getElementById("tx-preview").classList.remove("hidden");
  // 警告の大半（結びつかない銘柄・残高のある銘柄）は「銘柄の結びつけ」への
  // 指示なので、作業する場所に出す。結びつけの対象が無いときだけ
  // 取引プレビュー側に出す（口座未指定などが該当）。
  const hasLinking = ((data.unmatched_securities || []).length
    + (data.auto_linked_securities || []).length) > 0;
  const noticeTarget = hasLinking ? "tx-link-notices" : "tx-preview-summary";
  const noticeOther = hasLinking ? "tx-preview-summary" : "tx-link-notices";
  renderTxNotices(document.getElementById(noticeTarget), data);
  const otherEl = document.getElementById(noticeOther);
  if (otherEl) { otherEl.innerHTML = ""; otherEl.classList.add("hidden"); }
}

function renderTxMapping(detection) {
  const summary = document.getElementById("tx-detection-summary");
  const source = (_txPreview && _txPreview.source) || {};
  const encoding = source.encoding || (source.kind === "xlsx" ? "Excel" : "—");
  const delimiter = source.delimiter_mode === "multispace"
    ? t("tx.paste")
    : ({ ",": ",", "\t": "TAB", ";": ";", "|": "|" }[source.delimiter] || "—");
  const parts = [];
  parts.push(detection.header_row == null
    ? t("tx.detectedNoHeader", { encoding, delimiter })
    : t("tx.detected", { encoding, delimiter, header: detection.header_row + 1 }));
  if (detection.divisor === 10000) parts.push(t("tx.divisorFund"));
  (detection.identities || []).forEach((i) => {
    if (i.tested) parts.push(t("tx.identityOk", {
      name: i.name, passed: i.passed, tested: i.tested,
    }));
  });
  summary.textContent = parts.join(" ／ ");

  const tbody = document.querySelector("#tx-mapping-table tbody");
  tbody.innerHTML = "";
  (detection.columns || []).forEach((col) => {
    const samples = txSampleValues(col.index).slice(0, 3).join(" / ");
    const options = TX_FIELDS.map((f) =>
      `<option value="${f}"${f === col.field ? " selected" : ""}>${escapeHtml(t("tx.field." + f))}</option>`
    ).join("");
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${escapeHtml(col.header || `#${col.index + 1}`)}</td>
      <td class="muted">${escapeHtml(samples)}</td>
      <td><select class="settings-input tx-field-select" data-col="${col.index}">${options}</select></td>
      <td class="muted">${escapeHtml((col.evidence || []).join(" / "))}</td>
    `;
    tbody.appendChild(tr);
  });
}

function txSampleValues(index) {
  // プレビュー行から、その列に対応する値を拾って例示する
  const rows = (_txPreview && _txPreview.rows) || [];
  const detection = (_txPreview && _txPreview.detection) || {};
  const col = (detection.columns || []).find((c) => c.index === index);
  if (!col || col.field === "_") return [];
  const key = {
    trade_date: "trade_date", settle_date: "settle_date",
    security_name: "security_name", security_code: "security_code",
    tx_type: "tx_type", quantity: "quantity", unit_price: "unit_price",
    gross_amount: "gross_amount", net_amount: "net_amount",
    fee: "fee", tax: "tax", account_type: "lot_label", note: "note",
    currency: "currency",
  }[col.field];
  if (!key) return [];
  return rows.map((r) => (r[key] == null ? "" : String(r[key]))).filter(Boolean);
}

// 警告を「判断が要るもの」と「何をしたかの報告」に分けて描く。
// 全部を ⚠ で並べると、対処の要る 2 件が報告 4 件の中に埋もれる。
// 報告は畳んでおき、開けば読める。
function renderTxNotices(el, data) {
  if (!el) return;
  const notices = data.notices
    || (data.warnings || []).map((w) => ({ level: "action", text: w }));
  const actions = notices.filter((n) => n.level === "action");
  const infos = notices.filter((n) => n.level !== "action");
  let html = "";
  if (actions.length) {
    html += actions.map((n) =>
      `<div class="tx-notice-action">⚠ ${escapeHtml(n.text)}</div>`).join("");
  } else {
    html += `<div class="tx-notice-ok">✓ ${escapeHtml(t("tx.noActionNeeded"))}</div>`;
  }
  if (infos.length) {
    html += `<details class="tx-notice-info"><summary>${
      escapeHtml(t("tx.infoNotices", { n: infos.length }))}</summary>`
      + infos.map((n) => `<div>${escapeHtml(n.text)}</div>`).join("")
      + `</details>`;
  }
  el.classList.remove("hidden");
  el.innerHTML = html;
}

function renderTxUnmatched(unmatched, autoLinked) {
  const card = document.getElementById("tx-unmatched-card");
  const tbody = document.querySelector("#tx-unmatched-table tbody");
  tbody.innerHTML = "";
  // 自動で結びついた分も同じ表に出す。別の場所に隠すと、何がどう決まったのかを
  // 確かめるのに二か所を見ることになる。既に選ばれた状態で並ぶだけで、
  // 選び直しも解除も残りと同じ操作でできる。
  const entries = (autoLinked || []).concat(unmatched || []);
  if (!entries.length) {
    card.classList.add("hidden");
    return;
  }
  card.classList.remove("hidden");
  _txAutoLinked = new Set((autoLinked || []).map((u) => u.name));
  renderTxUnexplained();
  const securities = _securities || [];
  entries.forEach((u) => {
    // 候補は★付きで上に出すが、**価格で裏の取れたもの以外は既定で選ばない**。
    // 名前が似ているだけの別銘柄（「架空商事」と「架空撤退商事」）を自動で
    // 選ぶと、気づかないまま別の銘柄に合算されてしまう。
    const linked = u.auto_linked || null;
    const sug = new Map((u.suggestions || []).map((s) => [s.security_id, s]));
    // CSV が説明できていない保有を先頭に置く。名前が略されていて候補にすら
    // 出ない銘柄（略称と正式名称で類似度が伸びない組など）は、ここからしか選べない。
    const openIds = new Set(((_txPreview && _txPreview.unexplained_holdings) || [])
      .filter((h) => !h.claimed_by).map((h) => h.security_id));
    const rank = { match: 0, partial: 1, unknown: 2, mismatch: 3 };
    const ranked = securities.slice().sort((a, b) => {
      const oa = openIds.has(a.id), ob = openIds.has(b.id);
      if (oa !== ob) return oa ? -1 : 1;
      const sa = sug.get(a.id), sb = sug.get(b.id);
      if (!sa && !sb) return 0;
      if (!sa) return 1;
      if (!sb) return -1;
      const d = rank[sa.price_verdict] - rank[sb.price_verdict];
      return d !== 0 ? d : sb.score - sa.score;
    });
    const liveSuggestions = (u.suggestions || [])
      .filter((s) => s.price_verdict !== "mismatch" && !s.category_conflict
                     && !s.code_conflict && !s.name_refuted);
    const chosen = linked ? String(linked.security_id) : "";
    const options = [
      `<option value=""${chosen ? "" : " selected"}>${escapeHtml(t("tx.skipSecurity"))}</option>`,
      `<option value="${TX_NEW_SECURITY}">${escapeHtml(t("tx.registerSold"))}</option>`,
    ]
      .concat(ranked.map((s) => {
        const hit = sug.get(s.id);
        // 価格の裏取り結果を添える。名前の類似度より強い証拠なので前に出す。
        let mark = openIds.has(s.id) ? `◎ ${t("tx.unexplainedMark")} ` : "";
        if (hit) {
          if (hit.price_verdict === "match") {
            mark = `✓ ${t("tx.priceMatch", { n: hit.price_matched })} `;
          } else if (hit.price_verdict === "mismatch") {
            mark = `✕ ${t("tx.priceMismatch")} `;
          } else if (hit.price_verdict === "partial") {
            mark = `△ ${t("tx.pricePartial", { n: hit.price_matched, of: hit.price_checked })} `;
          } else {
            mark = `★ ${Math.round(hit.score * 100)}% `;
          }
        }
        const sel = String(s.id) === chosen ? " selected" : "";
        const held = _txHeldQty(s.id) !== undefined ? t("tx.heldOptionMark") : "";
        return `<option value="${s.id}"${sel}>${escapeHtml(mark + s.name + (s.code ? ` (${s.code})` : "") + held)}</option>`;
      }))
      .join("");
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${escapeHtml(u.name)}${(u.aliases || []).length
        ? `<div class="muted">${escapeHtml(t("tx.aliasesLabel") + u.aliases.join(" / "))}</div>` : ""}</td>
      <td class="num">${u.count}</td>
      <td class="num">${_txBalanceCell(u)}</td>
      <td>${_txEvidenceCell(u)}</td>
      <td class="tx-held-cell"></td>
      <td><select class="settings-input tx-link-select" data-name="${escapeHtml(u.name)}" data-has-candidate="${liveSuggestions.length ? "1" : ""}" data-closed-out="${u.closed_out ? "1" : ""}">${options}</select></td>
    `;
    tbody.appendChild(tr);
    const select = tr.querySelector("select");
    const heldCell = tr.querySelector(".tx-held-cell");
    heldCell.innerHTML = _txHeldCellHtml(select.value);
    _rememberTxChoice(u.name, select.value);
    select.addEventListener("change", () => {
      heldCell.innerHTML = _txHeldCellHtml(select.value);
      _rememberTxChoice(u.name, select.value);
      // 紐付けたその場で該当行を取込対象に戻す。ここで戻さないと、確定時に
      // 送るチェック済みキーが空のままになり「0件取込」で終わってしまう。
      applyTxSecurityMapLocally();
      renderTxRows((_txPreview && _txPreview.rows) || []);
      updateTxUnmatchedCount();
    });
  });
  // 候補があらかじめ選ばれている分を最初から反映しておく
  applyTxSecurityMapLocally();
  updateTxUnmatchedCount();
}

// ファイルが示す残高。「売り切った銘柄」と「名前が違うだけでまだ持っている
// 銘柄」はここでしか見分けられない。実データでは残高の残る銘柄が候補なしに
// 埋もれており、まとめて売却済み登録すると保有中の銘柄を二重に作っていた。
function _txBalanceCell(entry) {
  if (entry.undetermined > 0) {
    return `<span class="muted" title="${escapeHtml(t("tx.balanceUnknownHelp"))}">${
      escapeHtml(t("tx.balanceUnknown", { n: entry.undetermined }))}</span>`;
  }
  const span = entry.first_date && entry.last_date
    ? `${entry.first_date} 〜 ${entry.last_date}` : "";
  if (entry.closed_out) {
    return `<span class="chip chip-ok" title="${escapeHtml(span)}">${
      escapeHtml(t("tx.balanceClosed"))}</span>`;
  }
  return `<span class="chip chip-warn" title="${escapeHtml(span)}">${
    escapeHtml(fmtAmount(entry.net_quantity))}</span>`;
}

// スナップショット（MF PDF）のうち、CSV のどの行にも結びついていない保有。
// DESIGN の「スナップショットを錨に、CSV で説明できる分を差し引く」を
// 銘柄の照合にも使う。残った保有は CSV のどれかの名前が指しているはずで、
// 数十銘柄から選ぶ代わりにこの数件から選べばよくなる。
function renderTxUnexplained() {
  const box = document.getElementById("tx-unexplained");
  const list = (_txPreview && _txPreview.unexplained_holdings) || [];
  if (!list.length) {
    box.classList.add("hidden");
    return;
  }
  box.classList.remove("hidden");
  const items = list.map((h) => {
    const claimed = h.claimed_by
      ? `<span class="chip chip-ok">${escapeHtml(h.claimed_by)}</span>`
      : `<span class="chip chip-warn">${escapeHtml(t("tx.unclaimed"))}</span>`;
    return `<li>${escapeHtml(h.name)} <span class="muted">${
      escapeHtml(fmtAmount(h.quantity))}</span> ${claimed}</li>`;
  }).join("");
  box.innerHTML = `<div>${escapeHtml(t("tx.unexplainedTitle", { n: list.length }))}</div>`
    + `<ul class="tx-unexplained-list">${items}</ul>`;
}

// その銘柄を今この口座で保有しているか（プレビューの held_quantities から）。
// 「保有中の銘柄に結びつける」のか「売却済みとして登録する」のかは判断が
// 逆方向なので、選択のたびにここで見せる。
function _txHeldQty(securityId) {
  const held = (_txPreview && _txPreview.held_quantities) || {};
  return securityId ? held[String(securityId)] : undefined;
}

function _txHeldCellHtml(selectValue) {
  if (!selectValue || selectValue === TX_NEW_SECURITY) return "";
  const q = _txHeldQty(Number(selectValue));
  if (q !== undefined) {
    return `<span class="chip chip-ok">${escapeHtml(t("tx.heldNow", { q: fmtAmount(q) }))}</span>`;
  }
  return `<span class="muted">${escapeHtml(t("tx.notHeld"))}</span>`;
}

function _txEvidenceCell(entry) {
  if (entry.auto_linked) {
    const a = entry.auto_linked;
    const shifted = a.price_shifted ? ` ${t("tx.priceShifted")}` : "";
    // 価格だけでは決まらず口座で絞ったときは、そう書く。同じファンドが証券会社
    // ごとに別々に登録されている場合で、根拠が違う以上まとめて見せない。
    const why = a.account_match ? ` ${t("tx.autoLinkedByAccount")}` : "";
    if (a.code_match) {
      return `<span class="chip chip-ok">${escapeHtml(t("tx.autoLinkedByCode"))}</span>`;
    }
    if (a.quantity_match) {
      // 価格ではなく「未説明の保有と数量が一致した」で決めた場合。
      return `<span class="chip chip-ok">${escapeHtml(
        t("tx.autoLinkedByQuantity", { q: fmtAmount(a.quantity_match) }))}</span>`;
    }
    return `<span class="chip chip-ok">${escapeHtml(
      t("tx.autoLinked", { n: a.price_matched, of: a.price_checked }) + shifted + why)}</span>`;
  }
  if (entry.ambiguous) {
    return `<span class="chip chip-warn">${escapeHtml(
      t("tx.ambiguous", { n: entry.ambiguous.length }))}</span>`;
  }
  const sugs = entry.suggestions || [];
  if (!sugs.length) return `<span class="muted">${escapeHtml(t("tx.noCandidate"))}</span>`;
  if (sugs.every((s) => s.price_verdict === "mismatch" || s.category_conflict
                        || s.code_conflict || s.name_refuted)) {
    // 否定済み（価格不一致 or 資産クラス・地域違い）。候補として見せる意味は無い。
    return `<span class="muted">${escapeHtml(t("tx.refutedCandidates"))}</span>`;
  }
  const top = sugs[0];
  return `<span class="muted">${escapeHtml(
    t("tx.topCandidate", { pct: Math.round(top.score * 100) }))}</span>`;
}

// 何件決まって何件残っているかを常に出す。長い履歴では百近い銘柄になるので、
// 表を上から順に潰していけるよう残数が見えている必要がある。
function updateTxUnmatchedCount() {
  const el = document.getElementById("tx-unmatched-count");
  if (!el) return;
  const selects = [...document.querySelectorAll("#tx-unmatched-table .tx-link-select")];
  const decided = selects.filter((s) => s.value).length;
  el.textContent = t("tx.decidedCount", { done: decided, total: selects.length });

  // 「売却済みとして登録」は候補の無いものだけが対象。何件が対象かを
  // ボタンに出しておかないと、押しても何も起きないように見える。
  const target = selects.filter(
    (s) => !s.value && !s.dataset.hasCandidate && s.dataset.closedOut).length;
  const btn = document.getElementById("tx-bulk-sold");
  btn.textContent = t("tx.bulkRegisterSold", { n: target });
  btn.disabled = target === 0;
}

// まとめて決める。onlyUnselected なら既に決まっている行（自動で結びついた分を
// 含む）には触らない — せっかくの選択をボタン 1 つで消さないため。
//
// soldOutOnly は「売却済みとして登録」に付ける安全弁。作ってよいのは
// **ファイルが売り切りを示していて、かつ既存銘柄に候補が無い** ものだけ。
//   - 残高が残っている銘柄は、名前が違うだけでまだ保有している可能性が高い
//     （実データでも、残高の残る銘柄が候補なしに埋もれていた）。
//   - 候補がある銘柄は、その候補が正しければ二重登録になる。
// 「候補が無い」だけを条件にすると前者を巻き込む。売り切りの確認が要る。
function applyTxBulkChoice(value, { onlyUnselected, soldOutOnly }) {
  document.querySelectorAll("#tx-unmatched-table .tx-link-select").forEach((s) => {
    if (onlyUnselected && s.value) return;
    if (soldOutOnly && (s.dataset.hasCandidate || !s.dataset.closedOut)) return;
    s.value = value;
    _rememberTxChoice(s.dataset.name, s.value);
  });
  applyTxSecurityMapLocally();
  renderTxRows((_txPreview && _txPreview.rows) || []);
  updateTxUnmatchedCount();
}

function _rememberTxChoice(name, value) {
  // 同じコードで束ねたエントリは旧称の行にも同じ選択を効かせる
  const entry = [...((_txPreview || {}).auto_linked_securities || []),
                 ...((_txPreview || {}).unmatched_securities || [])]
    .find((e) => e.name === name);
  ((entry || {}).aliases || []).forEach((alias) => _rememberOneTxChoice(alias, value));
  _rememberOneTxChoice(name, value);
}

function _rememberOneTxChoice(name, value) {
  delete _txSecurityMap[name];
  _txNewSecurities.delete(name);
  if (value === TX_NEW_SECURITY) _txNewSecurities.add(name);
  else if (value) _txSecurityMap[name] = Number(value);
  // 自動で結びついた銘柄を「取り込まない」に戻したときは、鍵を消すのではなく
  // 0 を送る。消すだけだと確定時に「指定なし」となり、サーバ側の自動紐付けが
  // そのまま残って解除したつもりが効かない。
  else if (_txAutoLinked.has(name)) _txSecurityMap[name] = 0;
}

function applyTxSecurityMapLocally() {
  if (!_txPreview) return;
  (_txPreview.rows || []).forEach((r) => {
    const mapped = _txSecurityMap[r.security_name];
    const asNew = _txNewSecurities.has(r.security_name);
    if (mapped || asNew) {
      // 新規登録は確定時に作るので、ここでは「取り込む」印だけ付けておく
      r.security_id = mapped || r.security_id;
      r.register_as_new = asNew;
      r.included = !r.duplicate && r.confidence >= txThreshold("include_confidence", 0.7);
    } else if (r.matched_by === "unmatched" || _txAutoLinked.has(r.security_name)) {
      // 自動で結びついた行も、解除されたらその場で取込対象から外す
      r.security_id = null;
      r.register_as_new = false;
      r.included = false;
    }
  });
}

// 取引区分を判別できなかった行をまとめて指定する。
//
// 証券会社は種別欄を空けることがある（マネックスは投信のつみたてで空欄にし、
// 長い履歴では数百行になる）。空欄のままでは保有数も取得原価も動かせないので、
// 取り込んでも意味が無い。かといって買付と決めつけるのは危険で、売却との
// 区別は値からは付かない。1 行ずつ選ばせるのも現実的でないため、件数を
// 見せてまとめて指定してもらう。
// まとめて指定してよいのは **取引区分の欄が空欄だった行** だけ。
// 「その他」には 2 種類ある — 空欄（マネックスは投信のつみたてで空にする）と、
// ラベルはあるが対象外にした行（信用取引など）。後者まで書き換えると、
// 対象外にした信用取引が現物の買付に化けて保有数が壊れる。
// ラベルの有無で見分ける。
function _txTypeBulkTarget(r) {
  return r.tx_type === "other" && !r.cash_only
    && !((r.raw || {}).tx_type_raw) && !((r.raw || {}).margin);
}

function renderTxTypeBulk(rows) {
  const box = document.getElementById("tx-type-bulk");
  const select = document.getElementById("tx-type-bulk-select");
  const undetermined = rows.filter(_txTypeBulkTarget);
  if (!undetermined.length) {
    box.classList.add("hidden");
    return;
  }
  box.classList.remove("hidden");
  document.getElementById("tx-type-bulk-note").textContent =
    t("tx.typeBulkNote", { n: undetermined.length });
  if (!select.options.length) {
    select.innerHTML = ["buy", "sell", "dividend", "reinvest", "transfer_in", "transfer_out"]
      .map((k) => `<option value="${k}">${escapeHtml(txTypeLabel(k))}</option>`).join("");
  }
}

function applyTxTypeBulk() {
  if (!_txPreview) return;
  const kind = document.getElementById("tx-type-bulk-select").value;
  (_txPreview.rows || []).forEach((r) => {
    if (!_txTypeBulkTarget(r)) return;
    _txTypeOverrides[r.dedup_key] = kind;
    r.tx_type = kind;
    // 判別できなかったぶんの減点は、指定してもらった時点で理由が消える。
    // 戻さずに判定すると、指定したのに取込対象に入らないままになる。
    if (r.security_id != null || r.register_as_new) {
      const conf = r.confidence + txThreshold("unknown_type_penalty", 0.2);
      r.included = !r.duplicate && conf >= txThreshold("include_confidence", 0.7);
    }
  });
  renderTxRows(_txPreview.rows || []);
}

function renderTxRows(rows) {
  renderTxTypeBulk(rows);
  const tbody = document.querySelector("#tx-rows-table tbody");
  tbody.innerHTML = "";
  if (!rows.length) {
    tbody.innerHTML = `<tr><td colspan="9" class="muted">${t("label.noData")}</td></tr>`;
    return;
  }
  rows.forEach((r) => {
    let status = t("tx.statusNew");
    // 信用取引は設計上の対象外。「銘柄未確定」「要確認」と出すと対処が要るように
    // 読めるが、実際にやることは無い。専用の状態にして ⚠ からも外す。
    const isMargin = !!(r.raw || {}).margin;
    if (isMargin) status = t("tx.statusMargin");
    else if (r.cash_only) status = t("tx.statusCashOnly");
    else if (r.duplicate) status = t("tx.statusDuplicate");
    else if (r.register_as_new) status = t("tx.statusRegisterSold");
    else if (r.security_id == null) status = t("tx.statusUnmatched");
    else if (!r.included) status = t("tx.statusLowConfidence");
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td><input type="checkbox" class="tx-row-check" data-key="${escapeHtml(r.dedup_key)}"${r.included ? " checked" : ""}${r.duplicate ? " disabled" : ""} /></td>
      <td>${escapeHtml(r.trade_date || "")}</td>
      <td>${escapeHtml(txTypeLabel(r.tx_type))}</td>
      <td>${escapeHtml(r.security_name || "")}</td>
      <td class="num">${fmtAmount(r.quantity)}</td>
      <td class="num">${fmtPrice(r.unit_price, r.currency)}</td>
      <td class="num">${fmtMoney(r.net_amount, r.currency)}</td>
      <td>${escapeHtml(r.lot_label || "")}</td>
      <td>${escapeHtml(status)}${(() => {
        const warns = (r.warnings || []).filter((w) => !(isMargin && w.includes("信用取引")));
        return warns.length ? ` <span class="muted" title="${escapeHtml(warns.join(" / "))}">⚠</span>` : "";
      })()}</td>
    `;
    tbody.appendChild(tr);
  });
}

async function remapTxPreview() {
  if (!_txPreview) return;
  const overrides = {};
  document.querySelectorAll(".tx-field-select").forEach((sel) => {
    if (sel.value !== "_") overrides[sel.dataset.col] = sel.value;
  });
  try {
    const data = await apiCall(
      `/api/import/table/${_txPreview.batch_id}/remap`, "POST",
      {
        column_overrides: overrides,
        security_map: _txSecurityMap,
        account_name: document.getElementById("tx-account").value.trim(),
      });
    renderTxPreview(data);
    showResult("tx-upload-result", true, t("tx.parsed", {
      count: data.rows.length,
      confidence: Math.round((data.detection.confidence || 0) * 100),
    }));
  } catch (e) {
    showResult("tx-upload-result", false, t("tx.commitFail", { error: e.message }));
  }
}

async function commitTxBatch() {
  if (!_txPreview) return;
  const include = [];
  document.querySelectorAll(".tx-row-check").forEach((box) => {
    if (box.checked) include.push(box.dataset.key);
  });
  try {
    const data = await apiCall(
      `/api/import/table/${_txPreview.batch_id}/commit`, "POST",
      {
        account_name: document.getElementById("tx-account").value.trim(),
        include_keys: include,
        security_map: _txSecurityMap,
        new_securities: [..._txNewSecurities],
        type_overrides: _txTypeOverrides,
      });
    showResult("tx-commit-result", true, t("tx.commitDone", {
      inserted: data.inserted,
      skipped: data.skipped_duplicates,
      unmatched: data.skipped_unmatched,
      cash: data.skipped_cash || 0,
    }));
    resetTxForm();
    await loadImportHistory();
  } catch (e) {
    if (e.status === 409) {
      showResult("tx-commit-result", false,
        t("tx.duplicate", { id: (e.data && e.data.existing_batch_id) || "" }));
    } else {
      showResult("tx-commit-result", false, t("tx.commitFail", { error: e.message }));
    }
  }
}

function resetTxForm() {
  _txPreview = null;
  _txSecurityMap = {};
  _txNewSecurities = new Set();
  _txAutoLinked = new Set();
  _txTypeOverrides = {};
  document.getElementById("tx-file").value = "";
  document.getElementById("tx-paste").value = "";
  ["tx-mapping-card", "tx-unmatched-card", "tx-preview"].forEach((id) =>
    document.getElementById(id).classList.add("hidden"));
}

// ---- 銘柄詳細: 取得原価と取引履歴 ----

// 原価計算の修正後、取り込み直さずに再計算だけできる入口。
// API は前からあったが画面から呼べず、修正を反映するには巻き戻して
// 再取込するしかなかった。
document.getElementById("cb-recompute-btn").addEventListener("click", async () => {
  const note = document.getElementById("cb-recompute-result");
  note.textContent = "…";
  try {
    const res = await apiCall("/api/cost-basis/recompute", "POST", {});
    note.textContent = t("tx.recomputeDone", { n: res.groups ?? res.recomputed ?? "" });
    // 表示中の銘柄詳細を読み直す
    if (_secDetailId != null) showSecurityDetail(_secDetailId, _secRange);
  } catch (e) {
    note.textContent = t("tx.recomputeFail", { error: e.message });
  }
});

function renderCostBasisCard(data, secCur, currency) {
  const card = document.getElementById("cb-card");
  const groups = data.cost_basis || [];
  if (!groups.length) {
    card.classList.add("hidden");
    return;
  }
  card.classList.remove("hidden");

  const tbody = document.querySelector("#cb-table tbody");
  tbody.innerHTML = "";
  groups.forEach((g) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${escapeHtml(g.account || "")}</td>
      <td>${coverageBadgeHtml(g.coverage)}</td>
      <td class="num">${fmtAmount(g.covered_quantity)}</td>
      <td class="num">${fmtAmount(g.residual_quantity)}</td>
      <td class="num">${fmtPrice(g.residual_avg_cost, secCur)}</td>
      <td>${escapeHtml(g.acquired_on || "—")}</td>
      <td class="num">${plAmountHtml(g.realized_pl, currency)}</td>
      <td class="num">${fmtMoney(g.income_total, currency)}</td>
    `;
    tbody.appendChild(tr);
  });

  // 「取得単価を再計算した」と言い切れるのは全期間を覆えたときだけ。
  // 部分被覆では逆算の都合で MF と同じ値になるので、そこを正直に書く。
  const primary = groups[0];
  const explain = document.getElementById("cb-explain");
  const key = {
    full: "tx.explainFull",
    partial: "tx.explainPartial",
    partial_uncosted: "tx.explainPartialUncosted",
    unreconciled: "tx.explainUnreconciled",
  }[primary.coverage];
  explain.textContent = key
    ? t(key, {
        covered: fmtAmount(primary.covered_quantity),
        residual: fmtAmount(primary.residual_quantity),
      })
    : "";

  const warnings = groups.flatMap((g) => (g.warnings || []).map((w) => w.message));
  renderWarningsInto(document.getElementById("cb-warnings"), warnings);
  document.getElementById("cb-warnings").classList.toggle("hidden", !warnings.length);
}

async function loadTransactionHistory(securityId, secCur, currency, append) {
  const card = document.getElementById("tx-history-card");
  const tbody = document.querySelector("#tx-history-table tbody");
  const more = document.getElementById("tx-history-more");
  if (!append) {
    _txHistoryOffset = 0;
    tbody.innerHTML = "";
  }
  let data;
  try {
    data = await fetchJSON(
      `/api/securities/${securityId}/transactions?limit=50&offset=${_txHistoryOffset}`);
  } catch (e) {
    card.classList.add("hidden");
    return;
  }
  if (!data.total) {
    card.classList.add("hidden");
    return;
  }
  card.classList.remove("hidden");
  (data.transactions || []).forEach((tx) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${escapeHtml(tx.trade_date)}</td>
      <td>${escapeHtml(txTypeLabel(tx.tx_type))}</td>
      <td>${escapeHtml(tx.account || "")}</td>
      <td class="num">${fmtAmount(tx.quantity)}</td>
      <td class="num">${fmtPrice(tx.unit_price, secCur)}</td>
      <td class="num">${fmtMoney(tx.net_amount, tx.currency || currency)}</td>
      <td>${escapeHtml(tx.lot_label || "")}</td>
    `;
    tbody.appendChild(tr);
  });
  _txHistoryOffset += (data.transactions || []).length;
  more.classList.toggle("hidden", _txHistoryOffset >= data.total);
  more.onclick = () => loadTransactionHistory(securityId, secCur, currency, true);
}

// ---- 受信フォルダ（自動取込） ----

function inboxStatusInfo(status) {
  if (status === "committed") return { label: t("inbox.stCommitted"), cls: "diff-new" };
  if (status === "duplicate") return { label: t("inbox.stDuplicate"), cls: "diff-unchanged" };
  if (status === "rejected") return { label: t("inbox.stRejected"), cls: "diff-missing" };
  return { label: t("inbox.stError"), cls: "diff-missing" };
}

function renderInboxStatus(data) {
  const statusEl = document.getElementById("inbox-status");
  const scanBtn = document.getElementById("inbox-scan-btn");
  const tbody = document.querySelector("#inbox-events-table tbody");
  if (!data.enabled) {
    statusEl.textContent = t("inbox.disabled");
    scanBtn.classList.add("hidden");
    tbody.innerHTML = "";
    return;
  }
  scanBtn.classList.remove("hidden");
  const lastScan = data.last_scan_at
    ? t("inbox.lastScan", { time: fmtDate(data.last_scan_at) })
    : t("inbox.neverScanned");
  statusEl.innerHTML =
    `${t("inbox.dirLabel")}: <code>${escapeHtml(data.dir || "")}</code> ・ ` +
    `${t("inbox.pollLabel", { sec: data.poll_seconds })} ・ ${escapeHtml(lastScan)}`;

  const events = data.events || [];
  tbody.innerHTML = "";
  events.forEach((ev) => {
    const info = inboxStatusInfo(ev.status);
    const detailParts = [];
    if (ev.status === "committed") {
      detailParts.push(t("inbox.evCounts", {
        created: ev.created, updated: ev.updated, zeroed: ev.zeroed, date: ev.as_of || "-",
      }));
    }
    if (ev.detail) detailParts.push(ev.detail);
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td style="white-space:nowrap">${escapeHtml(fmtDate(ev.at))}</td>
      <td>${escapeHtml(ev.filename || "")}</td>
      <td><span class="diff-status">${escapeHtml(info.label)}</span></td>
      <td>${escapeHtml(detailParts.join(" / "))}</td>
    `;
    tr.className = info.cls;
    tbody.appendChild(tr);
  });
  if (events.length === 0) {
    tbody.innerHTML = `<tr><td colspan="4" class="muted">${t("inbox.noEvents")}</td></tr>`;
  }
}

async function loadInboxStatus() {
  const statusEl = document.getElementById("inbox-status");
  try {
    const data = await fetchJSON("/api/import/inbox");
    renderInboxStatus(data);
  } catch (e) {
    statusEl.textContent = `${t("status.error")}${e.message}`;
  }
}

document.getElementById("inbox-scan-btn").addEventListener("click", async () => {
  hideResult("inbox-scan-result");
  const btn = document.getElementById("inbox-scan-btn");
  btn.disabled = true;
  const origText = btn.textContent;
  btn.textContent = t("inbox.scanning");
  try {
    const data = await apiCall("/api/import/inbox/scan", "POST", {});
    renderInboxStatus(data);
    const n = (data.new_events || []).length;
    showResult("inbox-scan-result", true, t("inbox.scanDone", { count: n }));
    if (n > 0) loadImportHistory();
  } catch (e) {
    showResult("inbox-scan-result", false, t("inbox.scanFail", { error: e.message }));
  } finally {
    btn.disabled = false;
    btn.textContent = origText;
  }
});

// ---- 手動登録ページ ----

const MANAGE_TABS = ["sec", "cash", "metal", "estate", "crypto", "pension"];

document.querySelectorAll(".manage-tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".manage-tab").forEach((b) =>
      b.classList.toggle("active", b === btn));
    MANAGE_TABS.forEach((tab) => {
      const el = document.getElementById(`manage-tab-${tab}`);
      if (el) el.classList.toggle("hidden", tab !== btn.dataset.tab);
    });
  });
});

async function _loadSecuritiesCache() {
  const data = await fetchJSON("/api/securities");
  _securities = data.securities || [];
  return _securities;
}

async function loadManagePage() {
  // 日付初期値
  ["nh-as-of", "cash-as-of", "metal-as-of", "re-as-of", "pp-as-of", "re-val-date"].forEach((id) => {
    const el = document.getElementById(id);
    if (el && !el.value) el.value = todayISO();
  });
  _populateClassSelect();
  try {
    await Promise.all([_loadSecuritiesCache(), _loadAccountsCache()]);
  } catch (e) {
    console.warn("[asset-summary] manage load:", e);
  }
  renderSecuritiesTable();
  _syncSecuritySelects();
  loadManageHoldings();
  loadManageClassLists();
  loadCsStatusCard();
}

// Crypto-Summary 接続状態カード（暗号資産タブ先頭）
async function loadCsStatusCard() {
  const body = document.getElementById("cs-status-body");
  if (!body) return;
  if (!csMeta().enabled) {
    body.innerHTML = `<p class="settings-hint">${escapeHtml(t("cs.notConfigured"))}</p>`;
    return;
  }
  body.innerHTML = `<p class="loading">${t("label.loading")}</p>`;
  try {
    const d = await fetchJSON(
      `/api/crypto-summary/status?currency=${currentCurrency()}`
    );
    const parts = [];
    if (d.connected) {
      parts.push(
        `<p><span class="cs-dot-ok">●</span> ${escapeHtml(t("cs.connected"))}` +
        ` — ${escapeHtml(t("cs.statusSummary", { count: d.asset_count ?? "?" }))}` +
        ` ${fmtMoney(d.total_value, d.currency)}</p>`
      );
      if (d.cs_generated_at) {
        parts.push(`<p class="muted">${escapeHtml(
          t("status.updatedAt", { time: fmtDate(d.cs_generated_at) })
        )}</p>`);
      }
    } else {
      parts.push(
        `<p><span class="cs-dot-err">●</span> ${escapeHtml(t("cs.unreachable"))}</p>` +
        `<p class="muted">${escapeHtml(t("cs.unreachableDetail"))}</p>`
      );
      (d.warnings || []).forEach((w) => parts.push(`<p class="muted">⚠ ${escapeHtml(w)}</p>`));
    }
    if (d.url) {
      parts.push(
        `<p><a class="settings-save-btn cs-open-btn" href="${escapeHtml(d.url)}/#dashboard"` +
        ` target="_blank" rel="noopener">${escapeHtml(t("cs.openApp"))}</a></p>`
      );
    }
    body.innerHTML = parts.join("");
  } catch (e) {
    body.innerHTML = `<p class="muted">${t("status.error")}${escapeHtml(e.message)}</p>`;
  }
}

// ======================================================================
// タブごとの「登録済み一覧」（そのタブで登録したものをその場で消せるように）
// ======================================================================

// タブID → そのタブが扱う資産クラス
const MANAGE_TAB_CLASSES = {
  cash: ["cash"],
  metal: ["metal"],
  estate: ["real_estate"],
  crypto: ["crypto"],
  pension: ["pension", "point"],
};

async function loadManageClassLists() {
  // 評価額つきで出すので /api/summary を使う。初回は価格取得で数秒かかるため、
  // 直前に取得済みのサマリーがあればまずそちらで即描画しておく。
  if (_lastSummary) {
    Object.entries(MANAGE_TAB_CLASSES).forEach(([tab, classes]) => {
      renderManageClassList(tab, classes, _lastSummary.holdings || [],
                            _lastSummary.currency);
    });
  } else {
    Object.keys(MANAGE_TAB_CLASSES).forEach((tab) => {
      const tbody = document.querySelector(`#${tab}-list-table tbody`);
      if (tbody) tbody.innerHTML =
        `<tr><td colspan="6" class="loading">${t("label.loading")}</td></tr>`;
    });
  }
  let summary = null;
  try {
    summary = await fetchJSON(`/api/summary?currency=${currentCurrency()}`);
  } catch (e) {
    console.warn("[asset-summary] manage lists:", e);
  }
  const holdings = (summary && summary.holdings) || [];
  const currency = (summary && summary.currency) || currentCurrency();
  Object.entries(MANAGE_TAB_CLASSES).forEach(([tab, classes]) => {
    renderManageClassList(tab, classes, holdings, currency);
  });
}

function renderManageClassList(tab, classes, holdings, currency) {
  const tbody = document.querySelector(`#${tab}-list-table tbody`);
  const emptyEl = document.getElementById(`${tab}-list-empty`);
  const table = document.getElementById(`${tab}-list-table`);
  if (!tbody) return;
  // CS 連携の仮想行は AS では削除できない（実体が無い）ので登録済み一覧に出さない
  const rows = holdings.filter(
    (h) => classes.includes(h.asset_class) && h.origin !== "crypto_summary"
  );
  tbody.innerHTML = "";
  if (emptyEl) emptyEl.classList.toggle("hidden", rows.length > 0);
  if (table) table.classList.toggle("hidden", rows.length === 0);

  rows.forEach((h) => {
    const tr = document.createElement("tr");
    tr.innerHTML =
      `<td>${escapeHtml(h.name)}</td>` +
      `<td>${escapeHtml(h.account || "")}</td>` +
      `<td class="num">${fmtAmount(h.quantity)}${h.unit === "gram" ? " g" : ""}</td>` +
      `<td class="num">${fmtMoney(h.value, currency)}</td>` +
      `<td>${escapeHtml(h.as_of || "")}</td>`;
    const td = document.createElement("td");
    td.className = "num";
    const btn = document.createElement("button");
    btn.className = "delete-btn";
    btn.textContent = `✕ ${t("btn.delete")}`;
    btn.addEventListener("click", () => {
      openConfirmDialog(
        t("manage.deleteEntryTitle"),
        t("manage.deleteEntryMsg", { name: h.name, account: h.account || "" }),
        async () => {
          // その銘柄×口座の保有をすべて消し、他に保有が無ければ銘柄も消す
          const all = (await fetchJSON("/api/holdings")).holdings || [];
          const targets = all.filter(
            (x) => x.security_id === h.id && x.account_id === h.account_id
          );
          for (const x of targets) {
            await apiCall(`/api/holdings/${x.id}`, "DELETE");
          }
          const remaining = (await fetchJSON("/api/holdings")).holdings || [];
          if (!remaining.some((x) => x.security_id === h.id)) {
            try {
              await apiCall(`/api/securities/${h.id}`, "DELETE");
            } catch (_) { /* 他で参照されていれば銘柄は残す */ }
          }
          await loadManagePage();
        }
      );
    });
    td.appendChild(btn);
    tr.appendChild(td);
    tbody.appendChild(tr);
  });
}

// ---- 暗号資産の登録 ----

let _cryptoResults = [];

async function searchCrypto() {
  const q = document.getElementById("crypto-search").value.trim();
  const sel = document.getElementById("crypto-pick");
  if (!q) { sel.innerHTML = ""; return; }
  sel.innerHTML = `<option>${t("label.loading")}</option>`;
  try {
    const d = await fetchJSON(`/api/coin-search?q=${encodeURIComponent(q)}`);
    _cryptoResults = d.results || [];
    sel.innerHTML = "";
    _cryptoResults.slice(0, 25).forEach((c) => {
      const opt = document.createElement("option");
      opt.value = c.ref;
      opt.textContent = `${c.name}${c.symbol ? ` (${c.symbol})` : ""}`;
      sel.appendChild(opt);
    });
    if (!_cryptoResults.length) {
      sel.innerHTML = `<option value="">${t("manage.cryptoNoHit")}</option>`;
    }
  } catch (e) {
    sel.innerHTML = `<option value="">${escapeHtml(e.message)}</option>`;
  }
}

async function saveCrypto() {
  hideResult("crypto-result");
  const ref = document.getElementById("crypto-pick").value;
  const picked = _cryptoResults.find((c) => c.ref === ref);
  const account = document.getElementById("crypto-account").value.trim();
  const qty = document.getElementById("crypto-qty").value;
  const cost = document.getElementById("crypto-cost").value;
  const asOf = document.getElementById("crypto-as-of").value || todayISO();
  if (!ref || !picked) {
    showResult("crypto-result", false, t("manage.cryptoPickRequired"));
    return;
  }
  if (!account || !qty) {
    showResult("crypto-result", false, t("manage.cryptoFieldsRequired"));
    return;
  }
  try {
    const name = `${picked.name}${picked.symbol ? ` (${picked.symbol})` : ""}`;
    // 同じコインIDで既に登録済みならその銘柄を再利用する
    const existing = (_securities || []).find(
      (s) => s.price_source_type === "coingecko" && s.price_source_ref === ref
    );
    const secId = existing
      ? existing.id
      : (await apiCall("/api/securities", "POST", {
          name,
          asset_class: "crypto",
          currency: "JPY",
          unit: "unit",
          price_source_type: "coingecko",
          price_source_ref: ref,
        })).id;
    await apiCall("/api/holdings", "POST", {
      security_id: secId,
      account_name: account,
      quantity: qty,
      avg_cost: cost || null,
      as_of: asOf,
    });
    showResult("crypto-result", true, t("status.added"));
    document.getElementById("crypto-qty").value = "";
    document.getElementById("crypto-cost").value = "";
    await loadManagePage();
  } catch (e) {
    showResult("crypto-result", false, t("status.addFail", { error: e.message }));
  }
}

document.getElementById("crypto-search").addEventListener("change", searchCrypto);
document.getElementById("crypto-search").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); searchCrypto(); }
});
document.getElementById("crypto-save").addEventListener("click", saveCrypto);

function _populateClassSelect() {
  const sel = document.getElementById("ns-class");
  if (!sel || sel.options.length > 0) return;
  (META && META.asset_classes ? META.asset_classes : []).forEach((c) => {
    const opt = document.createElement("option");
    opt.value = c.id;
    opt.textContent = classLabel(c.id);
    sel.appendChild(opt);
  });
}

// クラス変更時に単位の既定値を合わせる
document.getElementById("ns-class").addEventListener("change", () => {
  const cls = document.getElementById("ns-class").value;
  const unitSel = document.getElementById("ns-unit");
  const map = {
    cash: "currency", fund_jp: "kuchi", fund_foreign: "kuchi",
    metal: "gram", real_estate: "unit", pension: "unit", point: "point",
  };
  unitSel.value = map[cls] || "share";
});

function renderSecuritiesTable() {
  const tbody = document.querySelector("#securities-table tbody");
  tbody.innerHTML = "";
  _securities.forEach((s) => {
    const tr = document.createElement("tr");
    const statusHtml = s.price_source_status === "unlinked"
      ? `<span class="unlinked-warn">${t("label.unlinked")}</span>`
      : escapeHtml(s.price_source_type && s.price_source_type !== "none"
          ? `${s.price_source_type}${s.price_source_ref ? ": " + s.price_source_ref : ""}`
          : "—");
    tr.innerHTML = `
      <td><span class="asset-name"><span class="asset-label">${escapeHtml(s.name)}</span></span></td>
      <td>${s.code ? `<code class="asset-code">${escapeHtml(s.code)}</code>` : ""}</td>
      <td class="sec-class-cell"></td>
      <td>${escapeHtml(s.currency || "")}</td>
      <td>${statusHtml}</td>
      <td class="num"><button class="merge-btn" style="margin-right:6px" title="${t("manage.mergeBtnTitle")}">⇆ ${t("manage.mergeBtn")}</button><button class="delete-btn" title="${t("btn.delete")}">✕ ${t("btn.delete")}</button></td>
    `;
    // クラス変更セレクト（設計: asset_class はUIから銘柄ごとに変更可）
    const cell = tr.querySelector(".sec-class-cell");
    const sel = document.createElement("select");
    sel.style.fontSize = "12px";
    sel.style.padding = "3px 6px";
    (META && META.asset_classes ? META.asset_classes : []).forEach((c) => {
      const opt = document.createElement("option");
      opt.value = c.id;
      opt.textContent = classLabel(c.id);
      if (c.id === s.asset_class) opt.selected = true;
      sel.appendChild(opt);
    });
    sel.addEventListener("change", async () => {
      try {
        await apiCall(`/api/securities/${s.id}`, "PUT", { asset_class: sel.value });
        await _loadSecuritiesCache();
      } catch (e) {
        alert(t("status.settingsSaveFail", { error: e.message }));
        sel.value = s.asset_class;
      }
    });
    cell.appendChild(sel);

    tr.querySelector(".merge-btn").addEventListener("click", () => openMergeDialog(s));
    tr.querySelector(".delete-btn").addEventListener("click", () => {
      openConfirmDialog(
        t("manage.deleteSecTitle"),
        t("manage.deleteSecMsg", { name: s.name }),
        async () => {
          try {
            await apiCall(`/api/securities/${s.id}`, "DELETE");
          } catch (e) {
            if (e.status === 409) {
              alert(t("manage.deleteSecConflict"));
              return;
            }
            throw e;
          }
          await _loadSecuritiesCache();
          renderSecuritiesTable();
          _syncSecuritySelects();
        }
      );
    });
    tbody.appendChild(tr);
  });
  if (_securities.length === 0) {
    tbody.innerHTML = `<tr><td colspan="6" class="muted">${t("manage.noSecurities")}</td></tr>`;
  }
}

// ---------------------------------------------------------------
// 銘柄の統合（名寄せ）。MF PDF が証券会社ごとの表記で同じファンドを
// 別銘柄として作ってしまったときに、保有・取引ごと1つにまとめる。
// ---------------------------------------------------------------

let _mergeSourceSec = null;

function openMergeDialog(source) {
  // quantity / avg_cost の単位が同じ銘柄だけが統合先になれる（API側も検証する）
  const candidates = (_securities || []).filter((s) =>
    s.id !== source.id &&
    s.asset_class === source.asset_class &&
    s.currency === source.currency &&
    s.unit === source.unit &&
    s.price_unit_divisor === source.price_unit_divisor);
  if (!candidates.length) {
    alert(t("manage.mergeNoCandidates"));
    return;
  }
  _mergeSourceSec = source;
  document.getElementById("merge-dialog-msg").textContent =
    t("manage.mergeMsg", { name: source.name });
  const sel = document.getElementById("merge-target");
  sel.innerHTML = "";
  candidates.forEach((s) => {
    const opt = document.createElement("option");
    opt.value = s.id;
    opt.textContent = s.name + (s.code ? ` (${s.code})` : "");
    sel.appendChild(opt);
  });
  document.getElementById("merge-dialog").classList.remove("hidden");
}

document.getElementById("merge-confirm").addEventListener("click", async () => {
  const source = _mergeSourceSec;
  const targetId = document.getElementById("merge-target").value;
  document.getElementById("merge-dialog").classList.add("hidden");
  _mergeSourceSec = null;
  if (!source || !targetId) return;
  try {
    await apiCall(`/api/securities/${targetId}/merge`, "POST", { source_id: source.id });
  } catch (e) {
    alert(t("manage.mergeFail", { error: e.message }));
    return;
  }
  await _loadSecuritiesCache();
  renderSecuritiesTable();
  _syncSecuritySelects();
  loadManageHoldings();
});

document.getElementById("merge-cancel").addEventListener("click", () => {
  _mergeSourceSec = null;
  document.getElementById("merge-dialog").classList.add("hidden");
});

document.getElementById("merge-dialog").addEventListener("click", (e) => {
  if (e.target === e.currentTarget) {
    _mergeSourceSec = null;
    e.currentTarget.classList.add("hidden");
  }
});

function _syncSecuritySelects() {
  // 保有追加フォームの銘柄セレクト
  const sel = document.getElementById("nh-security");
  const prev = sel.value;
  sel.innerHTML = "";
  _securities.forEach((s) => {
    const opt = document.createElement("option");
    opt.value = s.id;
    opt.textContent = s.name + (s.code ? ` (${s.code})` : "");
    sel.appendChild(opt);
  });
  if ([...sel.options].some((o) => o.value === prev)) sel.value = prev;

  // 不動産の評価履歴セレクト
  const reSel = document.getElementById("re-select");
  const rePrev = reSel.value;
  reSel.innerHTML = "";
  _securities.filter((s) => s.asset_class === "real_estate").forEach((s) => {
    const opt = document.createElement("option");
    opt.value = s.id;
    opt.textContent = s.name;
    reSel.appendChild(opt);
  });
  if ([...reSel.options].some((o) => o.value === rePrev)) reSel.value = rePrev;
  loadEstateValuations();

  const idxSel = document.getElementById("re-index-sec");
  if (idxSel) {
    const idxPrev = idxSel.value;
    idxSel.innerHTML = "";
    _securities.filter((s) => s.asset_class === "real_estate").forEach((s) => {
      const opt = document.createElement("option");
      opt.value = s.id;
      opt.textContent = s.name;
      idxSel.appendChild(opt);
    });
    if ([...idxSel.options].some((o) => o.value === idxPrev)) idxSel.value = idxPrev;
    loadEstateIndexOptions();
  }
}

// ---------------------------------------------------------------
// 不動産価格指数の紐付け
// ---------------------------------------------------------------

async function ensureReIndexOptions() {
  if (_reIndexOptions) return _reIndexOptions;
  try {
    _reIndexOptions = await fetchJSON("/api/re-index/options");
  } catch (e) {
    console.warn("[asset-summary] re-index options:", e);
  }
  return _reIndexOptions;
}

async function loadEstateIndexOptions() {
  const regionSel = document.getElementById("re-index-region");
  const typeSel = document.getElementById("re-index-type");
  if (!regionSel || !typeSel) return;
  const opts = await ensureReIndexOptions();
  if (!opts) return;
  if (!regionSel.options.length) {
    const none = document.createElement("option");
    none.value = "";
    none.textContent = t("manage.estateIndexNone");
    regionSel.appendChild(none);
    opts.regions.forEach((r) => {
      const o = document.createElement("option");
      o.value = r.code;
      o.textContent = r.label;
      regionSel.appendChild(o);
    });
    opts.types.forEach((ty) => {
      const o = document.createElement("option");
      o.value = ty.code;
      o.textContent = ty.label;
      typeSel.appendChild(o);
    });
  }
  const asOf = document.getElementById("re-index-asof");
  if (asOf) {
    // 実際に取り込めている最新月を出す。「3ヶ月遅れ」と決め打ちしない
    // （公表が止まることがあるため、延長がどこまで効くかはこれが唯一の手がかり）。
    asOf.textContent = opts.as_of
      ? t("manage.estateIndexAsOf", { month: opts.as_of.slice(0, 7) })
      : t("manage.estateIndexNotFetched");
  }
  ["re-index-attribution", "settings-re-index-attribution"].forEach((id) => {
    const el = document.getElementById(id);
    if (el) el.textContent = opts.attribution || "";
  });
  syncEstateIndexSelects();
}

function syncEstateIndexSelects() {
  const secId = document.getElementById("re-index-sec").value;
  const sec = _securities.find((s) => String(s.id) === String(secId));
  const prefix = (_reIndexOptions && _reIndexOptions.ref_prefix) || "re_index:";
  const ref = sec && sec.price_source_ref;
  let region = "";
  let type = "";
  if (ref && ref.startsWith(prefix)) {
    const parts = ref.slice(prefix.length).split(":");
    region = parts[0] || "";
    type = parts[1] || "";
  }
  document.getElementById("re-index-region").value = region;
  const typeSel = document.getElementById("re-index-type");
  typeSel.value = type || typeSel.options[0]?.value || "";
}

// 新規銘柄
document.getElementById("ns-save").addEventListener("click", async () => {
  hideResult("ns-result");
  const name = document.getElementById("ns-name").value.trim();
  if (!name) {
    showResult("ns-result", false, t("status.required"));
    return;
  }
  const cls = document.getElementById("ns-class").value;
  const body = {
    name,
    code: document.getElementById("ns-code").value.trim() || null,
    asset_class: cls,
    currency: document.getElementById("ns-currency").value,
    unit: document.getElementById("ns-unit").value,
  };
  if (cls === "fund_jp" || cls === "fund_foreign") body.price_unit_divisor = 10000;
  try {
    await apiCall("/api/securities", "POST", body);
    showResult("ns-result", true, t("status.addDone"));
    document.getElementById("ns-name").value = "";
    document.getElementById("ns-code").value = "";
    await _loadSecuritiesCache();
    renderSecuritiesTable();
    _syncSecuritySelects();
  } catch (e) {
    showResult("ns-result", false, t("status.addFail", { error: e.message }));
  }
});

// 保有追加
document.getElementById("nh-save").addEventListener("click", async () => {
  hideResult("nh-result");
  const securityId = document.getElementById("nh-security").value;
  const account = document.getElementById("nh-account").value.trim();
  const qty = document.getElementById("nh-quantity").value;
  if (!securityId || !account || qty === "") {
    showResult("nh-result", false, t("status.required"));
    return;
  }
  const body = {
    security_id: Number(securityId),
    account_name: account,
    quantity: qty,
  };
  const avgCost = document.getElementById("nh-avg-cost").value;
  if (avgCost !== "") body.avg_cost = avgCost;
  const asOf = document.getElementById("nh-as-of").value;
  if (asOf) body.as_of = asOf;
  const lotLabel = document.getElementById("nh-lot-label").value.trim();
  if (lotLabel) body.lot_label = lotLabel;
  try {
    await apiCall("/api/holdings", "POST", body);
    showResult("nh-result", true, t("status.addDone"));
    document.getElementById("nh-quantity").value = "";
    document.getElementById("nh-avg-cost").value = "";
    document.getElementById("nh-lot-label").value = "";
    await _loadAccountsCache();
    loadManageHoldings();
  } catch (e) {
    showResult("nh-result", false, t("status.addFail", { error: e.message }));
  }
});

// 登録済み保有一覧
async function loadManageHoldings() {
  const tbody = document.querySelector("#manage-holdings-table tbody");
  tbody.innerHTML = `<tr><td colspan="8" class="loading">${t("label.loading")}</td></tr>`;
  try {
    const data = await fetchJSON("/api/holdings");
    const holdings = data.holdings || [];
    const secById = {};
    _securities.forEach((s) => { secById[s.id] = s; });
    const acctById = {};
    _accounts.forEach((a) => { acctById[a.id] = a.display_name || a.name; });

    tbody.innerHTML = "";
    holdings.forEach((h) => {
      const sec = secById[h.security_id] || {};
      const name = h.name || h.security_name || sec.name || `#${h.security_id}`;
      const account = h.account || h.account_name || acctById[h.account_id] || "";
      const asOf = h.as_of || h.as_of_date || "";
      const snapId = h.id != null ? h.id : h.snapshot_id;
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${escapeHtml(name)}</td>
        <td>${escapeHtml(account)}</td>
        <td>${escapeHtml(h.lot_label || "")}</td>
        <td class="num">${fmtAmount(h.quantity)}</td>
        <td class="num">${fmtAmount(h.avg_cost)}</td>
        <td>${escapeHtml(asOf)}</td>
        <td class="num"><button class="delete-btn" title="${t("btn.delete")}">✕ ${t("btn.delete")}</button></td>
      `;
      tr.querySelector(".delete-btn").addEventListener("click", () => {
        openConfirmDialog(
          t("manage.deleteHoldingTitle"),
          t("manage.deleteHoldingMsg", { name, account, date: asOf }),
          async () => {
            await apiCall(`/api/holdings/${encodeURIComponent(snapId)}`, "DELETE");
            loadManageHoldings();
          }
        );
      });
      tbody.appendChild(tr);
    });
    if (holdings.length === 0) {
      tbody.innerHTML = `<tr><td colspan="7" class="muted">${t("label.noHoldings")}</td></tr>`;
    }
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="7" class="muted">${t("status.error")}${escapeHtml(e.message)}</td></tr>`;
  }
}

// 既存銘柄の再利用 or 新規作成 → security_id を返す
async function _findOrCreateSecurity(match, createBody) {
  const found = _securities.find(match);
  if (found) return found.id;
  const d = await apiCall("/api/securities", "POST", createBody);
  await _loadSecuritiesCache();
  const id = d && (d.id != null ? d.id : (d.security && d.security.id));
  if (id != null) return id;
  // レスポンスに id が無い場合はキャッシュから引き直す
  const again = _securities.find(match);
  if (again) return again.id;
  throw new Error("security id unresolved");
}

// 現金・預金
document.getElementById("cash-save").addEventListener("click", async () => {
  hideResult("cash-result");
  const name = document.getElementById("cash-name").value.trim();
  const account = document.getElementById("cash-account").value.trim();
  const amount = document.getElementById("cash-amount").value;
  const ccy = document.getElementById("cash-currency").value;
  if (!name || !account || amount === "") {
    showResult("cash-result", false, t("status.required"));
    return;
  }
  try {
    const secId = await _findOrCreateSecurity(
      (s) => s.asset_class === "cash" && s.name === name && s.currency === ccy,
      {
        name,
        asset_class: "cash",
        currency: ccy,
        unit: "currency",
        price_source_type: ccy === "JPY" ? "none" : "fx",
        price_source_ref: ccy === "JPY" ? null : ccy,
        price_source_status: ccy === "JPY" ? "not_required" : "linked",
      }
    );
    const body = { security_id: secId, account_name: account, quantity: amount };
    const asOf = document.getElementById("cash-as-of").value;
    if (asOf) body.as_of = asOf;
    await apiCall("/api/holdings", "POST", body);
    showResult("cash-result", true, t("status.addDone"));
    document.getElementById("cash-amount").value = "";
    await _loadAccountsCache();
  } catch (e) {
    showResult("cash-result", false, t("status.addFail", { error: e.message }));
  }
});

// 貴金属
const METAL_NAMES = { XAU: "金（現物）", XAG: "銀（現物）", XPT: "プラチナ（現物）" };

document.getElementById("metal-save").addEventListener("click", async () => {
  hideResult("metal-result");
  const kind = document.getElementById("metal-kind").value;
  const account = document.getElementById("metal-account").value.trim();
  const grams = document.getElementById("metal-grams").value;
  if (!account || grams === "") {
    showResult("metal-result", false, t("status.required"));
    return;
  }
  try {
    const secId = await _findOrCreateSecurity(
      (s) => s.asset_class === "metal" && s.price_source_ref === kind && s.unit === "gram",
      {
        name: METAL_NAMES[kind] || kind,
        asset_class: "metal",
        currency: "JPY",
        unit: "gram",
        price_source_type: "metal",
        price_source_ref: kind,
        price_source_status: "linked",
      }
    );
    const body = { security_id: secId, account_name: account, quantity: grams };
    const cost = document.getElementById("metal-cost").value;
    if (cost !== "") body.avg_cost = cost;
    const asOf = document.getElementById("metal-as-of").value;
    if (asOf) body.as_of = asOf;
    await apiCall("/api/holdings", "POST", body);
    showResult("metal-result", true, t("status.addDone"));
    document.getElementById("metal-grams").value = "";
    document.getElementById("metal-cost").value = "";
    await _loadAccountsCache();
  } catch (e) {
    showResult("metal-result", false, t("status.addFail", { error: e.message }));
  }
});

// 不動産
document.getElementById("re-save").addEventListener("click", async () => {
  hideResult("re-result");
  const name = document.getElementById("re-name").value.trim();
  const account = document.getElementById("re-account").value.trim();
  const cost = document.getElementById("re-cost").value;
  if (!name || !account || cost === "") {
    showResult("re-result", false, t("status.required"));
    return;
  }
  try {
    const secId = await _findOrCreateSecurity(
      (s) => s.asset_class === "real_estate" && s.name === name,
      {
        name,
        asset_class: "real_estate",
        currency: "JPY",
        unit: "unit",
        price_source_type: "manual",
        price_source_status: "manual",
      }
    );
    const body = { security_id: secId, account_name: account, quantity: "1", avg_cost: cost };
    const asOf = document.getElementById("re-as-of").value;
    if (asOf) body.as_of = asOf;
    await apiCall("/api/holdings", "POST", body);
    showResult("re-result", true, t("status.addDone"));
    document.getElementById("re-name").value = "";
    document.getElementById("re-cost").value = "";
    await _loadAccountsCache();
    _syncSecuritySelects();
  } catch (e) {
    showResult("re-result", false, t("status.addFail", { error: e.message }));
  }
});

// 不動産 評価履歴
async function loadEstateValuations() {
  const secId = document.getElementById("re-select").value;
  const tbody = document.querySelector("#re-val-table tbody");
  if (!secId) {
    tbody.innerHTML = `<tr><td colspan="3" class="muted">${t("manage.noEstate")}</td></tr>`;
    return;
  }
  try {
    const data = await fetchJSON(`/api/securities/${encodeURIComponent(secId)}/manual-prices`);
    const prices = data.prices || data.manual_prices || [];
    tbody.innerHTML = "";
    prices.forEach((p) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${escapeHtml(p.date)}</td>
        <td class="num">${fmtMoney(p.value != null ? p.value : p.price, "JPY")}</td>
        <td class="num"><button class="delete-btn" title="${t("btn.delete")}">✕ ${t("btn.delete")}</button></td>
      `;
      tr.querySelector(".delete-btn").addEventListener("click", () => {
        openConfirmDialog(
          t("manage.deleteValTitle"),
          t("manage.deleteValMsg", { date: p.date }),
          async () => {
            await apiCall(
              `/api/securities/${encodeURIComponent(secId)}/manual-prices/${encodeURIComponent(p.date)}`,
              "DELETE"
            );
            loadEstateValuations();
          }
        );
      });
      tbody.appendChild(tr);
    });
    if (prices.length === 0) {
      tbody.innerHTML = `<tr><td colspan="3" class="muted">${t("manage.noValuations")}</td></tr>`;
    }
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="3" class="muted">${t("status.error")}${escapeHtml(e.message)}</td></tr>`;
  }
}

document.getElementById("re-select").addEventListener("change", loadEstateValuations);

document.getElementById("re-val-add").addEventListener("click", async () => {
  hideResult("re-val-result");
  const secId = document.getElementById("re-select").value;
  const date = document.getElementById("re-val-date").value;
  const value = document.getElementById("re-val-value").value;
  if (!secId || !date || value === "") {
    showResult("re-val-result", false, t("status.required"));
    return;
  }
  try {
    await apiCall(`/api/securities/${encodeURIComponent(secId)}/manual-price`, "POST", { date, value });
    showResult("re-val-result", true, t("status.addDone"));
    document.getElementById("re-val-value").value = "";
    loadEstateValuations();
  } catch (e) {
    showResult("re-val-result", false, t("status.addFail", { error: e.message }));
  }
});

document.getElementById("re-index-sec")?.addEventListener("change", syncEstateIndexSelects);

document.getElementById("re-index-save")?.addEventListener("click", async () => {
  hideResult("re-index-result");
  const secId = document.getElementById("re-index-sec").value;
  const region = document.getElementById("re-index-region").value;
  const type = document.getElementById("re-index-type").value;
  if (!secId) {
    showResult("re-index-result", false, t("status.required"));
    return;
  }
  const prefix = (_reIndexOptions && _reIndexOptions.ref_prefix) || "re_index:";
  // 地域が未選択なら連携解除。price_source_type/status は manual のまま触らない
  const ref = region ? `${prefix}${region}:${type}` : null;
  try {
    await apiCall(`/api/securities/${encodeURIComponent(secId)}`, "PUT", {
      price_source_ref: ref,
    });
    showResult("re-index-result", true, t("status.settingsSaved"));
    await _loadSecuritiesCache();
  } catch (e) {
    showResult("re-index-result", false, t("status.settingsSaveFail", { error: e.message }));
  }
});

// 年金・ポイント
document.getElementById("pp-kind").addEventListener("change", () => {
  const kind = document.getElementById("pp-kind").value;
  document.getElementById("pp-cost-row").classList.toggle("hidden", kind !== "pension");
  document.getElementById("pp-value-label").textContent =
    kind === "pension" ? t("manage.ppValue") : t("manage.ppPoints");
  const acct = document.getElementById("pp-account");
  if (kind === "pension" && !acct.value) acct.value = "年金";
});

document.getElementById("pp-save").addEventListener("click", async () => {
  hideResult("pp-result");
  const kind = document.getElementById("pp-kind").value;
  const name = document.getElementById("pp-name").value.trim();
  const account = document.getElementById("pp-account").value.trim();
  const value = document.getElementById("pp-value").value;
  if (!name || !account || value === "") {
    showResult("pp-result", false, t("status.required"));
    return;
  }
  const asOf = document.getElementById("pp-as-of").value || todayISO();
  try {
    if (kind === "pension") {
      // 年金: quantity=1・avg_cost=取得価額総額・評価額は manual-price に登録
      const secId = await _findOrCreateSecurity(
        (s) => s.asset_class === "pension" && s.name === name,
        {
          name,
          asset_class: "pension",
          currency: "JPY",
          unit: "unit",
          price_source_type: "manual",
          price_source_status: "manual",
        }
      );
      const body = { security_id: secId, account_name: account, quantity: "1", as_of: asOf };
      const cost = document.getElementById("pp-cost").value;
      if (cost !== "") body.avg_cost = cost;
      await apiCall("/api/holdings", "POST", body);
      await apiCall(`/api/securities/${encodeURIComponent(secId)}/manual-price`, "POST", {
        date: asOf, value,
      });
    } else {
      // ポイント: quantity=ポイント数（1pt=1円換算はサーバ側規約）
      const secId = await _findOrCreateSecurity(
        (s) => s.asset_class === "point" && s.name === name,
        {
          name,
          asset_class: "point",
          currency: "JPY",
          unit: "point",
          price_source_type: "none",
          price_source_status: "not_required",
        }
      );
      await apiCall("/api/holdings", "POST", {
        security_id: secId, account_name: account, quantity: value, as_of: asOf,
      });
    }
    showResult("pp-result", true, t("status.addDone"));
    document.getElementById("pp-value").value = "";
    document.getElementById("pp-cost").value = "";
    await _loadAccountsCache();
  } catch (e) {
    showResult("pp-result", false, t("status.addFail", { error: e.message }));
  }
});

// ---- 設定ページ ----

async function loadSettingsPage() {
  loadEstateIndexOptions();  // 出典表記（PDL 1.0 の必須要件）を出す
  // 表示設定（/api/meta を再取得して最新値に）
  try {
    const meta = await fetchJSON("/api/meta");
    META = meta;
    _rebuildClassMeta();
    const s = meta.settings || {};
    renderSettingsIncludes();
    if (s.default_currency) {
      document.getElementById("set-default-currency").value = s.default_currency;
    }
    document.getElementById("set-merge-cash").checked = s.merge_cash !== false;
  } catch (e) {
    console.warn("[asset-summary] meta load:", e);
  }
  _syncCurrencyLabels();
  loadUnlinkedSecurities();
  loadSettingsCsStatus();
  // 言語切替などで再表示された場合、保持している判定結果を描き直す
  if (_autolinkSuggestions && _autolinkSuggestions.length) renderAutolinkResults();
}

// 設定ページ: Crypto-Summary 連携の状態（読み取り専用・設定は env で行う）
async function loadSettingsCsStatus() {
  const el = document.getElementById("settings-cs-status");
  if (!el) return;
  if (!csMeta().enabled) {
    el.innerHTML = escapeHtml(t("cs.notConfigured"));
    return;
  }
  el.innerHTML = t("label.loading");
  try {
    const d = await fetchJSON("/api/crypto-summary/status");
    const dot = d.connected
      ? `<span class="cs-dot-ok">●</span> ${escapeHtml(t("cs.connected"))}`
      : `<span class="cs-dot-err">●</span> ${escapeHtml(t("cs.unreachable"))}`;
    const link = d.url
      ? ` — <a href="${escapeHtml(d.url)}" target="_blank" rel="noopener">${escapeHtml(d.url)}</a>`
      : "";
    el.innerHTML = dot + link;
  } catch (e) {
    el.innerHTML = escapeHtml(t("status.error")) + escapeHtml(e.message);
  }
}

async function _saveSettings(patch) {
  hideResult("settings-save-result");
  try {
    await apiCall("/api/settings", "PUT", patch);
    showResult("settings-save-result", true, t("status.settingsSaved"));
  } catch (e) {
    showResult("settings-save-result", false, t("status.settingsSaveFail", { error: e.message }));
  }
}

/** 設定ページ: 資産クラスごとの「総資産に含める」「ダッシュボードに表示」。 */
function renderSettingsIncludes() {
  const tbody = document.querySelector("#settings-class-table tbody");
  if (!tbody) return;
  const includes = (META && META.settings && META.settings.include_classes) || {};
  const chips = (META && META.settings && META.settings.dashboard_chip_classes) || null;
  tbody.innerHTML = "";
  ((META && META.asset_classes) || []).forEach((c) => {
    const tr = document.createElement("tr");

    const nameTd = document.createElement("td");
    nameTd.innerHTML =
      `<span class="tag-chip small" style="--tag-color:${classColor(c.id)}">` +
      `${escapeHtml(classLabel(c.id))}</span>`;
    tr.appendChild(nameTd);

    const incTd = document.createElement("td");
    incTd.className = "num";
    const inc = document.createElement("input");
    inc.type = "checkbox";
    inc.checked = includes[c.id] !== false;
    inc.addEventListener("change", () => setClassIncluded(c.id, inc.checked));
    incTd.appendChild(inc);
    tr.appendChild(incTd);

    const chipTd = document.createElement("td");
    chipTd.className = "num";
    const chip = document.createElement("input");
    chip.type = "checkbox";
    // 未設定（自動）のときは、いま実際に出ているクラスを反映して見せる
    chip.checked = chips ? chips.includes(c.id) : _autoChipClasses().includes(c.id);
    chip.addEventListener("change", () => setClassChipShown(c.id, chip.checked));
    chipTd.appendChild(chip);
    tr.appendChild(chipTd);

    tbody.appendChild(tr);
  });
}

/** チップ未設定時の既定＝保有があるクラス。 */
function _autoChipClasses() {
  return ((_lastSummary && _lastSummary.classes) || []).map((c) => c.class);
}

async function setClassChipShown(classId, shown) {
  const current = (META && META.settings && META.settings.dashboard_chip_classes)
    || _autoChipClasses();
  const next = shown
    ? Array.from(new Set([...current, classId]))
    : current.filter((c) => c !== classId);
  if (!META) META = {};
  if (!META.settings) META.settings = {};
  META.settings.dashboard_chip_classes = next;
  try {
    await apiCall("/api/settings", "PUT", { dashboard_chip_classes: next });
  } catch (e) {
    showResult("settings-save-result", false, e.message);
    return;
  }
  renderDashIncludeRow();
}
document.getElementById("set-default-currency").addEventListener("change", (e) => {
  _saveSettings({ default_currency: e.target.value });
});

document.getElementById("set-merge-cash").addEventListener("change", (e) => {
  if (META && META.settings) META.settings.merge_cash = e.target.checked;
  // 保有一覧は表示のたびに /api/summary を引き直すので、保存だけで反映される
  _saveSettings({ merge_cash: e.target.checked });
});

let _showLinkedList = false;  // 「連携済みの銘柄」リストの開閉（ページ再読込で閉じる）

/** 投信検索つきの連携行。relink=true なら現在の連携先と「連携を解除」も出す。 */
function _buildFundLinkRow(s, relink) {
  const row = document.createElement("div");
  row.className = "unlinked-row";
  const current = relink && s.price_source_ref
    ? `<code class="asset-code">${escapeHtml(s.price_source_ref)}</code>` : "";
  const unlinkBtn = relink
    ? `<button class="delete-btn fund-unlink-btn" style="margin-left:auto">${t("settings.unlinkBtn")}</button>`
    : "";
  row.innerHTML = `
    <div class="unlinked-row-head">
      <span class="sec-name">${escapeHtml(s.name)}</span>
      ${classBadgeHtml(s.asset_class)}
      ${s.code ? `<code class="asset-code">${escapeHtml(s.code)}</code>` : ""}
      ${current}
      ${unlinkBtn}
    </div>
    <div class="unlinked-search">
      <input class="settings-input fund-query" type="text" value="${escapeHtml(s.name)}"
        placeholder="${t("settings.searchPh")}" />
      <button class="action-btn fund-search-btn">${t("btn.search")}</button>
    </div>
    <div class="fund-results"></div>
    <div class="settings-result hidden fund-link-result"></div>
  `;
  const resultsEl = row.querySelector(".fund-results");
  const linkResult = row.querySelector(".fund-link-result");
  const doSearch = async () => {
    const q = row.querySelector(".fund-query").value.trim();
    if (!q) return;
    resultsEl.innerHTML = `<div class="loading">${t("label.loading")}</div>`;
    try {
      const data = await fetchJSON(`/api/fund-search?q=${encodeURIComponent(q)}`);
      const results = data.results || [];
      resultsEl.innerHTML = "";
      if (results.length === 0) {
        resultsEl.innerHTML = `<div class="muted" style="font-size:12px;padding:4px">${t("settings.noResults")}</div>`;
        return;
      }
      results.forEach((r) => {
        const rr = document.createElement("div");
        rr.className = "fund-result-row";
        rr.innerHTML = `
          <span class="fund-name">${escapeHtml(r.name)}</span>
          <span class="fund-cat">${escapeHtml(r.category || "")}</span>
          <code class="asset-code">${escapeHtml(r.isin || "")}</code>
          <button class="tx-link-btn">${t("btn.select")}</button>
        `;
        rr.querySelector("button").addEventListener("click", async () => {
          linkResult.classList.add("hidden");
          try {
            const d = await apiCall(`/api/securities/${s.id}`, "PUT", {
              price_source_type: "toushin",
              price_source_ref: r.ref,
              price_source_status: "linked",
            });
            linkResult.className = "settings-result ok fund-link-result";
            linkResult.textContent = t("settings.linkDone", { name: r.name })
              + _mergedSummaryText(d && d.merged)
              + _pensionUnitsText(d && d.pension_units);
            linkResult.classList.remove("hidden");
            setTimeout(loadUnlinkedSecurities, 900);
          } catch (e) {
            linkResult.className = "settings-result err fund-link-result";
            linkResult.textContent = t("settings.linkFail", { error: e.message });
            linkResult.classList.remove("hidden");
          }
        });
        resultsEl.appendChild(rr);
      });
    } catch (e) {
      resultsEl.innerHTML = `<div class="muted" style="font-size:12px;padding:4px">${t("status.error")}${escapeHtml(e.message)}</div>`;
    }
  };
  row.querySelector(".fund-search-btn").addEventListener("click", doSearch);
  row.querySelector(".fund-query").addEventListener("keydown", (e) => {
    if (e.key === "Enter") doSearch();
  });
  if (relink) {
    row.querySelector(".fund-unlink-btn").addEventListener("click", () => {
      openConfirmDialog(
        t("settings.unlinkTitle"),
        t("settings.unlinkMsg", { name: s.name }),
        async () => {
          await apiCall(`/api/securities/${s.id}`, "PUT", {
            price_source_type: "none",
            price_source_ref: null,
          });
          loadUnlinkedSecurities();
        },
        t("settings.unlinkBtn")
      );
    });
  }
  return row;
}

async function loadUnlinkedSecurities() {
  const wrap = document.getElementById("unlinked-list");
  const emptyEl = document.getElementById("unlinked-empty");
  const linkedWrap = document.getElementById("linked-list");
  const linkedToggle = document.getElementById("linked-toggle");
  wrap.innerHTML = `<div class="loading">${t("label.loading")}</div>`;
  emptyEl.classList.add("hidden");
  try {
    await _loadSecuritiesCache();
  } catch (e) {
    wrap.innerHTML = `<div class="muted">${t("status.error")}${escapeHtml(e.message)}</div>`;
    return;
  }
  // 年金（iDeCo・企業型DC）の中身も投信なので、未連携なら連携対象に含める。
  // 連携すると評価額から口数が逆算され、日々の基準価額で自動評価される
  const unlinked = _securities.filter((s) =>
    s.price_source_status === "unlinked" ||
    (s.asset_class === "pension" && s.price_source_status !== "linked"));
  // 自動判定ボタンは未連携が1件以上あるときだけ表示
  const autolinkBtn = document.getElementById("autolink-suggest-btn");
  if (autolinkBtn) autolinkBtn.classList.toggle("hidden", unlinked.length === 0);
  wrap.innerHTML = "";
  if (unlinked.length === 0) emptyEl.classList.remove("hidden");
  unlinked.forEach((s) => wrap.appendChild(_buildFundLinkRow(s, false)));

  // 連携済み（投信協会）の一覧 — 誤連携の修正（別ファンドへ再連携）と解除
  const linked = _securities.filter((s) =>
    s.price_source_status === "linked" && s.price_source_type === "toushin");
  if (linkedToggle) linkedToggle.classList.toggle("hidden", linked.length === 0);
  if (linkedWrap) {
    linkedWrap.innerHTML = "";
    linked.forEach((s) => linkedWrap.appendChild(_buildFundLinkRow(s, true)));
    linkedWrap.classList.toggle("hidden", !_showLinkedList || linked.length === 0);
  }
}

document.getElementById("linked-toggle").addEventListener("click", () => {
  _showLinkedList = !_showLinkedList;
  document.getElementById("linked-list").classList.toggle("hidden", !_showLinkedList);
});

// ---- 投信 自動連携（一括判定） ----

/** 連携時に同一ファンドの重複銘柄が自動統合されたときの追記メッセージ。 */
function _mergedSummaryText(merged) {
  if (!merged || !merged.length) return "";
  const list = merged.map((m) =>
    `${(m.merged_names || []).map((n) => `「${n}」`).join("")}→「${m.target_name}」`
  ).join(" / ");
  return " " + t("settings.autoMerged", { list });
}

/** 年金銘柄の口数が評価額から逆算されたときの追記メッセージ。 */
function _pensionUnitsText(derived) {
  if (!derived || !derived.length) return "";
  const list = derived.map((d) => `「${d.name}」`).join(" / ");
  return " " + t("settings.pensionDerived", { list });
}

function _autolinkCandidateByRef(s, ref) {
  if (!ref) return null;
  return (s.candidates || []).find((c) => c.ref === ref) || null;
}

// 基準価額は参考表示（マスク対象外）のため fmtMoney は使わない
function _fmtNav(v) {
  const n = Number(v);
  return isFinite(n) ? n.toLocaleString() : String(v);
}

function _autolinkBadgeHtml(status) {
  if (status === "auto") {
    return `<span class="diff-new"><span class="diff-status">${t("settings.autolink.badgeAuto")}</span></span>`;
  }
  if (status === "candidates") {
    return `<span class="autolink-cand"><span class="diff-status">${t("settings.autolink.badgeCandidates")}</span></span>`;
  }
  return `<span class="diff-unchanged"><span class="diff-status">${t("settings.autolink.badgeNone")}</span></span>`;
}

function _autolinkNavCellHtml(cand) {
  if (!cand) return '<span class="muted">—</span>';
  if (cand.nav_match === true) {
    return `<span class="pl-pos">${escapeHtml(t("settings.autolink.navMatch", {
      value: cand.nav_value != null ? _fmtNav(cand.nav_value) : "—",
      date: cand.nav_date || "—",
    }))}</span>`;
  }
  if (cand.nav_match === false) {
    return `<span class="unlinked-warn">${escapeHtml(t("settings.autolink.navMismatch", {
      value: cand.nav_value != null ? _fmtNav(cand.nav_value) : "—",
      reported: cand.reported_price != null ? _fmtNav(cand.reported_price) : "—",
    }))}</span>`;
  }
  // 年金: 基準価額の記載が無いため円単位照合は不能だが、取込2回分以上あれば
  // 「評価額の推移を候補NAVの騰落率で説明できるか」で裏を取れる
  if (cand.movement_match === true) {
    return `<span class="pl-pos">${escapeHtml(t("settings.autolink.movementMatch", {
      n: cand.movement_periods || 0,
    }))}</span>`;
  }
  if (cand.movement_match === false) {
    return `<span class="unlinked-warn">${escapeHtml(t("settings.autolink.movementMismatch"))}</span>`;
  }
  return `<span class="muted">${escapeHtml(t("settings.autolink.navUnknown"))}</span>`;
}

function renderAutolinkResults() {
  const box = document.getElementById("autolink-results");
  const tbody = document.querySelector("#autolink-table tbody");
  const summary = document.getElementById("autolink-summary");
  if (!box || !tbody || !summary) return;

  const suggestions = _autolinkSuggestions || [];
  if (suggestions.length === 0) {
    box.classList.add("hidden");
    return;
  }
  box.classList.remove("hidden");

  // 集計チップ
  const counts = { auto: 0, candidates: 0, none: 0 };
  suggestions.forEach((s) => {
    if (counts[s.status] !== undefined) counts[s.status] += 1;
  });
  summary.innerHTML = `
    <span class="section-chip autolink-chip autolink-chip-auto">${escapeHtml(t("settings.autolink.chipAuto", { count: counts.auto }))}</span>
    <span class="section-chip autolink-chip autolink-chip-cand">${escapeHtml(t("settings.autolink.chipCandidates", { count: counts.candidates }))}</span>
    <span class="section-chip autolink-chip autolink-chip-none">${escapeHtml(t("settings.autolink.chipNone", { count: counts.none }))}</span>
  `;

  tbody.innerHTML = "";
  suggestions.forEach((s) => {
    if (s._checked === undefined) s._checked = s.status === "auto";
    const tr = document.createElement("tr");

    // チェックボックス
    const tdCheck = document.createElement("td");
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.className = "autolink-check";
    if (s.status === "auto") {
      cb.checked = s._checked;
    } else if (s.status === "candidates") {
      // 候補を選択するまではチェック不可
      cb.disabled = !s._selRef;
      cb.checked = !!s._selRef && !!s._checked;
    } else {
      cb.disabled = true;
      cb.checked = false;
    }
    cb.addEventListener("change", () => { s._checked = cb.checked; });
    tdCheck.appendChild(cb);

    // 銘柄名（MF側）
    const tdName = document.createElement("td");
    tdName.innerHTML = `<span class="asset-label">${escapeHtml(s.name)}</span>`;

    // 判定バッジ
    const tdBadge = document.createElement("td");
    tdBadge.innerHTML = _autolinkBadgeHtml(s.status);

    // 連携先候補 / 基準価額照合
    const tdCand = document.createElement("td");
    const tdNav = document.createElement("td");
    if (s.status === "auto") {
      const cand = _autolinkCandidateByRef(s, s.best_ref) || (s.candidates || [])[0] || null;
      tdCand.innerHTML = cand
        ? `<span class="fund-name">${escapeHtml(cand.name)}</span> <span class="muted">${escapeHtml(cand.company || "")}</span>`
        : `<code class="asset-code">${escapeHtml(s.best_ref || "")}</code>`;
      tdNav.innerHTML = _autolinkNavCellHtml(cand);
    } else if (s.status === "candidates") {
      const sel = document.createElement("select");
      sel.className = "settings-input autolink-select";
      const ph = document.createElement("option");
      ph.value = "";
      ph.textContent = t("settings.autolink.selectPh");
      sel.appendChild(ph);
      (s.candidates || []).forEach((c) => {
        const opt = document.createElement("option");
        opt.value = c.ref;
        let label = `${c.name} / ${c.company || ""} / ` +
          t("settings.autolink.similarity", { pct: Math.round(Number(c.score) * 100) });
        if (c.nav_match === false) label += " " + t("settings.autolink.optNavMismatch");
        if (c.movement_match === true) {
          label += " " + t("settings.autolink.optMovementMatch", { n: c.movement_periods || 0 });
        } else if (c.movement_match === false) {
          label += " " + t("settings.autolink.optMovementMismatch");
        }
        opt.textContent = label;
        sel.appendChild(opt);
      });
      if (s._selRef) sel.value = s._selRef;
      sel.addEventListener("change", () => {
        s._selRef = sel.value || null;
        cb.disabled = !s._selRef;
        cb.checked = !!s._selRef;
        s._checked = cb.checked;
        tdNav.innerHTML = _autolinkNavCellHtml(_autolinkCandidateByRef(s, s._selRef));
      });
      tdCand.appendChild(sel);
      tdNav.innerHTML = _autolinkNavCellHtml(_autolinkCandidateByRef(s, s._selRef));
    } else {
      tdCand.innerHTML = `<span class="muted">${escapeHtml(t("settings.autolink.searchManually"))}</span>`;
      tdNav.innerHTML = '<span class="muted">—</span>';
    }

    tr.appendChild(tdCheck);
    tr.appendChild(tdName);
    tr.appendChild(tdBadge);
    tr.appendChild(tdCand);
    tr.appendChild(tdNav);
    tbody.appendChild(tr);
  });
}

async function runAutolinkSuggest() {
  const btn = document.getElementById("autolink-suggest-btn");
  const progress = document.getElementById("autolink-progress");
  hideResult("autolink-suggest-result");
  hideResult("autolink-apply-result");
  btn.disabled = true;
  progress.classList.remove("hidden");
  try {
    // 投信協会へのスロットル付き照会のため、応答まで数分かかることがある
    const d = await apiCall("/api/fund-links/suggest", "POST", null);
    _autolinkSuggestions = (d && d.suggestions) || [];
    renderAutolinkResults();
    renderWarningsInto(document.getElementById("autolink-warnings"), (d && d.warnings) || []);
    if (_autolinkSuggestions.length === 0) {
      showResult("autolink-suggest-result", true, t("settings.autolink.noSuggestions"));
    }
  } catch (e) {
    if (e.status === 409) {
      showResult("autolink-suggest-result", false, t("settings.autolink.busy"));
    } else {
      showResult("autolink-suggest-result", false, t("settings.autolink.suggestFail", { error: e.message }));
    }
  } finally {
    btn.disabled = false;
    progress.classList.add("hidden");
  }
}

async function applyAutolinkSelected() {
  hideResult("autolink-apply-result");
  const links = [];
  (_autolinkSuggestions || []).forEach((s) => {
    if (!s._checked) return;
    const ref = s.status === "auto" ? (s.best_ref || s._selRef) : s._selRef;
    if (ref) links.push({ security_id: s.security_id, ref });
  });
  if (links.length === 0) {
    showResult("autolink-apply-result", false, t("settings.autolink.noneSelected"));
    return;
  }
  const applyBtn = document.getElementById("autolink-apply-btn");
  const suggestBtn = document.getElementById("autolink-suggest-btn");
  applyBtn.disabled = true;
  suggestBtn.disabled = true;
  const origText = applyBtn.textContent;
  // 適用と同時に価格履歴の取得が走る（1件あたり数秒かかる）
  applyBtn.textContent = t("settings.autolink.applying");
  try {
    const d = await apiCall("/api/fund-links/apply", "POST", { links });
    const linkedIds = new Set(links.map((l) => l.security_id));
    _autolinkSuggestions = (_autolinkSuggestions || []).filter((s) => !linkedIds.has(s.security_id));
    renderAutolinkResults();
    showResult("autolink-apply-result", true, t("settings.autolink.applyDone", {
      count: d && d.linked != null ? d.linked : links.length,
    }) + _mergedSummaryText(d && d.merged) + _pensionUnitsText(d && d.pension_units));
    renderWarningsInto(document.getElementById("autolink-warnings"), (d && d.warnings) || []);
    // 未連携一覧を再読込（連携済みの銘柄が消える）
    loadUnlinkedSecurities();
  } catch (e) {
    showResult("autolink-apply-result", false, t("settings.autolink.applyFail", { error: e.message }));
  } finally {
    applyBtn.disabled = false;
    applyBtn.textContent = origText;
    suggestBtn.disabled = false;
  }
}

document.getElementById("autolink-suggest-btn").addEventListener("click", runAutolinkSuggest);
document.getElementById("autolink-apply-btn").addEventListener("click", applyAutolinkSelected);

document.getElementById("refresh-prices-btn").addEventListener("click", async () => {
  const btn = document.getElementById("refresh-prices-btn");
  hideResult("refresh-prices-result");
  btn.disabled = true;
  const origText = btn.textContent;
  btn.textContent = t("settings.refreshing");
  try {
    const d = await apiCall("/api/refresh-prices", "POST", {});
    let msg = t("settings.refreshDone");
    if (d && d.warnings && d.warnings.length) {
      msg += " / ⚠ " + d.warnings.join(" / ");
    }
    showResult("refresh-prices-result", true, msg);
  } catch (e) {
    showResult("refresh-prices-result", false, t("settings.refreshFail", { error: e.message }));
  } finally {
    btn.disabled = false;
    btn.textContent = origText;
  }
});

// ---- 戻るボタン・レンジタブ ----

document.getElementById("class-back").addEventListener("click", () => navigate("classes"));
document.getElementById("account-back").addEventListener("click", () => navigate("accounts"));
document.getElementById("security-back").addEventListener("click", () => navigate("holdings"));
document.getElementById("cs-asset-back").addEventListener("click", () =>
  navigate("classes", "detail", { name: "crypto" }));

document.getElementById("dash-range-tabs").querySelectorAll(".range-tab").forEach((btn) =>
  btn.addEventListener("click", () => loadDashHistoryChart(btn.dataset.range)));

document.getElementById("class-range-tabs").querySelectorAll(".range-tab").forEach((btn) =>
  btn.addEventListener("click", () => loadClassHistoryChart(null, btn.dataset.range)));

document.getElementById("cs-asset-range-tabs").querySelectorAll(".range-tab").forEach((btn) =>
  btn.addEventListener("click", () => {
    if (_csAssetSym == null) return;
    showCsAssetDetail(_csAssetSym, btn.dataset.range);
  }));

document.getElementById("acct-range-tabs").querySelectorAll(".range-tab").forEach((btn) =>
  btn.addEventListener("click", () => loadAcctHistoryChart(null, btn.dataset.range)));

document.getElementById("pf-range-tabs").querySelectorAll(".range-tab").forEach((btn) =>
  btn.addEventListener("click", () => loadPfHistoryChart(null, btn.dataset.range)));

document.getElementById("sec-range-tabs").querySelectorAll(".range-tab").forEach((btn) =>
  btn.addEventListener("click", () => {
    if (_secDetailId == null) return;
    _secRange = btn.dataset.range;
    localStorage.setItem("as_sec_range", _secRange);
    showSecurityDetail(_secDetailId, _secRange);
  }));

// ---- 通貨・再取得 ----

document.getElementById("currency").addEventListener("change", () => {
  localStorage.setItem("as_currency", currentCurrency());
  const cur = getCurrentPage();
  // 通貨に依存しない画面（取込・手動登録・設定）は再読込不要
  if (cur === "import" || cur === "manage" || cur === "settings") return;
  router();
});

document.getElementById("refresh").addEventListener("click", () => {
  router();
});

// ---- メタ情報 ----

function _rebuildClassMeta() {
  CLASS_META = {};
  ((META && META.asset_classes) || []).forEach((c) => {
    CLASS_META[c.id] = { label_ja: c.label_ja, label_en: c.label_en, color: c.color };
  });
}

async function loadMeta() {
  try {
    META = await fetchJSON("/api/meta");
    _rebuildClassMeta();
    // 対応通貨でセレクトを再構築
    const sel = document.getElementById("currency");
    const currencies = META.currencies || ["JPY", "USD", "EUR", "GBP"];
    const prev = sel.value;
    sel.innerHTML = "";
    currencies.forEach((c) => {
      const opt = document.createElement("option");
      opt.value = c;
      opt.textContent = c;
      sel.appendChild(opt);
    });
    const saved = localStorage.getItem("as_currency");
    const def = (META.settings && META.settings.default_currency) || "JPY";
    const want = saved || def;
    sel.value = currencies.includes(want) ? want : currencies[0];
  } catch (e) {
    console.warn("[asset-summary] meta:", e);
  }
}

// ======================================================================
// Myポートフォリオ / タグ
// ======================================================================

let _tags = [];
let _tagSummary = null;
let _tagAllocations = {};      // security_id(str) → [{tag_id, weight}]
let _assignFilter = "all";     // 割当リストの絞り込み: all | unallocated
let _autoAllocSuggestions = [];        // /api/tag-rules/suggest の結果
let _autoAllocFilter = "actionable";   // actionable(提案のみ) | all
let _pfEditingId = null;       // 編集中のポートフォリオid（null=新規）
let _pfFormTagIds = new Set();
let _tagSumChart = null;
let _pfTagChart = null;
let _dashTagChart = null;
let _pfDetail = null;

const TAG_PALETTE = [
  "#2f81f7", "#3fb950", "#a371f7", "#e3b341", "#39c5cf",
  "#f0883e", "#db61a2", "#6cb6ff", "#8957e5", "#8b949e",
];

function tagColor(tag, idx) {
  return (tag && tag.color) || TAG_PALETTE[idx % TAG_PALETTE.length];
}

function showPortfoliosList() {
  document.getElementById("portfolios-list-view").classList.remove("hidden");
  document.getElementById("pf-detail-view").classList.add("hidden");
}

function showPortfolioDetailView() {
  document.getElementById("portfolios-list-view").classList.add("hidden");
  document.getElementById("pf-detail-view").classList.remove("hidden");
}

// 詳細ビューの取得は遅い（価格・FX・Crypto-Summary への往復を伴う全再評価）。
// 続けて別の対象を開くと古い応答が後から届いて上書きしうるので、開くたびに
// 採番し、描画の直前に自分がまだ最新かを確かめる。
let _pfDetailReq = 0;

/** 詳細ビューの中身を消す。取得前に呼び、前に見ていた対象が残らないようにする。 */
function clearPortfolioDetail(name) {
  _pfDetail = null;
  document.getElementById("pf-detail-name").textContent = name || "";
  document.getElementById("pf-detail-meta").textContent = "";
  clearDetailHero("pf-detail");
  ["pf-tag-table", "pf-currency-table", "pf-class-table", "pf-account-table",
   "pf-holdings-table"].forEach((id) => {
    const tbody = document.querySelector(`#${id} tbody`);
    if (tbody) tbody.innerHTML = "";
  });
  if (_pfTagChart) { _pfTagChart.destroy(); _pfTagChart = null; }
}

// ---- 一覧ページ ----

async function loadPortfoliosPage() {
  const currency = currentCurrency();
  try {
    const [tagsRes, allocRes] = await Promise.all([
      fetchJSON("/api/tags"),
      fetchJSON("/api/security-tags"),
    ]);
    _tags = tagsRes.tags || [];
    _tagAllocations = allocRes.allocations || {};
  } catch (e) {
    console.warn("[asset-summary] tags:", e);
    _tags = [];
    _tagAllocations = {};
  }
  try {
    _tagSummary = await fetchJSON(`/api/tag-summary?currency=${currency}`);
  } catch (e) {
    console.warn("[asset-summary] tag summary:", e);
    _tagSummary = null;
  }
  if (!_securities.length) {
    try { _securities = (await fetchJSON("/api/securities")).securities || []; }
    catch (_) { /* noop */ }
  }
  renderTagSummary();
  renderUnallocBanner();
  renderTagTable();
  renderTagAssignList();
  await loadPortfolioList();
  applyI18n();
}

/** 配分が100%に満たない銘柄を目立たせる警告帯。 */
function renderUnallocBanner() {
  const el = document.getElementById("unalloc-banner");
  if (!el) return;
  const rows = (_tagSummary && _tagSummary.unallocated) || [];
  if (!rows.length) {
    el.classList.add("hidden");
    el.innerHTML = "";
    return;
  }
  const currency = currentCurrency();
  const total = rows.reduce((sum, r) => sum + Number(r.unallocated_value || 0), 0);
  const names = rows.slice(0, 6).map((r) => escapeHtml(r.name)).join("、");
  const more = rows.length > 6 ? t("pf.andMore", { count: rows.length - 6 }) : "";
  el.innerHTML =
    `⚠ <strong>${escapeHtml(t("pf.unallocWarn", {
      count: rows.length, amount: fmtMoney(total, currency),
    }))}</strong>` +
    `<div class="unalloc-names">${names}${more}</div>` +
    `<button class="card-link" id="unalloc-jump">${escapeHtml(t("pf.unallocJump"))}</button>`;
  el.classList.remove("hidden");
  const jump = document.getElementById("unalloc-jump");
  if (jump) {
    jump.addEventListener("click", () => {
      _assignFilter = "unallocated";
      _syncAssignFilterChips();
      renderTagAssignList();
      document.getElementById("tag-assign-list").scrollIntoView({ behavior: "smooth" });
    });
  }
}

function _syncAssignFilterChips() {
  const all = document.getElementById("assign-filter-all");
  const un = document.getElementById("assign-filter-unalloc");
  if (all) all.classList.toggle("active", _assignFilter === "all");
  if (un) un.classList.toggle("active", _assignFilter === "unallocated");
}

/** タグ別ドーナツ＋内訳テーブルを描画（ダッシュボードのクラス別と同じ構成）。 */
function _renderTagChartAndTable(rows, total, currency, canvasId, tableId, chartRef, opts) {
  const clickable = !!(opts && opts.clickable);
  const slices = (rows || []).map((r, i) => ({
    id: r.tag_id,
    label: r.name,
    value: Number(r.value || 0),
    color: r.tag_id === 0 ? "#6e7681" : (r.color || TAG_PALETTE[i % TAG_PALETTE.length]),
  })).filter((s) => s.value > 0);

  const chart = renderDoughnut(
    canvasId, chartRef, slices, currency, Number(total || 0),
    clickable ? (slice) => openTag(slice.id) : null
  );

  const tbody = document.querySelector(`#${tableId} tbody`);
  if (tbody) {
    tbody.innerHTML = "";
    slices.forEach((s, i) => {
      const row = (rows || []).find((r) => r.tag_id === s.id) || {};
      const tr = document.createElement("tr");
      if (clickable) tr.className = "clickable";
      // 構成比: スマホでは評価額の2行目、広い画面では列（valueWeightCellHtml参照）。
      // 4列を並べる方式は実機フォントの幅差で右端が切れる端末があるため
      const weightStr = row.weight != null ? row.weight + "%" : "";
      tr.innerHTML =
        `<td><span class="tag-chip small" style="--tag-color:${s.color}">` +
        `${escapeHtml(s.label)}</span></td>` +
        `<td class="num">${valueWeightCellHtml(s.value, currency, weightStr)}</td>` +
        `<td class="num">${dayChangeCellHtml(row, currency)}</td>` +
        `<td class="num weight-col">${weightStr || "—"}</td>` +
        (clickable ? `<td class="chev">›</td>` : "");
      tr.addEventListener("mouseenter", () => _setDoughnutActive(chart, i));
      tr.addEventListener("mouseleave", () => _setDoughnutActive(chart, null));
      if (clickable) tr.addEventListener("click", () => openTag(s.id));
      tbody.appendChild(tr);
    });
  }
  return chart;
}

function renderTagSummary() {
  const emptyEl = document.getElementById("tagsum-empty");
  const currency = currentCurrency();
  const rows = (_tagSummary && _tagSummary.by_tag) || [];
  emptyEl.classList.toggle("hidden", rows.some((r) => r.tag_id !== 0));
  _tagSumChart = _renderTagChartAndTable(
    rows, _tagSummary && _tagSummary.total_value, currency,
    "tagsum-chart", "tagsum-table", _tagSumChart, { clickable: true }
  );
}

/** タグ行/スライスのクリック先。未分類は「未配分の割当」へ誘導する。 */
function openTag(tagId) {
  if (tagId === 0) {
    _assignFilter = "unallocated";
    _syncAssignFilterChips();
    renderTagAssignList();
    document.getElementById("tag-assign-list").scrollIntoView({ behavior: "smooth" });
    return;
  }
  navigate("portfolios", "tag", { id: tagId });
}

async function showTagDetail(tagId) {
  const req = ++_pfDetailReq;
  showPortfolioDetailView();
  // 取得を待つ間に前のタグの内容を見せない。名前だけは手元の一覧から先に出す。
  const known = _tags.find((x) => String(x.id) === String(tagId));
  clearPortfolioDetail(known ? known.name : "");
  const loading = document.getElementById("pf-detail-loading");
  loading.classList.remove("hidden");
  const currency = currentCurrency();
  try {
    if (!_tags.length) {
      const tags = (await fetchJSON("/api/tags")).tags || [];
      if (req !== _pfDetailReq) return;
      _tags = tags;
    }
    const d = await fetchJSON(`/api/tags/${tagId}/holdings?currency=${currency}`);
    if (req !== _pfDetailReq) return;   // 追い越された応答は捨てる
    _pfDetail = d;
    renderPortfolioDetail({ tagView: true });
    loadPfHistoryChart(`tag:${tagId}`, null);
  } catch (e) {
    if (req !== _pfDetailReq) return;
    document.getElementById("pf-detail-name").textContent = t("pf.loadFailed");
    console.warn("[asset-summary] tag detail:", e);
  } finally {
    if (req === _pfDetailReq) loading.classList.add("hidden");
  }
}

function renderTagTable() {
  const tbody = document.querySelector("#tag-table tbody");
  tbody.innerHTML = "";
  const currency = currentCurrency();
  const valueByTag = {};
  ((_tagSummary && _tagSummary.by_tag) || []).forEach((r) => {
    valueByTag[r.tag_id] = r;
  });

  _tags.forEach((tag, i) => {
    const row = valueByTag[tag.id] || {};
    const color = tagColor(tag, i);
    const tr = document.createElement("tr");

    // 名前: その場で編集（変更を確定した時点で保存）
    const nameTd = document.createElement("td");
    const chip = document.createElement("span");
    chip.className = "tag-chip tag-chip-edit";
    chip.style.setProperty("--tag-color", color);
    const nameInput = document.createElement("input");
    nameInput.type = "text";
    nameInput.className = "tag-name-input";
    nameInput.value = tag.name;
    nameInput.addEventListener("change", async () => {
      const name = nameInput.value.trim();
      if (!name || name === tag.name) { nameInput.value = tag.name; return; }
      try {
        await apiCall(`/api/tags/${tag.id}`, "PUT", { name });
        loadPortfoliosPage();
      } catch (e) {
        showResult("tag-add-result", false, e.message);
        nameInput.value = tag.name;
      }
    });
    chip.appendChild(nameInput);
    nameTd.appendChild(chip);
    tr.appendChild(nameTd);

    // 色: パレットから選ぶか、カラーピッカーで自由に決める
    const colorTd = document.createElement("td");
    colorTd.className = "tag-color-cell";
    TAG_PALETTE.forEach((c) => {
      const sw = document.createElement("button");
      sw.className = "tag-swatch" + (c.toLowerCase() === String(color).toLowerCase() ? " on" : "");
      sw.style.background = c;
      sw.title = c;
      sw.addEventListener("click", () => setTagColor(tag.id, c));
      colorTd.appendChild(sw);
    });
    const picker = document.createElement("input");
    picker.type = "color";
    picker.className = "color-input tag-color-picker";
    picker.value = /^#[0-9a-f]{6}$/i.test(String(color)) ? color : "#2f81f7";
    picker.title = t("pf.tagColorCustom");
    picker.addEventListener("change", () => setTagColor(tag.id, picker.value));
    colorTd.appendChild(picker);
    tr.appendChild(colorTd);

    const rest = document.createElement("td");
    rest.className = "num";
    rest.textContent = String(tag.security_count ?? 0);
    tr.appendChild(rest);
    const valTd = document.createElement("td");
    valTd.className = "num";
    valTd.textContent = row.value != null ? fmtMoney(row.value, currency) : "—";
    tr.appendChild(valTd);
    const dayTd = document.createElement("td");
    dayTd.className = "num";
    dayTd.innerHTML = dayChangeCellHtml(row, currency);
    tr.appendChild(dayTd);
    const wTd = document.createElement("td");
    wTd.className = "num";
    wTd.textContent = row.weight != null ? row.weight + "%" : "—";
    tr.appendChild(wTd);

    const actions = document.createElement("td");
    actions.className = "row-actions";
    const del = document.createElement("button");
    del.className = "btn-link danger";
    del.textContent = t("btn.delete");
    del.addEventListener("click", () => {
      openConfirmDialog(
        t("pf.tagDeleteTitle"),
        t("pf.tagDeleteMsg", { name: tag.name }),
        async () => {
          await apiCall(`/api/tags/${tag.id}`, "DELETE");
          loadPortfoliosPage();
        }
      );
    });
    actions.appendChild(del);
    tr.appendChild(actions);
    tbody.appendChild(tr);
  });
}

/** タグ色の変更。ドーナツ・チップの色は保存後の再読込で揃う。 */
async function setTagColor(tagId, color) {
  try {
    await apiCall(`/api/tags/${tagId}`, "PUT", { color });
    hideResult("tag-add-result");
    loadPortfoliosPage();
  } catch (e) {
    showResult("tag-add-result", false, e.message);
  }
}

// ---- 銘柄へのタグ割当 ----

function renderTagAssignList() {
  const wrap = document.getElementById("tag-assign-list");
  const q = (document.getElementById("tag-assign-search").value || "").toLowerCase();
  wrap.innerHTML = "";
  if (!_tags.length) {
    wrap.innerHTML = `<p class="card-hint">${escapeHtml(t("pf.noTagsYet"))}</p>`;
    return;
  }
  // 未配分銘柄の評価額（警告帯と同じ情報源）。並び順と強調に使う。
  const unallocValue = {};
  ((_tagSummary && _tagSummary.unallocated) || []).forEach((u) => {
    unallocValue[u.security_id] = Number(u.unallocated_value || 0);
  });

  const _used = (sec) => (_tagAllocations[String(sec.id)] || [])
    .reduce((sum, a) => sum + Number(a.weight || 0), 0);

  let securities = _assignableAssets().filter((s) =>
    !q || (s.name || "").toLowerCase().includes(q) ||
    (s.code || "").toLowerCase().includes(q));
  if (_assignFilter === "unallocated") {
    securities = securities.filter((s) => _used(s) < 100);
  }
  // 未配分（かつ金額の大きい順）を先頭に出して、対応漏れに気づけるようにする
  securities = securities.slice().sort((a, b) => {
    const ua = _used(a) < 100, ub = _used(b) < 100;
    if (ua !== ub) return ua ? -1 : 1;
    if (ua && ub) return (unallocValue[b.id] || 0) - (unallocValue[a.id] || 0);
    return (a.name || "").localeCompare(b.name || "", "ja");
  });

  if (!securities.length) {
    wrap.innerHTML = `<p class="card-hint">${escapeHtml(t("pf.assignNone"))}</p>`;
    return;
  }

  securities.slice(0, 200).forEach((sec) => {
    const allocs = _tagAllocations[String(sec.id)] || [];
    const byTag = {};
    allocs.forEach((a) => { byTag[a.tag_id] = a.weight; });
    const used = allocs.reduce((sum, a) => sum + Number(a.weight || 0), 0);

    const row = document.createElement("div");
    row.className = "assign-row" + (used < 100 ? " unallocated" : "");
    const head = document.createElement("div");
    head.className = "assign-head";
    const amount = unallocValue[sec.id];
    head.innerHTML =
      `<span class="assign-name">${escapeHtml(sec.name)}</span>` +
      (sec.external ? csBadgeHtml() : "") +
      (sec.code && !sec.external ? `<span class="assign-code">${escapeHtml(sec.code)}</span>` : "") +
      (used < 100 && amount
        ? `<span class="assign-amount">${fmtMoney(amount, currentCurrency())}</span>` : "") +
      `<span class="assign-remain${used < 100 ? " warn" : ""}" data-remain="${sec.id}">` +
      `${_remainLabel(used)}</span>`;
    row.appendChild(head);

    const inputs = document.createElement("div");
    inputs.className = "assign-inputs";
    _tags.forEach((tag, i) => {
      const cell = document.createElement("label");
      cell.className = "assign-cell";
      cell.innerHTML =
        `<span class="tag-chip small" style="--tag-color:${tagColor(tag, i)}">` +
        `${escapeHtml(tag.name)}</span>`;
      const input = document.createElement("input");
      input.type = "number";
      input.min = "0";
      input.max = "100";
      input.step = "0.1";   // 8資産均等型の 12.5 / 37.5 等を手でも編集できるように
      input.className = "assign-weight";
      input.value = byTag[tag.id] != null ? String(Number(byTag[tag.id])) : "";
      input.placeholder = "0";
      input.dataset.securityId = sec.id;
      input.dataset.tagId = tag.id;
      input.addEventListener("input", () => _updateRemainLabel(sec.id));
      input.addEventListener("change", () => saveSecurityTags(sec.id));
      cell.appendChild(input);
      inputs.appendChild(cell);
    });
    row.appendChild(inputs);
    wrap.appendChild(row);
  });
  if (securities.length > 200) {
    const note = document.createElement("p");
    note.className = "card-hint";
    note.textContent = t("pf.assignTruncated", { count: securities.length });
    wrap.appendChild(note);
  }
}

/** タグを割り当てられる資産の一覧。AS の銘柄 + Crypto-Summary 由来のコイン。
 *
 * CS のコインは AS の DB に無いため /api/securities には出ない。タグ集計の
 * 結果（仮想保有を含む）から拾って、同じ割当 UI に並べる。
 */
function _assignableAssets() {
  const rows = (_securities || []).map((s) => ({
    id: s.id, name: s.name, code: s.code, external: false,
  }));
  const seen = new Set();
  ((_tagSummary && _tagSummary.holdings) || []).forEach((h) => {
    if (h.origin !== "crypto_summary" || seen.has(h.id)) return;
    seen.add(h.id);
    rows.push({ id: h.id, name: h.name, code: h.code, external: true });
  });
  return rows;
}

function _remainLabel(used) {
  const remain = 100 - used;
  if (Math.abs(remain) < 0.001) return t("pf.allocated");
  if (remain > 0) {
    return used === 0
      ? t("pf.unassigned")
      : t("pf.remaining", { pct: String(Math.round(remain * 100) / 100) });
  }
  return t("pf.over", { pct: String(Math.round(-remain * 100) / 100) });
}

function _collectWeights(securityId) {
  const inputs = document.querySelectorAll(
    `.assign-weight[data-security-id="${securityId}"]`
  );
  const allocations = [];
  let used = 0;
  inputs.forEach((el) => {
    const w = Number(el.value);
    if (el.value !== "" && isFinite(w) && w > 0) {
      allocations.push({ tag_id: Number(el.dataset.tagId), weight: w });
      used += w;
    }
  });
  return { allocations, used };
}

function _updateRemainLabel(securityId) {
  const { used } = _collectWeights(securityId);
  const el = document.querySelector(`[data-remain="${securityId}"]`);
  if (el) {
    el.textContent = _remainLabel(used);
    el.classList.toggle("over", used > 100.0001);
  }
}

async function saveSecurityTags(securityId) {
  const { allocations, used } = _collectWeights(securityId);
  if (used > 100.0001) return;   // 100%超は保存しない（ラベルで警告済み）
  // Crypto-Summary 由来の資産は AS の銘柄ではないので別のエンドポイントへ
  const key = String(securityId);
  const url = key.startsWith("cs:")
    ? `/api/asset-tags/${encodeURIComponent(key)}`
    : `/api/securities/${securityId}/tags`;
  try {
    await apiCall(url, "PUT", { allocations });
    _tagAllocations[String(securityId)] = allocations.map((a) => ({
      tag_id: a.tag_id, weight: String(a.weight),
    }));
    // 合計が変わるのでサマリーとタグ表を更新
    const currency = currentCurrency();
    _tagSummary = await fetchJSON(`/api/tag-summary?currency=${currency}`);
    _tags = (await fetchJSON("/api/tags")).tags || [];
    renderTagSummary();
    renderUnallocBanner();
    renderTagTable();
    _updateRemainLabel(securityId);
    loadPortfolioList();
  } catch (e) {
    alert(e.message);
  }
}

// ---- タグの自動配分（ルールベース） ----

/** チェック可能なのは適用の意味がある行だけ。 */
function _autoAllocSelectable(s) {
  return s.status === "new" || s.status === "change";
}

/**
 * 既定チェックは「新規かつクラス推定でない」行のみ。
 * クラス推定(fallback)は 2840 を国内株式にした推論そのものなので、
 * 新規であっても自動では選ばない — 必ず人が見てから適用する。
 * 変更(change)も手作業の配分を誤って潰さないよう既定は未チェック。
 */
function _autoAllocDefaultChecked(s) {
  return s.status === "new" && !s.fallback;
}

function _autoAllocBadgeHtml(s) {
  const badge = (cls, key) =>
    `<span class="${cls}"><span class="diff-status">${escapeHtml(t(key))}</span></span>`;
  if (s.status === "new") return badge("diff-new", "pf.autoalloc.badgeNew");
  if (s.status === "change") return badge("diff-changed", "pf.autoalloc.badgeChange");
  if (s.status === "missing-tag") return badge("diff-missing", "pf.autoalloc.badgeMissingTag");
  if (s.status === "no-rule") return badge("diff-unchanged", "pf.autoalloc.badgeNoRule");
  return badge("diff-unchanged", "pf.autoalloc.badgeUnchanged");
}

/** [{tag_id, name, weight}] → タグチップ列。空は「—」。 */
function _autoAllocChipsHtml(allocs) {
  if (!allocs || !allocs.length) return '<span class="muted">—</span>';
  const colorById = {};
  (_tags || []).forEach((tag, i) => { colorById[tag.id] = tagColor(tag, i); });
  return allocs.map((a) =>
    `<span class="tag-chip small" style="--tag-color:${colorById[a.tag_id] || "#8b949e"}">` +
    `${escapeHtml(a.name || String(a.tag_id))} ${escapeHtml(String(Number(a.weight)))}%</span>`
  ).join(" ");
}

function _autoAllocReasonHtml(s) {
  if (!s.rule_id) return '<span class="muted">—</span>';
  let text;
  if (s.matched_by === "code") {
    text = t("pf.autoalloc.byCode", { value: s.matched_value });
  } else if (s.matched_by === "keyword") {
    text = t("pf.autoalloc.byKeyword", { value: s.matched_value });
  } else if (s.matched_by === "symbol") {
    text = t("pf.autoalloc.bySymbol", { value: s.matched_value });
  } else {
    text = t("pf.autoalloc.byClass", { value: classLabel(s.matched_value) });
  }
  let html = `<span class="muted">${escapeHtml(text)}</span>`;
  if (s.fallback) {
    html += ` <span class="autoalloc-warn">${escapeHtml(t("pf.autoalloc.fallbackNote"))}</span>`;
  }
  if (s.fund_shape_warning) {
    html += ` <span class="autoalloc-warn strong">${escapeHtml(t("pf.autoalloc.fundShapeWarn"))}</span>`;
  }
  return html;
}

function _autoAllocVisible() {
  const list = _autoAllocSuggestions || [];
  if (_autoAllocFilter === "all") return list;
  // 「提案のみ」: 既にルール通り／ルール対象外の行を隠す。
  // missing-tag はタグ作成というユーザー対応が要るので隠さない。
  return list.filter((s) => s.status !== "unchanged" && s.status !== "no-rule");
}

function _syncAutoAllocFilterChips() {
  document.getElementById("autoalloc-filter-actionable")
    .classList.toggle("active", _autoAllocFilter === "actionable");
  document.getElementById("autoalloc-filter-all")
    .classList.toggle("active", _autoAllocFilter === "all");
}

function renderAutoAllocResults() {
  const box = document.getElementById("autoalloc-results");
  const tbody = document.querySelector("#autoalloc-table tbody");
  const summary = document.getElementById("autoalloc-summary");
  if (!box || !tbody || !summary) return;

  const all = _autoAllocSuggestions || [];
  if (!all.length) {
    box.classList.add("hidden");
    return;
  }
  box.classList.remove("hidden");

  const counts = { new: 0, change: 0, unchanged: 0, "no-rule": 0, "missing-tag": 0 };
  let fallbacks = 0;
  all.forEach((s) => {
    if (counts[s.status] !== undefined) counts[s.status] += 1;
    if (s.fallback) fallbacks += 1;
  });
  summary.innerHTML =
    `<span class="section-chip autolink-chip autolink-chip-auto">${escapeHtml(t("pf.autoalloc.chipNew", { count: counts.new }))}</span>` +
    `<span class="section-chip autolink-chip autoalloc-chip-change">${escapeHtml(t("pf.autoalloc.chipChange", { count: counts.change }))}</span>` +
    `<span class="section-chip autolink-chip autolink-chip-none">${escapeHtml(t("pf.autoalloc.chipUnchanged", { count: counts.unchanged }))}</span>` +
    `<span class="section-chip autolink-chip autolink-chip-none">${escapeHtml(t("pf.autoalloc.chipNoRule", { count: counts["no-rule"] }))}</span>` +
    `<span class="section-chip autolink-chip autolink-chip-cand">${escapeHtml(t("pf.autoalloc.chipFallback", { count: fallbacks }))}</span>`;

  // タグ未作成の行があれば警告（どのタグを作ればよいかを列挙）
  const missing = [...new Set(all.flatMap((s) => s.missing_tags || []))];
  renderWarningsInto(
    document.getElementById("autoalloc-warnings"),
    missing.length ? [t("pf.autoalloc.missingTagWarn", { names: missing.join(" / ") })] : []
  );

  const visible = _autoAllocVisible();
  const empty = document.getElementById("autoalloc-empty");
  const tableWrap = document.querySelector("#autoalloc-table").closest(".table-wrap");
  empty.classList.toggle("hidden", visible.length > 0);
  tableWrap.classList.toggle("hidden", visible.length === 0);

  tbody.innerHTML = "";
  visible.forEach((s) => {
    if (s._checked === undefined) s._checked = _autoAllocDefaultChecked(s);
    const tr = document.createElement("tr");
    if (s.status === "unchanged") tr.className = "diff-unchanged";

    const tdCheck = document.createElement("td");
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.className = "autoalloc-check";
    if (_autoAllocSelectable(s)) {
      cb.checked = !!s._checked;
    } else {
      cb.disabled = true;
      cb.checked = false;
    }
    cb.addEventListener("change", () => { s._checked = cb.checked; });
    tdCheck.appendChild(cb);

    const tdName = document.createElement("td");
    tdName.innerHTML =
      `<span class="asset-label">${escapeHtml(s.name)}</span>` +
      (s.external ? csBadgeHtml() : "") +
      (s.code ? ` <code class="asset-code">${escapeHtml(s.code)}</code>` : "");

    const tdBadge = document.createElement("td");
    tdBadge.innerHTML = _autoAllocBadgeHtml(s);

    const tdCurrent = document.createElement("td");
    tdCurrent.innerHTML = _autoAllocChipsHtml(s.current);

    const tdSuggested = document.createElement("td");
    tdSuggested.innerHTML = _autoAllocChipsHtml(s.suggested);

    const tdReason = document.createElement("td");
    tdReason.innerHTML = _autoAllocReasonHtml(s);

    tr.appendChild(tdCheck);
    tr.appendChild(tdName);
    tr.appendChild(tdBadge);
    tr.appendChild(tdCurrent);
    tr.appendChild(tdSuggested);
    tr.appendChild(tdReason);
    tbody.appendChild(tr);
  });

  // 全選択チェックの状態を表示中の選択可能行に合わせる
  const selectable = visible.filter(_autoAllocSelectable);
  const checkAll = document.getElementById("autoalloc-check-all");
  checkAll.disabled = selectable.length === 0;
  checkAll.checked = selectable.length > 0 && selectable.every((s) => s._checked);
}

async function runAutoAllocSuggest() {
  const btn = document.getElementById("autoalloc-suggest-btn");
  hideResult("autoalloc-suggest-result");
  hideResult("autoalloc-apply-result");
  btn.disabled = true;
  try {
    const d = await apiCall("/api/tag-rules/suggest", "POST", null);
    _autoAllocSuggestions = (d && d.suggestions) || [];
    renderAutoAllocResults();
  } catch (e) {
    showResult("autoalloc-suggest-result", false,
      t("pf.autoalloc.suggestFail", { error: e.message }));
  } finally {
    btn.disabled = false;
  }
}

async function applyAutoAllocSelected() {
  hideResult("autoalloc-apply-result");
  const ids = (_autoAllocSuggestions || [])
    .filter((s) => _autoAllocSelectable(s) && s._checked)
    .map((s) => s.security_id);
  if (!ids.length) {
    showResult("autoalloc-apply-result", false, t("pf.autoalloc.noneSelected"));
    return;
  }
  const applyBtn = document.getElementById("autoalloc-apply-btn");
  const suggestBtn = document.getElementById("autoalloc-suggest-btn");
  applyBtn.disabled = true;
  suggestBtn.disabled = true;
  const origText = applyBtn.textContent;
  applyBtn.textContent = t("pf.autoalloc.applying");
  try {
    const d = await apiCall("/api/tag-rules/apply", "POST", { security_ids: ids });
    showResult("autoalloc-apply-result", true,
      t("pf.autoalloc.applyDone", { count: d && d.applied != null ? d.applied : ids.length }));
    if (d && d.warnings && d.warnings.length) {
      renderWarningsInto(document.getElementById("autoalloc-warnings"), d.warnings);
    }
    // 適用はサーバ側で再判定されるため、提案も取り直して整合させる
    const refreshed = await apiCall("/api/tag-rules/suggest", "POST", null);
    _autoAllocSuggestions = (refreshed && refreshed.suggestions) || [];
    renderAutoAllocResults();
    // サマリー・タグ表・割当リスト・未配分バナーに反映
    await loadPortfoliosPage();
  } catch (e) {
    showResult("autoalloc-apply-result", false,
      t("pf.autoalloc.applyFail", { error: e.message }));
  } finally {
    applyBtn.disabled = false;
    applyBtn.textContent = origText;
    suggestBtn.disabled = false;
  }
}

// ---- ポートフォリオ一覧 ----

async function loadPortfolioList() {
  const currency = currentCurrency();
  const tbody = document.querySelector("#pf-table tbody");
  const emptyEl = document.getElementById("pf-empty");
  let list = [];
  try {
    list = (await fetchJSON(`/api/portfolios?currency=${currency}`)).portfolios || [];
  } catch (e) {
    console.warn("[asset-summary] portfolios:", e);
  }
  tbody.innerHTML = "";
  emptyEl.classList.toggle("hidden", list.length > 0);
  const tagName = {};
  _tags.forEach((tg, i) => { tagName[tg.id] = { name: tg.name, color: tagColor(tg, i) }; });

  list.forEach((p) => {
    const tr = document.createElement("tr");
    tr.className = "clickable";
    const chips = (p.tag_ids || []).map((id) => {
      const info = tagName[id];
      return info
        ? `<span class="tag-chip small" style="--tag-color:${info.color}">${escapeHtml(info.name)}</span>`
        : "";
    }).join("");
    const extra = (p.include_security_ids || []).length
      ? `<span class="assign-code">${t("pf.plusIndividual", { count: p.include_security_ids.length })}</span>`
      : "";
    tr.innerHTML =
      `<td>${escapeHtml(p.name)}${p.note ? `<div class="assign-code">${escapeHtml(p.note)}</div>` : ""}</td>` +
      `<td>${chips || `<span class="assign-code">${escapeHtml(t("pf.noTagFilter"))}</span>`}${extra}</td>` +
      `<td class="num">${p.holding_count}</td>` +
      `<td class="num">${fmtMoney(p.value, currency)}</td>` +
      `<td class="num">${dayChangeCellHtml(p, currency)}</td>` +
      `<td class="num">${plAmountHtml(p.pl, currency)}</td>` +
      `<td class="num">${plPctHtml(p.pl_pct)}</td>`;
    tr.addEventListener("click", (e) => {
      if (e.target.closest("button")) return;
      navigate("portfolios", "detail", { id: p.id });
    });
    const actions = document.createElement("td");
    actions.className = "row-actions";
    const edit = document.createElement("button");
    edit.className = "btn-link";
    edit.textContent = t("btn.edit");
    edit.addEventListener("click", (e) => { e.stopPropagation(); openPortfolioForm(p); });
    const del = document.createElement("button");
    del.className = "btn-link danger";
    del.textContent = t("btn.delete");
    del.addEventListener("click", (e) => {
      e.stopPropagation();
      openConfirmDialog(
        t("pf.deleteTitle"),
        t("pf.deleteMsg", { name: p.name }),
        async () => {
          await apiCall(`/api/portfolios/${p.id}`, "DELETE");
          loadPortfolioList();
        }
      );
    });
    actions.appendChild(edit);
    actions.appendChild(del);
    tr.appendChild(actions);
    tbody.appendChild(tr);
  });
}

function openPortfolioForm(portfolio) {
  _pfEditingId = portfolio ? portfolio.id : null;
  _pfFormTagIds = new Set(portfolio ? portfolio.tag_ids || [] : []);
  document.getElementById("pf-name").value = portfolio ? portfolio.name : "";
  document.getElementById("pf-note").value = (portfolio && portfolio.note) || "";
  hideResult("pf-form-result");
  renderPfTagPicker();
  document.getElementById("pf-form").classList.remove("hidden");
}

function renderPfTagPicker() {
  const wrap = document.getElementById("pf-tag-picker");
  wrap.innerHTML = "";
  if (!_tags.length) {
    wrap.innerHTML = `<span class="card-hint">${escapeHtml(t("pf.noTagsYet"))}</span>`;
    return;
  }
  _tags.forEach((tag, i) => {
    const label = document.createElement("label");
    label.className = "tag-pick";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = _pfFormTagIds.has(tag.id);
    cb.addEventListener("change", () => {
      if (cb.checked) _pfFormTagIds.add(tag.id);
      else _pfFormTagIds.delete(tag.id);
    });
    label.appendChild(cb);
    const chip = document.createElement("span");
    chip.className = "tag-chip small";
    chip.style.setProperty("--tag-color", tagColor(tag, i));
    chip.textContent = tag.name;
    label.appendChild(chip);
    wrap.appendChild(label);
  });
}

async function savePortfolio() {
  const name = document.getElementById("pf-name").value.trim();
  if (!name) {
    showResult("pf-form-result", false, t("pf.nameRequired"));
    return;
  }
  const body = {
    name,
    note: document.getElementById("pf-note").value.trim(),
    tag_ids: Array.from(_pfFormTagIds),
  };
  try {
    if (_pfEditingId) await apiCall(`/api/portfolios/${_pfEditingId}`, "PUT", body);
    else await apiCall("/api/portfolios", "POST", body);
    document.getElementById("pf-form").classList.add("hidden");
    loadPortfolioList();
  } catch (e) {
    showResult("pf-form-result", false, e.message);
  }
}

// ---- ポートフォリオ詳細 ----

async function showPortfolioDetail(id) {
  const req = ++_pfDetailReq;
  showPortfolioDetailView();
  clearPortfolioDetail();
  const loading = document.getElementById("pf-detail-loading");
  loading.classList.remove("hidden");
  const currency = currentCurrency();
  try {
    if (!_tags.length) {
      const tags = (await fetchJSON("/api/tags")).tags || [];
      if (req !== _pfDetailReq) return;
      _tags = tags;
    }
    const d = await fetchJSON(`/api/portfolios/${id}?currency=${currency}`);
    if (req !== _pfDetailReq) return;   // 追い越された応答は捨てる
    _pfDetail = d;
    renderPortfolioDetail({ tagView: false });
    loadPfHistoryChart(`portfolio:${id}`, null);
  } catch (e) {
    if (req !== _pfDetailReq) return;
    document.getElementById("pf-detail-name").textContent = t("pf.loadFailed");
    console.warn("[asset-summary] portfolio detail:", e);
  } finally {
    if (req === _pfDetailReq) loading.classList.add("hidden");
  }
}

function renderPortfolioDetail(opts) {
  const d = _pfDetail;
  if (!d) return;
  if (opts !== undefined) _pfDetailOpts = opts;
  const tagView = !!(_pfDetailOpts && _pfDetailOpts.tagView);
  const currency = d.currency;
  const p = d.portfolio || {};
  document.getElementById("pf-detail-name").textContent = p.name || "";
  document.getElementById("pf-detail-meta").textContent = tagView
    ? t("pf.tagViewHint")
    : (p.note || "");
  // タグ1つ分の内訳では「タグ別」カードは自明なので隠す
  const tagCard = document.querySelector('#pf-detail-view [data-card="pf-tag"]');
  if (tagCard) tagCard.classList.toggle("hidden", tagView);
  renderDetailHero("pf-detail", d, currency);

  if (!tagView) {
    _pfTagChart = _renderTagChartAndTable(
      d.by_tag, d.total_value, currency, "pf-tag-chart", "pf-tag-table", _pfTagChart
    );
  }

  _renderGroupTable("pf-currency-table", d.by_currency, currency);
  _renderGroupTable("pf-class-table", d.by_class, currency,
    (r) => navigate("classes", "detail", { name: r.key }));
  _renderGroupTable("pf-account-table", d.by_account, currency,
    (r) => navigate("accounts", "detail", { name: r.key }));

  // 構成銘柄は保有一覧と同じ表・同じ遷移にする（行クリックで銘柄詳細へ）。
  // タグ・計上額はこの画面固有なので extraCols で足す。
  document
    .querySelectorAll("#pf-holdings-table .pf-tag-col")
    .forEach((el) => el.classList.toggle("hidden", tagView));
  const extraCols = [];
  if (!tagView) {
    extraCols.push({
      detailLabel: t("pf.tags"),
      cellClass: "pf-tag-col",
      render: (h) => (h.tags || []).map((tg) => {
        const idx = _tags.findIndex((x) => x.id === tg.id);
        const color = tagColor(_tags[idx], idx < 0 ? 0 : idx);
        const w = Number(tg.weight);
        return `<span class="tag-chip small" style="--tag-color:${color}">` +
          `${escapeHtml(tg.name)}${w < 100 ? ` ${w}%` : ""}</span>`;
      }).join("") || '<span class="muted">—</span>',
    });
  }
  extraCols.push({
    detailLabel: t("pf.countedValueRatio"),
    render: (h) => amountRatioCellHtml(h.portfolio_value, h.portfolio_ratio, currency),
  });
  renderHoldingsRows(
    document.querySelector("#pf-holdings-table tbody"),
    sortHoldingRows(d.holdings || [], _pfSort.key, _pfSort.dir),
    currency, { extraCols }
  );
  _syncSortIndicators("pf-holdings-table", _pfSort);
  applyI18n();
}

/**
 * 「◯◯別」の内訳テーブル。onRow を渡すと行クリックでその画面へ飛べる
 * （ダッシュボードのクラス別・タグ別と同じ操作感にするため）。
 */
function _renderGroupTable(tableId, rows, currency, onRow) {
  const tbody = document.querySelector(`#${tableId} tbody`);
  if (!tbody) return;
  tbody.innerHTML = "";
  (rows || []).forEach((r) => {
    const tr = document.createElement("tr");
    if (onRow) tr.className = "clickable";
    const weightStr = r.weight != null ? r.weight + "%" : "";
    tr.innerHTML =
      `<td>${escapeHtml(r.label || r.key || "")}${onRow ? ' <span class="row-arrow">›</span>' : ""}</td>` +
      `<td class="num">${valueWeightCellHtml(r.value, currency, weightStr)}</td>` +
      `<td class="num">${dayChangeCellHtml(r, currency)}</td>` +
      `<td class="num weight-col">${weightStr || "—"}</td>`;
    if (onRow) tr.addEventListener("click", () => onRow(r));
    tbody.appendChild(tr);
  });
  if (!(rows || []).length) {
    tbody.innerHTML = `<tr><td colspan="4" class="muted">${t("label.noData")}</td></tr>`;
  }
}

// ---- 汎用ドーナツ（クラス別ドーナツと同じ見た目・中央テキスト付き） ----

function renderDoughnut(canvasId, chartRef, slices, currency, total, onSlice) {
  const ctx = document.getElementById(canvasId);
  if (!ctx) return null;
  if (chartRef) chartRef.destroy();
  if (!slices.length) {
    ctx.getContext("2d").clearRect(0, 0, ctx.width, ctx.height);
    return null;
  }
  const gSlices = groupDonutSlices(slices, total);
  const chart = new Chart(ctx, {
    type: "doughnut",
    data: {
      labels: gSlices.map((s) => s.label),
      datasets: [{
        data: gSlices.map((s) => s.value),
        backgroundColor: gSlices.map((s) => s.color),
        borderWidth: 0,
        hoverOffset: 6,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      cutout: "68%",
      layout: { padding: _donutPadding() },
      // リサイズやレイアウト確定で寸法が変わっても余白を取り直す
      onResize(c) { c.options.layout.padding = _donutPadding(); },
      plugins: { legend: { display: false }, tooltip: { enabled: false } },
      onHover(evt, elements) {
        const idx = elements.length ? elements[0].index : null;
        if (chart.$activeIndex !== idx) {
          chart.$activeIndex = idx;
          chart.draw();
        }
        if (onSlice && evt.native && evt.native.target) {
          const s = idx == null ? null : (chart.$slices || [])[idx];
          evt.native.target.style.cursor =
            s && !s.isOthers && !_isTouchOnly() ? "pointer" : "default";
        }
      },
      onClick(evt, elements) {
        // タップ端末では遷移しない（中央テキストに内訳が出るだけにする）。
        // 「その他」スライスは遷移先が無いのでどの端末でも遷移しない
        if (!onSlice || !elements.length || _isTouchOnly()) return;
        const s = (chart.$slices || [])[elements[0].index];
        if (s && !s.isOthers) onSlice(s);
      },
    },
    plugins: [sliceLabelPlugin, centerTextPlugin],
  });
  chart.$currency = currency;
  chart.$total = total;
  chart.$activeIndex = null;
  chart.$slices = gSlices;
  chart.$rowToSlice = _rowToSliceMap(gSlices);
  return chart;
}

function _setDoughnutActive(chart, idx) {
  if (!chart) return;
  const g = idx == null ? null : (chart.$rowToSlice || [])[idx];
  const next = g == null ? null : g;
  if (chart.$activeIndex !== next) {
    chart.$activeIndex = next;
    chart.draw();
  }
}

// ---- イベント配線 ----

document.getElementById("pf-back").addEventListener("click", () => navigate("portfolios"));
document.getElementById("pf-new-btn").addEventListener("click", () => openPortfolioForm(null));
document.getElementById("pf-save").addEventListener("click", savePortfolio);
document.getElementById("pf-cancel").addEventListener("click", () => {
  document.getElementById("pf-form").classList.add("hidden");
});
document.getElementById("tag-assign-search").addEventListener("input", renderTagAssignList);
document.getElementById("assign-filter-all").addEventListener("click", () => {
  _assignFilter = "all";
  _syncAssignFilterChips();
  renderTagAssignList();
});
document.getElementById("assign-filter-unalloc").addEventListener("click", () => {
  _assignFilter = "unallocated";
  _syncAssignFilterChips();
  renderTagAssignList();
});
document.getElementById("autoalloc-suggest-btn").addEventListener("click", runAutoAllocSuggest);
document.getElementById("autoalloc-apply-btn").addEventListener("click", applyAutoAllocSelected);
document.getElementById("autoalloc-filter-actionable").addEventListener("click", () => {
  _autoAllocFilter = "actionable";
  _syncAutoAllocFilterChips();
  renderAutoAllocResults();
});
document.getElementById("autoalloc-filter-all").addEventListener("click", () => {
  _autoAllocFilter = "all";
  _syncAutoAllocFilterChips();
  renderAutoAllocResults();
});
document.getElementById("autoalloc-check-all").addEventListener("change", (e) => {
  const checked = e.target.checked;
  _autoAllocVisible().forEach((s) => {
    if (_autoAllocSelectable(s)) s._checked = checked;
  });
  renderAutoAllocResults();
});
document.getElementById("tag-add").addEventListener("click", async () => {
  const nameEl = document.getElementById("tag-new-name");
  const name = nameEl.value.trim();
  if (!name) {
    showResult("tag-add-result", false, t("pf.tagNameRequired"));
    return;
  }
  try {
    await apiCall("/api/tags", "POST", {
      name,
      color: document.getElementById("tag-new-color").value,
    });
    nameEl.value = "";
    hideResult("tag-add-result");
    loadPortfoliosPage();
  } catch (e) {
    showResult("tag-add-result", false, e.message);
  }
});

// ---- ダッシュボードのレイアウト編集 イベント ----

document.getElementById("dash-layout-btn").addEventListener("click", () => {
  const panel = document.getElementById("dash-layout-panel");
  const opening = panel.classList.contains("hidden");
  if (opening) renderDashLayoutPanel();
  panel.classList.toggle("hidden", !opening);
});
document.getElementById("dash-layout-close").addEventListener("click", () => {
  document.getElementById("dash-layout-panel").classList.add("hidden");
});
document.getElementById("dash-layout-reset").addEventListener("click", () => {
  saveDashboardLayout(
    Object.keys(DASH_WIDGET_LABELS).map((id) => ({ id, visible: id !== "tags" }))
  );
});

// ---- 認証（任意有効化のログインゲート） ----

function _showLoginScreen() {
  const el = document.getElementById("login-screen");
  if (el) el.classList.remove("hidden");
}

async function _checkAuth() {
  // /auth/me は認証オフ時もスタブが応答する。失敗時はゲート無しとみなして続行
  // （ローカル開発でサーバーが古い場合など）。
  try {
    return await (await fetch(apiUrl("/auth/me"))).json();
  } catch (_) {
    return { authenticated: true, enabled: false };
  }
}

// ---- 初期化 ----

(async () => {
  const saved = localStorage.getItem("as_currency");
  if (saved) document.getElementById("currency").value = saved;

  const me = await _checkAuth();
  if (me.enabled && !me.authenticated) {
    applyI18n();
    if (new URLSearchParams(location.search).get("login") === "denied") {
      document.getElementById("login-denied").classList.remove("hidden");
    }
    _showLoginScreen();
    return;  // 未ログインでは何も読み込まない（API は全て 401）
  }
  if (me.enabled) {
    const logout = document.getElementById("logout-btn");
    if (logout) {
      logout.classList.remove("hidden");
      if (me.email) logout.title = me.email;
    }
  }

  await loadMeta();
  _syncThemeBtn();
  _syncMaskBtn();
  _syncLangBtn();
  _syncCurrencyLabels();
  applyI18n();
  // コインアイコンは裏で取得（待たない — 2回目以降の描画から出れば十分）
  _ensureCsCoinIcons();
  // 初期描画は現在の URL ハッシュから（直リンク・リロードで状態復元）
  router();
})();
