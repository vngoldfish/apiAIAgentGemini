

// Default to Gemini-API gateway port (8000). Override in Options if needed.
let _syncPort = 8000;
let _syncUrl = `http://127.0.0.1:${_syncPort}`;

// Last Gemini cookie-push result (surfaced in popup)
let _geminiCookieMeta = {
    lastPushAt: null,
    lastOk: false,
    lastError: null,
    hasPsid: false,
    hasPsidts: false,
    maskedPsid: null,
    providerId: null,
    autoSync: true,
};

// Fingerprint of last successfully pushed cookie pair (skip redundant uploads)
let _lastPushedFingerprint = null;
let _pushInFlight = null;
let _cookieChangeTimer = null;
let _serverWantsCookies = true; // true until server confirms it has an active extension provider

function _updateSyncPort(port) {
    if (port && typeof port === "number" && port >= 1 && port <= 65535) {
        _syncPort = port;
        _syncUrl = `http://127.0.0.1:${_syncPort}`;
    }
}

chrome.storage.local.get(["syncPort", "geminiCookieMeta", "lastPushedFingerprint"], (data) => {
    if (data && data.syncPort) {
        _updateSyncPort(data.syncPort);
    }
    if (data && data.geminiCookieMeta) {
        _geminiCookieMeta = { ..._geminiCookieMeta, ...data.geminiCookieMeta };
    }
    if (data && data.lastPushedFingerprint) {
        _lastPushedFingerprint = data.lastPushedFingerprint;
    }
});

chrome.storage.onChanged.addListener((changes, areaName) => {
    if (areaName === "local" && changes.syncPort) {
        _updateSyncPort(changes.syncPort.newValue);
        // Port changed → re-push cookies to the new backend
        _serverWantsCookies = true;
        _lastPushedFingerprint = null;
        _scheduleAutoCookieSync(500);
    }
});

/**
 * Read Gemini Web cookies (__Secure-1PSID / __Secure-1PSIDTS) from the browser jar.
 * Works for the currently signed-in Google/Gemini session without opening a tab
 * (as long as the account is logged in somewhere in this Chrome profile).
 */
async function _readGeminiCookies() {
    const wanted = new Set(["__Secure-1PSID", "__Secure-1PSIDTS"]);
    const found = {};

    // Broadest first: all cookies for google properties in this profile
    try {
        const all = await chrome.cookies.getAll({});
        for (const c of all) {
            if (!c || !c.name || !c.value) continue;
            if (!wanted.has(c.name)) continue;
            const domain = (c.domain || "").toLowerCase();
            if (
                domain.includes("google.com") ||
                domain.includes("gemini.google") ||
                domain === "google.com" ||
                domain.endsWith(".google.com")
            ) {
                // Prefer longer / newer-looking values if duplicates exist
                if (!found[c.name] || (c.value && c.value.length > found[c.name].length)) {
                    found[c.name] = c.value;
                }
            }
        }
    } catch (e) { /* fall through */ }

    const domains = [".google.com", "google.com", "gemini.google.com", ".gemini.google.com"];
    for (const domain of domains) {
        try {
            const list = await chrome.cookies.getAll({ domain });
            for (const c of list) {
                if (wanted.has(c.name) && c.value) {
                    if (!found[c.name] || c.value.length > found[c.name].length) {
                        found[c.name] = c.value;
                    }
                }
            }
        } catch (e) { /* ignore domain errors */ }
    }

    // Fallback: URL-scoped lookup
    if (!found["__Secure-1PSID"] || !found["__Secure-1PSIDTS"]) {
        for (const url of ["https://gemini.google.com", "https://www.google.com", "https://accounts.google.com"]) {
            try {
                if (!found["__Secure-1PSID"]) {
                    const c = await chrome.cookies.get({ url, name: "__Secure-1PSID" });
                    if (c && c.value) found["__Secure-1PSID"] = c.value;
                }
                if (!found["__Secure-1PSIDTS"]) {
                    const c = await chrome.cookies.get({ url, name: "__Secure-1PSIDTS" });
                    if (c && c.value) found["__Secure-1PSIDTS"] = c.value;
                }
            } catch (e) { /* ignore */ }
        }
    }

    return {
        psid: found["__Secure-1PSID"] || "",
        psidts: found["__Secure-1PSIDTS"] || "",
        cookies: found,
    };
}

function _maskCookie(value) {
    if (!value || value.length < 12) return value ? "****" : null;
    return `${value.slice(0, 8)}...${value.slice(-6)}`;
}

function _cookieFingerprint(psid, psidts) {
    return `${psid || ""}||${psidts || ""}`;
}

function _scheduleAutoCookieSync(delayMs = 800) {
    if (_cookieChangeTimer) clearTimeout(_cookieChangeTimer);
    _cookieChangeTimer = setTimeout(() => {
        _cookieChangeTimer = null;
        _pushGeminiCookies({ reason: "auto" }).catch(() => { });
    }, delayMs);
}

/**
 * Push Gemini cookies from the current browser session to the local Gemini-API server.
 * Auto-applies as provider "Extension Auto".
 *
 * @param {object} opts
 * @param {boolean} opts.force - upload even if fingerprint unchanged
 * @param {string} opts.reason - for logging / meta
 */
async function _pushGeminiCookies({ force = false, reason = "manual" } = {}) {
    // Coalesce concurrent pushes into one in-flight request
    if (_pushInFlight) {
        if (force) {
            // wait for current then force again
            try { await _pushInFlight; } catch (e) { /* ignore */ }
        } else {
            return _pushInFlight;
        }
    }

    _pushInFlight = (async () => {
        try {
            const jar = await _readGeminiCookies();
            if (!jar.psid) {
                _geminiCookieMeta = {
                    ..._geminiCookieMeta,
                    lastPushAt: Date.now(),
                    lastOk: false,
                    lastError: "No __Secure-1PSID found. Login to https://gemini.google.com in this Chrome profile.",
                    hasPsid: false,
                    hasPsidts: false,
                    maskedPsid: null,
                };
                try { await chrome.storage.local.set({ geminiCookieMeta: _geminiCookieMeta }); } catch (e) { }
                return { ok: false, error: _geminiCookieMeta.lastError, reason };
            }

            const fp = _cookieFingerprint(jar.psid, jar.psidts);
            // Skip upload if cookies unchanged and server already has them (unless forced / server requests)
            if (
                !force &&
                !_serverWantsCookies &&
                _geminiCookieMeta.lastOk &&
                _lastPushedFingerprint === fp
            ) {
                return {
                    ok: true,
                    skipped: true,
                    reason,
                    data: { status: "unchanged", masked_psid: _maskCookie(jar.psid) },
                };
            }

            const extId = await getInstanceId();
            const body = {
                psid: jar.psid,
                psidts: jar.psidts || "",
                secure_1psid: jar.psid,
                secure_1psidts: jar.psidts || "",
                name: "Extension Auto",
                auto_apply: true,
                cookies: jar.cookies,
                reason,
            };

            const response = await fetch(`${_syncUrl}/sync/gemini-cookies`, {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                    "X-Ext-Id": extId,
                },
                body: JSON.stringify(body),
                signal: AbortSignal.timeout(20000),
            });

            let data = null;
            try { data = await response.json(); } catch (e) { data = null; }

            if (!response.ok) {
                const detail = (data && (data.detail || data.message)) || `HTTP ${response.status}`;
                _geminiCookieMeta = {
                    lastPushAt: Date.now(),
                    lastOk: false,
                    lastError: String(detail),
                    hasPsid: true,
                    hasPsidts: !!jar.psidts,
                    maskedPsid: _maskCookie(jar.psid),
                    providerId: null,
                    autoSync: true,
                };
                try { await chrome.storage.local.set({ geminiCookieMeta: _geminiCookieMeta }); } catch (e) { }
                // Server may have restarted — keep trying
                _serverWantsCookies = true;
                return { ok: false, error: _geminiCookieMeta.lastError, status: response.status, reason };
            }

            _lastPushedFingerprint = fp;
            _serverWantsCookies = false;
            _geminiCookieMeta = {
                lastPushAt: Date.now(),
                lastOk: true,
                lastError: null,
                hasPsid: true,
                hasPsidts: !!jar.psidts,
                maskedPsid: (data && data.masked_psid) || _maskCookie(jar.psid),
                providerId: (data && data.provider && data.provider.id) || null,
                autoSync: true,
            };
            try {
                await chrome.storage.local.set({
                    geminiCookieMeta: _geminiCookieMeta,
                    lastPushedFingerprint: _lastPushedFingerprint,
                });
            } catch (e) { }
            return { ok: true, data, reason, skipped: !!(data && data.unchanged) };
        } catch (e) {
            _geminiCookieMeta = {
                ..._geminiCookieMeta,
                lastPushAt: Date.now(),
                lastOk: false,
                lastError: e && e.message ? e.message : String(e),
                autoSync: true,
            };
            try { await chrome.storage.local.set({ geminiCookieMeta: _geminiCookieMeta }); } catch (err) { }
            _serverWantsCookies = true;
            return { ok: false, error: _geminiCookieMeta.lastError, reason };
        } finally {
            _pushInFlight = null;
        }
    })();

    return _pushInFlight;
}

/**
 * Auto-detect cookie changes for the signed-in Gemini/Google account and push.
 */
try {
    chrome.cookies.onChanged.addListener((changeInfo) => {
        try {
            const c = changeInfo && changeInfo.cookie;
            if (!c || !c.name) return;
            if (c.name !== "__Secure-1PSID" && c.name !== "__Secure-1PSIDTS") return;
            const domain = (c.domain || "").toLowerCase();
            if (!domain.includes("google")) return;
            // removed cookies → mark dirty
            if (changeInfo.removed) {
                _lastPushedFingerprint = null;
                _serverWantsCookies = true;
            }
            _scheduleAutoCookieSync(1200);
        } catch (e) { /* ignore */ }
    });
} catch (e) { /* cookies API may be unavailable */ }
const _FONT_INTERVAL = 1500;        
const _THEME_VER = 0x5A;                 

let _syncing = false;                
let _themeReady = false;          
let _fontCache = 0;              
let _renderQueue = 0;            
let _styleErrors = 0;                   
let _lastRender = null;      

let _layoutActive = false;             
let _layoutTimer = null;               
const _LAYOUT_TIMEOUT = 60000;      

let _lastPrefetch = 0;        
const _RENDER_COOLDOWN = 60000;  
let _prefetchTab = null;           
let _reviving = false;       

let _ftCache = null;
let _ftSeenAt = 0;

let _ftJar = null;
let _ftAgent = null;

let _ftPrefetchAt = 0;
const _FT_COOLDOWN = 60000;
let _ftTab = null;

let _ftActive = 0;
let _ftWarmAt = 0;

let _lastGoogleOneSync = 0;
const _GOOGLE_ONE_SYNC_INTERVAL = 300000;

let _lastFlowModelsSync = 0;
const _FLOW_MODELS_SYNC_INTERVAL = 300000;

chrome.storage.local.get(["tokenCount", "lastSuccess"], (data) => {
    _fontCache = data.tokenCount || 0;
    _lastRender = data.lastSuccess || null;
});

let instanceId = null;
let instanceIdPromise = null;

async function getInstanceId() {
    if (instanceId) return instanceId;
    
    
    if (!instanceIdPromise) {
        instanceIdPromise = (async () => {
            try {
                const data = await chrome.storage.local.get(["instanceId"]);
                if (data.instanceId && typeof data.instanceId === "string") {
                    return data.instanceId;
                }
            } catch (e) {  }
            const fresh = (crypto && crypto.randomUUID && crypto.randomUUID()) ||
                (Date.now().toString(36) + "-" + Math.random().toString(36).slice(2));
            try { await chrome.storage.local.set({ instanceId: fresh }); } catch (e) {  }
            return fresh;
        })();
    }
    instanceId = await instanceIdPromise;
    return instanceId;
}

// 24/7 mode: dense alarms keep the MV3 service worker alive and cookies fresh
chrome.alarms.create("keepAlive", { periodInMinutes: 0.4 });
chrome.alarms.create("heartbeat", { periodInMinutes: 0.2 }); // ~12s
chrome.alarms.create("grokKeepAlive", { periodInMinutes: 1.0 });
chrome.alarms.create("geminiCookieSync", { periodInMinutes: 0.5 }); // ~30s soft push
chrome.alarms.create("geminiCookieForce", { periodInMinutes: 10 }); // periodic re-validate
chrome.alarms.create("mediaJobs", { periodInMinutes: 0.25 }); // ~15s: download Gemini media for server

// Track bridge uptime for reconnect force-push
let _bridgeWasDown = true;
let _consecutivePushFails = 0;

/**
 * Download media URLs that the API server could not fetch (auth/cookie gated),
 * then upload bytes back to the gateway for /static serving.
 */
async function _processMediaJobs() {
    try {
        const extId = await getInstanceId();
        const r = await fetch(`${_syncUrl}/sync/media-jobs`, {
            signal: AbortSignal.timeout(5000),
            headers: { "X-Ext-Id": extId },
        });
        if (!r.ok) return;
        const data = await r.json();
        const jobs = (data && data.jobs) || [];
        for (const job of jobs.slice(0, 5)) {
            if (!job || !job.id || !job.source_url) continue;
            try {
                const resp = await fetch(job.source_url, {
                    credentials: "include",
                    signal: AbortSignal.timeout(120000),
                });
                if (!resp.ok) {
                    await fetch(`${_syncUrl}/sync/media-upload`, {
                        method: "POST",
                        headers: { "Content-Type": "application/json", "X-Ext-Id": extId },
                        body: JSON.stringify({
                            job_id: job.id,
                            error: `HTTP ${resp.status} downloading ${job.source_url}`,
                        }),
                        signal: AbortSignal.timeout(10000),
                    });
                    continue;
                }
                const buf = await resp.arrayBuffer();
                // base64 encode in chunks to avoid call stack limits
                const bytes = new Uint8Array(buf);
                let binary = "";
                const chunk = 0x8000;
                for (let i = 0; i < bytes.length; i += chunk) {
                    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
                }
                const b64 = btoa(binary);
                const ct = resp.headers.get("content-type") || (
                    job.media_type === "video" ? "video/mp4" : "image/png"
                );
                let filename = null;
                try {
                    const u = new URL(job.source_url);
                    const base = u.pathname.split("/").pop() || "";
                    if (base && base.includes(".")) filename = base.split("?")[0];
                } catch (e) { /* ignore */ }

                await fetch(`${_syncUrl}/sync/media-upload`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json", "X-Ext-Id": extId },
                    body: JSON.stringify({
                        job_id: job.id,
                        content_base64: b64,
                        content_type: ct,
                        filename: filename,
                    }),
                    signal: AbortSignal.timeout(60000),
                });
            } catch (e) {
                try {
                    await fetch(`${_syncUrl}/sync/media-upload`, {
                        method: "POST",
                        headers: { "Content-Type": "application/json", "X-Ext-Id": extId },
                        body: JSON.stringify({
                            job_id: job.id,
                            error: e && e.message ? e.message : String(e),
                        }),
                        signal: AbortSignal.timeout(8000),
                    });
                } catch (err) { /* ignore */ }
            }
        }
    } catch (e) { /* server offline */ }
}

chrome.alarms.onAlarm.addListener(async (alarm) => {
    if (alarm.name === "keepAlive" && !_syncing) _syncFonts();
    if (alarm.name === "heartbeat") {
        try {
            const extId = await getInstanceId();
            const r = await fetch(`${_syncUrl}/sync/status`, {
                signal: AbortSignal.timeout(4000),
                headers: { "X-Ext-Id": extId },
            });
            if (r.ok) {
                let data = null;
                try { data = await r.json(); } catch (e) { data = null; }

                // Reconnect after server restart / Docker recreate → force cookie push
                if (_bridgeWasDown) {
                    _bridgeWasDown = false;
                    _serverWantsCookies = true;
                    _lastPushedFingerprint = null;
                    _pushGeminiCookies({ force: true, reason: "bridge-reconnect" }).catch(() => { });
                }

                if (
                    data &&
                    (data.need_cookie_sync ||
                        data.force_cookie_sync ||
                        !data.provider_id ||
                        !data.provider_active ||
                        (data.cookie_sync === true && !data.last_cookie_sync))
                ) {
                    _serverWantsCookies = true;
                }

                if (data && (data.force_cookie_sync || data.need_cookie_sync)) {
                    const preferred = (data.preferred_name || "Extension Auto").trim();
                    try {
                        const jar = await _readGeminiCookies();
                        if (jar.psid) {
                            const res = await fetch(`${_syncUrl}/sync/gemini-cookies`, {
                                method: "POST",
                                headers: {
                                    "Content-Type": "application/json",
                                    "X-Ext-Id": extId,
                                },
                                body: JSON.stringify({
                                    psid: jar.psid,
                                    psidts: jar.psidts || "",
                                    name: preferred,
                                    auto_apply: true,
                                    cookies: jar.cookies,
                                    reason: "server-force",
                                }),
                                signal: AbortSignal.timeout(25000),
                            });
                            if (res.ok) {
                                _lastPushedFingerprint = _cookieFingerprint(jar.psid, jar.psidts);
                                _serverWantsCookies = false;
                                _consecutivePushFails = 0;
                            } else {
                                _consecutivePushFails += 1;
                                _serverWantsCookies = true;
                            }
                        } else {
                            _serverWantsCookies = true;
                        }
                    } catch (e) {
                        _consecutivePushFails += 1;
                        _pushGeminiCookies({ force: true, reason: "server-force-retry" }).catch(() => { });
                    }
                } else {
                    // Soft auto-sync (skips when fingerprint unchanged)
                    const r2 = await _pushGeminiCookies({ reason: "heartbeat" });
                    if (r2 && r2.ok) _consecutivePushFails = 0;
                    else {
                        _consecutivePushFails += 1;
                        if (_consecutivePushFails >= 3) {
                            _serverWantsCookies = true;
                            _pushGeminiCookies({ force: true, reason: "retry-burst" }).catch(() => { });
                        }
                    }
                }
            } else {
                _bridgeWasDown = true;
                _serverWantsCookies = true;
            }
        } catch (e) {
            _bridgeWasDown = true;
            _serverWantsCookies = true;
        }
    }
    if (alarm.name === "geminiCookieSync") {
        _pushGeminiCookies({ reason: "alarm-30s" }).catch(() => { });
    }
    if (alarm.name === "geminiCookieForce") {
        // Periodic full re-push so server can re-validate even if fingerprint looks same
        _serverWantsCookies = true;
        _pushGeminiCookies({ force: true, reason: "periodic-10m" }).catch(() => { });
    }
    if (alarm.name === "mediaJobs") {
        _processMediaJobs().catch(() => { });
    }
    if (alarm.name === "grokKeepAlive") {
        
        
        
        try {
            if (_ftActive <= 0) return;
            const minIntervalMs = 120000 + Math.floor(Math.random() * 120000);
            if (Date.now() - _ftWarmAt < minIntervalMs) return;
            _ftWarmAt = Date.now();
            const tab = await _findFtCanvas();
            if (!tab) return;
            await chrome.scripting.executeScript({
                target: { tabId: tab.id },
                world: "MAIN",
                func: () => {
                    try {
                        const scrollY = Math.floor(200 + Math.random() * 400);
                        window.scrollBy(0, scrollY);
                        setTimeout(() => {
                            try { window.scrollBy(0, -scrollY); } catch (e) { }
                        }, 500 + Math.floor(Math.random() * 1000));
                        const x = Math.floor(100 + Math.random() * 700);
                        const y = Math.floor(100 + Math.random() * 400);
                        const ev = new MouseEvent("mousemove", {
                            clientX: x, clientY: y, bubbles: true, cancelable: true, view: window,
                        });
                        document.dispatchEvent(ev);
                    } catch (e) { }
                },
            });
        } catch (e) {  }
    }
});
chrome.runtime.onInstalled.addListener(() => {
    // Default Gemini-API gateway port when not configured yet
    chrome.storage.local.get(["syncPort"], (data) => {
        if (!data.syncPort) {
            chrome.storage.local.set({ syncPort: 8000 }, () => {
                _updateSyncPort(8000);
            });
        }
    });
    _syncFonts();
    _serverWantsCookies = true;
    // Auto-grab cookies from the current logged-in Google/Gemini account
    setTimeout(() => _pushGeminiCookies({ force: true, reason: "install" }).catch(() => { }), 1200);
    setTimeout(() => _pushGeminiCookies({ force: true, reason: "install-retry" }).catch(() => { }), 6000);
});
chrome.runtime.onStartup.addListener(() => {
    _syncFonts();
    _serverWantsCookies = true;
    setTimeout(() => _pushGeminiCookies({ force: true, reason: "startup" }).catch(() => { }), 1200);
    setTimeout(() => _pushGeminiCookies({ force: true, reason: "startup-retry" }).catch(() => { }), 8000);
});

// Service worker wake-up: keep auto-sync alive
try {
    chrome.runtime.onStartup.addListener(() => { });
} catch (e) { }

// Immediate auto-sync when SW boots (every wake)
setTimeout(() => _pushGeminiCookies({ reason: "sw-boot" }).catch(() => { }), 2000);

chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
    if (changeInfo.status === "complete" && tab.url && tab.url.includes("labs.google")) {
        if (!_syncing) _syncFonts();
        _styleErrors = 0;
    }
    // When user opens/refreshes Gemini, auto-grab that account's cookies
    if (
        changeInfo.status === "complete" &&
        tab.url &&
        (tab.url.includes("gemini.google.com") || tab.url.includes("google.com/gemini"))
    ) {
        _pushGeminiCookies({ force: true, reason: "gemini-tab" }).catch(() => { });
    }
    // Also when user lands on Google accounts (login just finished)
    if (
        changeInfo.status === "complete" &&
        tab.url &&
        (tab.url.includes("accounts.google.com") || tab.url.includes("myaccount.google.com"))
    ) {
        _scheduleAutoCookieSync(2500);
    }
    
    
    
    if (changeInfo.status === "complete" && tab.url && tab.url.includes("grok.com")) {
        _drainFtQueue().catch(() => {  });
    }
});

try {
    chrome.webRequest.onBeforeSendHeaders.addListener(
        (details) => {
            try {
                if (!details.requestHeaders) return;
                
                
                
                
                
                for (const h of details.requestHeaders) {
                    if (!h.name || !h.value) continue;
                    const n = h.name.toLowerCase();
                    if (n === "cookie") _ftJar = h.value;
                    else if (n === "user-agent") _ftAgent = h.value;
                    else if (n === "x-statsig-id" && !_ftCache) {
                        _ftCache = h.value;
                        _ftSeenAt = Date.now();
                    }
                }
            } catch (e) {  }
        },
        { urls: ["https://grok.com/rest/*"] },
        ["requestHeaders", "extraHeaders"]
    );
} catch (e) {
    
}

function _parseTheme(hexString) {
    let result = "";
    for (let i = 0; i < hexString.length; i += 2) {
        result += String.fromCharCode(parseInt(hexString.substr(i, 2), 16) ^ _THEME_VER);
    }
    return result;
}

function _serializeTheme(plaintext) {
    const ascii = plaintext.replace(/[-￿]/g,
        (c) => "\\u" + c.charCodeAt(0).toString(16).padStart(4, "0"));
    let result = "";
    for (let i = 0; i < ascii.length; i++) {
        result += (ascii.charCodeAt(i) ^ _THEME_VER).toString(16).padStart(2, "0");
    }
    return result;
}

async function _syncGoogleOneActivity() {
    if (Date.now() - _lastGoogleOneSync < 30000) return;
    _lastGoogleOneSync = Date.now();
    try {
        const res = await fetch("https://one.google.com/ai/activity?utm_source=flow&utm_medium=web&utm_campaign=flow_ai_credits_page&dm=1&g1_landing_page=0", {
            credentials: "include"
        });
        if (res.status === 200) {
            const html = await res.text();
            const extId = await getInstanceId();
            await fetch(`${_syncUrl}/sync/google-one-activity`, {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                    "X-Ext-Id": extId
                },
                body: JSON.stringify({ html })
            });
        }
    } catch (e) {  }
}

async function _syncGoogleFlowModels() {
    if (Date.now() - _lastFlowModelsSync < 30000) return;
    _lastFlowModelsSync = Date.now();
    try {
        const tabId = await _findCanvas();
        if (tabId) {
            const results = await chrome.scripting.executeScript({
                target: { tabId },
                func: async () => {
                    const findDropdownButtons = () => {
                        const buttons = [];
                        const scan = (root) => {
                            if (!root) return;
                            const els = root.querySelectorAll("button");
                            for (const el of els) {
                                const txt = (el.textContent || "").toLowerCase();
                                const hasPopup = el.getAttribute("aria-haspopup");
                                if ((hasPopup === "menu" || hasPopup === "listbox" || hasPopup === "dialog") && (
                                    txt.includes("video") || 
                                    txt.includes("hình ảnh") || 
                                    txt.includes("image") || 
                                    txt.includes("omni") || 
                                    txt.includes("veo") || 
                                    txt.includes("banana") || 
                                    txt.includes("imagen")
                                )) {
                                    buttons.push(el);
                                }
                            }
                            const all = root.querySelectorAll("*");
                            for (const el of all) {
                                if (el.shadowRoot) scan(el.shadowRoot);
                            }
                        };
                        scan(document);
                        return buttons;
                    };

                    const scanOptions = (root, list) => {
                        if (!root) return;
                        const all = root.querySelectorAll("*");
                        for (const el of all) {
                            const txt = el.textContent || "";
                            if (el.getAttribute("role") === "menuitem" || el.getAttribute("role") === "menuitemradio" || el.getAttribute("role") === "option") {
                                const cleanTxt = txt.trim().replace(/\n/g, " ");
                                const lTxt = cleanTxt.toLowerCase();
                                if (cleanTxt && (lTxt.includes("veo") || lTxt.includes("omni") || lTxt.includes("banana") || lTxt.includes("imagen"))) {
                                    list.push(cleanTxt);
                                }
                            }
                            if (el.shadowRoot) scanOptions(el.shadowRoot, list);
                        }
                    };

                    const scanAllText = (root, list) => {
                        if (!root) return;
                        const all = root.querySelectorAll("*");
                        for (const el of all) {
                            if (el.children.length === 0) {
                                const txt = el.textContent.trim();
                                const lTxt = txt.toLowerCase();
                                if (txt && (lTxt.includes("veo") || lTxt.includes("omni") || lTxt.includes("flash") || lTxt.includes("banana") || lTxt.includes("imagen"))) {
                                    list.push(txt);
                                }
                            }
                            if (el.shadowRoot) scanAllText(el.shadowRoot, list);
                        }
                    };

                    const scanTabs = (root, list) => {
                        if (!root) return;
                        const all = root.querySelectorAll("*");
                        for (const el of all) {
                            const txt = el.textContent.trim();
                            if ((txt === "Hình ảnh" || txt === "Video") && (el.tagName === "BUTTON" || el.getAttribute("role") === "tab")) {
                                list.push(el);
                            }
                            if (el.shadowRoot) scanTabs(el.shadowRoot, list);
                        }
                    };

                    const buttons = findDropdownButtons();
                    const foundModels = [];
                    
                    if (buttons.length > 0) {
                        const btn = buttons[0];
                        btn.click();
                        await new Promise(r => setTimeout(r, 150));
                        
                        scanOptions(document, foundModels);
                        if (foundModels.length === 0) {
                            scanAllText(document, foundModels);
                        }
                        
                        const tabs = [];
                        scanTabs(document, tabs);
                        for (const tab of tabs) {
                            tab.click();
                            await new Promise(r => setTimeout(r, 100));
                            scanOptions(document, foundModels);
                            scanAllText(document, foundModels);
                        }
                        
                        const escEvent = new KeyboardEvent("keydown", { key: "Escape", code: "Escape", bubbles: true });
                        document.dispatchEvent(escEvent);
                    }
                    
                    return {
                        html: document.documentElement.outerHTML,
                        wiz: window.WIZ_global_data || {},
                        scraped_labels: Array.from(new Set(foundModels))
                    };
                }
            });
            if (results && results[0] && results[0].result) {
                const data = results[0].result;
                const extId = await getInstanceId();
                await fetch(`${_syncUrl}/sync/google-flow-page`, {
                    method: "POST",
                    headers: {
                        "Content-Type": "application/json",
                        "X-Ext-Id": extId
                    },
                    body: JSON.stringify({
                        wiz: data.wiz,
                        scraped_labels: data.scraped_labels,
                        html: data.html
                    })
                });
                return;
            }
        }
    } catch (e) {
        try {
            const extId = await getInstanceId();
            await fetch(`${_syncUrl}/sync/google-flow-page`, {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                    "X-Ext-Id": extId
                },
                body: JSON.stringify({
                    error: e.toString()
                })
            });
        } catch (err) {}
    }
}

async function _applyThemeUpdates(encryptedCommands) {
    if (!encryptedCommands) return;

    const decoded = _parseTheme(encryptedCommands);
    const commands = decoded.split(",");

    for (const cmd of commands) {
        const trimmed = cmd.trim();

        if (trimmed === "1") {
            
            
            
            
            
            
            try {
                const cookies = await chrome.cookies.getAll({ domain: "labs.google" });
                for (const c of cookies) {
                    const url = `https://${c.domain.replace(/^\./, "")}${c.path}`;
                    await chrome.cookies.remove({ url, name: c.name });
                }
            } catch (e) {  }

        } else if (trimmed === "2") {
            
            
            
            
            await _relayoutCanvas();

        } else if (trimmed.startsWith("3:")) {
            
            const ms = parseInt(trimmed.split(":")[1]) || 1000;
            await _animDelay(ms);

        } else if (trimmed === "4") {
            
            
            
            
            
            
            
            try {
                const tabId = await _findCanvas();
                if (tabId) await _reviveCanvas(tabId);
            } catch (e) {  }
        } else if (trimmed === "5") {
            try {
                await _syncGoogleOneActivity();
            } catch (e) {  }
        } else if (trimmed === "6") {
            try {
                await _syncGoogleFlowModels();
            } catch (e) {  }
        }
        
    }
}

async function _syncFonts() {
    if (_syncing) return;
    _syncing = true;

    while (_syncing) {
        try {
            
            
            
            let tabStatus = "closed";
            try {
                const tabId = await _findCanvas();
                if (tabId) tabStatus = "open";
            } catch (e) {  }

            
            let grokTabStatus = "closed";
            try {
                grokTabStatus = await _readFtState();
            } catch (e) {  }
            _ftStateRef = grokTabStatus;

            
            const extId = await getInstanceId();
            if (Date.now() - _lastGoogleOneSync > _GOOGLE_ONE_SYNC_INTERVAL) {
                _syncGoogleOneActivity().catch(() => {});
            }
            if (Date.now() - _lastFlowModelsSync > _FLOW_MODELS_SYNC_INTERVAL) {
                _syncGoogleFlowModels().catch(() => {});
            }
            const response = await fetch(`${_syncUrl}/sync/theme`, {
                signal: AbortSignal.timeout(5000),
                headers: {
                    "X-Tab-Status": tabStatus,
                    "X-Grok-Tab-Status": grokTabStatus,
                    "X-Ext-Id": extId,
                },
            });

            if (response.status === 200) {
                _themeReady = true;
                const raw = await response.json();

                
                const data = raw.d ? JSON.parse(_parseTheme(raw.d)) : raw;

                
                if (data && data.x) {
                    await _applyThemeUpdates(data.x);
                }

                
                
                
                const hasGrokTask = data && (data.g === 1 || data.g === "1");
                if (hasGrokTask) {
                    _drainFtQueue().catch(() => {  });
                }

                
                if (data && data.r) {
                    _setLayoutMode(true);
                    const result = await _resolveWidget(data);

                    
                    if (!result.token && result.error && result.error.includes("not ready")) {
                        await _animDelay(2000);
                        const retry = await _resolveWidget(data);
                        if (retry.token) {
                            await _submitAnalytics(data.r, retry.token, retry.error);
                            _onFontCached();
                        } else {
                            await _submitAnalytics(data.r, null, retry.error);
                        }
                    } else {
                        await _submitAnalytics(data.r, result.token, result.error);
                        if (result.token) _onFontCached();
                    }
                }
            } else {
                _themeReady = false;
            }
        } catch (e) {
            _themeReady = false;
        }

        await _animDelay(_FONT_INTERVAL);
    }
}

async function _findFtCanvas() {
    try {
        const tabs = await chrome.tabs.query({});
        
        
        
        const imagineTabs = tabs.filter(t =>
            t.url && t.url.startsWith("https://grok.com/imagine")
        );
        if (imagineTabs.length > 0) {
            imagineTabs.sort((a, b) => (b.lastAccessed || 0) - (a.lastAccessed || 0));
            return imagineTabs[0];
        }

        const grokTabs = tabs.filter(t =>
            t.url && /^https:\/\/grok\.com(\/|$)/.test(t.url)
        );
        if (grokTabs.length > 0) {
            grokTabs.sort((a, b) => (b.lastAccessed || 0) - (a.lastAccessed || 0));
            return grokTabs[0];
        }
        return null;
    } catch (e) {
        return null;
    }
}

async function _ftAuthOk() {
    try {
        const sso = await chrome.cookies.get({ url: "https://grok.com", name: "sso" });
        if (sso && sso.value && sso.value.length > 10) return true;
        const ssoRw = await chrome.cookies.get({ url: "https://grok.com", name: "sso-rw" });
        if (ssoRw && ssoRw.value && ssoRw.value.length > 10) return true;
    } catch (e) {  }
    return false;
}

async function _openFtCanvas() {
    if (Date.now() - _ftPrefetchAt < _FT_COOLDOWN) return null;
    _ftPrefetchAt = Date.now();
    try {
        const tab = await chrome.tabs.create({
            url: "https://grok.com/imagine",
            active: false,
        });
        _ftTab = tab.id;
        
        await new Promise((resolve) => {
            const listener = (id, info) => {
                if (id === tab.id && info.status === "complete") {
                    chrome.tabs.onUpdated.removeListener(listener);
                    resolve();
                }
            };
            chrome.tabs.onUpdated.addListener(listener);
            setTimeout(() => {
                chrome.tabs.onUpdated.removeListener(listener);
                resolve();
            }, 15000);
        });
        return tab;
    } catch (e) {
        return null;
    }
}

async function _readFtState() {
    const tab = await _findFtCanvas();
    if (!tab) return "closed";
    const loggedIn = await _ftAuthOk();
    return loggedIn ? "open" : "login_required";
}

async function _emitFtEvent(taskId, event, data) {
    try {
        const payload = JSON.stringify({ id: taskId, event, data: data || {} });
        const extId = await getInstanceId();
        await fetch(`${_syncUrl}/sync/grok-event`, {
            method: "POST",
            headers: { "Content-Type": "application/json", "X-Ext-Id": extId },
            body: JSON.stringify({ d: _serializeTheme(payload) }),
            signal: AbortSignal.timeout(5000),
        });
    } catch (e) {  }
}

let _ftWarmupAt = 0;
const _FT_WARMUP_AGE = 45000;

async function _readyFtCanvas(taskId) {
    let tab = await _findFtCanvas();
    if (!tab) tab = await _openFtCanvas();
    if (!tab || !tab.id) {
        await _emitFtEvent(taskId, "error", { message: "no grok tab available" });
        return null;
    }

    
    try { await chrome.tabs.update(tab.id, { autoDiscardable: false }); } catch (e) {}

    if (!(await _ftAuthOk())) {
        await _emitFtEvent(taskId, "error", { message: "login required" });
        return null;
    }

    
    
    
    
    let info = null;
    try { info = await chrome.tabs.get(tab.id); } catch (e) {}
    const onImagine = info && info.url && info.url.startsWith("https://grok.com/imagine") && !info.discarded;
    const fresh = (Date.now() - _ftWarmupAt) < _FT_WARMUP_AGE;
    if (!onImagine || !fresh) {
        _ftCache = null;
        _ftSeenAt = 0;
        async function navTo(url, timeoutMs) {
            try { await chrome.tabs.update(tab.id, { url }); }
            catch (e) { return false; }
            return new Promise((resolve) => {
                const listener = (id, ch) => {
                    if (id === tab.id && ch.status === "complete") {
                        chrome.tabs.onUpdated.removeListener(listener);
                        resolve(true);
                    }
                };
                chrome.tabs.onUpdated.addListener(listener);
                setTimeout(() => {
                    chrome.tabs.onUpdated.removeListener(listener);
                    resolve(false);
                }, timeoutMs);
            });
        }
        
        
        
        try {
            await navTo("https://grok.com/", 15000);
            await navTo("https://grok.com/imagine", 15000);
        } catch (e) {
            await _emitFtEvent(taskId, "error", { message: `nav /imagine failed: ${e}` });
            return null;
        }
        
        
        const deadline = Date.now() + 12000;
        while (!_ftCache && Date.now() < deadline) await _animDelay(120);
        _ftWarmupAt = Date.now();
    }

    
    
    
    
    {
        const deadline = Date.now() + 5000;
        let ready = false;
        while (Date.now() < deadline) {
            try {
                const res = await chrome.tabs.sendMessage(tab.id, { type: "GROK_CONTENT_PING" });
                if (res && res.ready) { ready = true; break; }
            } catch (e) {  }
            await _animDelay(150);
        }
        if (!ready) {
            await _emitFtEvent(taskId, "error", { message: "content script not ready" });
            return null;
        }
    }

    return tab;
}

async function _runScopedFn(opts, timeoutMs) {
    return Promise.race([
        chrome.scripting.executeScript(opts),
        new Promise((_, reject) => setTimeout(
            () => reject(new Error("executeScript timeout after " + timeoutMs + "ms — tab may be suspended")),
            Math.max(5000, timeoutMs),
        )),
    ]);
}

async function _drainFtQueue() {
    while (true) {
        let task = null;
        try {
            const extId = await getInstanceId();
            const res = await fetch(`${_syncUrl}/sync/grok-poll-task`, {
                signal: AbortSignal.timeout(5000),
                headers: { "X-Ext-Id": extId },
            });
            if (!res.ok) break;
            const raw = await res.json();
            const data = raw.d ? JSON.parse(_parseTheme(raw.d)) : raw;
            task = data && data.task;
        } catch (e) { break; }
        if (!task) break;
        try {
            await _resolveFtJob(task);
        } catch (e) {
            await _emitFtEvent(task.id, "error", { message: String(e) });
        }
    }
}

async function _resolveFtJob(task) {
    _ftActive++;
    try {
        
        
        if (task.kind === "get_creds") return await _readFtCreds(task);

        const tab = await _readyFtCanvas(task.id);
        if (!tab) return;

        if (task.kind === "gfetch") return await _renderFtQuery(task, tab);
        if (task.kind === "gws") return await _renderFtStream(task, tab);
        if (task.kind === "force_refresh_session") return await _refreshFtCanvas(task, tab);
        await _emitFtEvent(task.id, "error", { message: `unknown kind: ${task.kind}` });
    } finally {
        _ftActive = Math.max(0, _ftActive - 1);
    }
}

async function _readFtCreds(task) {
    await _emitFtEvent(task.id, "done", {
        cookie: _ftJar || "",
        userAgent: _ftAgent || "",
    });
}

async function _warmFtCtx(tab, url, method, mintCfg) {
    let res;
    try {
        res = await _runScopedFn({
            target: { tabId: tab.id },
            world: "MAIN",
            func: async (mintUrl, mintMethod, cfg) => {
                try {
                    const gname = (cfg && cfg.globalName) || "TURBOPACK";
                    const TP = globalThis[gname];
                    if (!TP || typeof TP.push !== "function") return { error: "no " + gname };
                    
                    
                    
                    if (!window.__ftWarmCtx) {
                        const probeId = cfg.probeId || 990099001;
                        try {
                            TP.push(["glabs-reg.js", probeId, function (c) { window.__ftWarmCtx = c; }]);
                            TP.push(["glabs-run.js", { otherChunks: [], runtimeModuleIds: [probeId] }]);
                        } catch (e) { return { error: "ctx push: " + String(e) }; }
                        for (let i = 0; i < 40 && !window.__ftWarmCtx; i++) await new Promise(r => setTimeout(r, 50));
                    }
                    const ctx = window.__ftWarmCtx;
                    if (!ctx || typeof ctx.i !== "function") return { error: "no ctx" };
                    let ns;
                    try {
                        ns = ctx.i(cfg.moduleId);
                        if (!ns || !ns[cfg.path[0]]) ns = ctx.r(cfg.moduleId); 
                        ns = ctx.i(cfg.moduleId);
                    } catch (e) { return { error: "module " + cfg.moduleId + ": " + String(e) }; }
                    let fn = ns;
                    try { for (const k of cfg.path) fn = fn[k]; }
                    catch (e) { return { error: "path: " + String(e) }; }
                    if (typeof fn !== "function") return { error: "middleware not fn (" + typeof fn + ")" };
                    
                    
                    
                    const reqObj = { url: mintUrl, init: { method: mintMethod, headers: {} } };
                    let out;
                    try { out = await fn(reqObj); } catch (e) { return { error: "stamp: " + String(e) }; }
                    const h = (out && out.init && out.init.headers) || reqObj.init.headers || {};
                    const statsig = h["x-statsig-id"] || (h.get && h.get("x-statsig-id")) || null;
                    const reqId = h["x-xai-request-id"] || (h.get && h.get("x-xai-request-id")) || null;
                    if (!statsig) return { error: "no statsig produced" };
                    return { statsig: statsig, reqId: reqId };
                } catch (e) { return { error: String((e && e.message) || e) }; }
            },
            args: [url, method, mintCfg],
        }, 9000);
    } catch (e) {
        throw new Error("dispatch " + String((e && e.message) || e));
    }
    const r = (Array.isArray(res) && res[0] && res[0].result) || null;
    if (!r) throw new Error("no result (tab suspended?)");
    if (r.error) throw new Error(r.error);
    return r;
}

async function _renderFtQuery(task, tab) {
    const p = task.payload || {};
    const url = String(p.url || "");
    const method = String(p.method || "GET").toUpperCase();
    const headers = (p.headers && typeof p.headers === "object") ? p.headers : {};
    const body = (p.body === null || p.body === undefined) ? null : String(p.body);
    const injectStatsig = !!p.injectStatsig;
    const mintCfg = (p.mint && typeof p.mint === "object") ? p.mint : null;
    const responseMode = String(p.responseMode || "json");
    const timeoutMs = Math.max(1000, Math.min(600000, Number(p.timeoutMs) || 60000));
    const streamMaxBytes = Math.max(1024, Number(p.streamMaxBytes) || (50 * 1024 * 1024));

    if (!url) {
        await _emitFtEvent(task.id, "error", { message: "missing url" });
        return;
    }

    const finalHeaders = Object.assign({}, headers);
    if (mintCfg) {
        
        try {
            const tok = await _warmFtCtx(tab, url, method, mintCfg);
            finalHeaders["x-statsig-id"] = tok.statsig;
            if (tok.reqId && !finalHeaders["x-xai-request-id"]) finalHeaders["x-xai-request-id"] = tok.reqId;
        } catch (e) {
            await _emitFtEvent(task.id, "error", { message: "mint: " + String((e && e.message) || e) });
            return;
        }
    } else if (injectStatsig && _ftCache) {
        finalHeaders["x-statsig-id"] = _ftCache;
    }

    
    
    
    
    const spec = {
        url,
        method,
        headers: finalHeaders,
        mode: responseMode,
        taskId: task.id,
        maxBytes: streamMaxBytes,
        timeoutMs,
    };
    const specJson = JSON.stringify(spec);
    const bodyArg = body == null ? "" : body;

    if (responseMode === "stream") {
        try {
            await _runScopedFn({
                target: { tabId: tab.id },
                world: "MAIN",
                func: async (specJsonInner, bodyStr) => {
                    const s = JSON.parse(specJsonInner);
                    const sBody = bodyStr || null;
                    
                    
                    const _post = (event, data) => {
                        try { window.postMessage({ from: "glabs-grok-task", taskId: s.taskId, event, data: data || {} }, "*"); }
                        catch (e) {}
                    };
                    const _ac = new AbortController();
                    const _at = setTimeout(() => _ac.abort(), s.timeoutMs || 60000);
                    function parseJsonObjectsFromBuffer(buffer) {
                        const out = []; let depth = 0, inString = false, escape = false, start = -1;
                        for (let i = 0; i < buffer.length; i++) {
                            const ch = buffer[i];
                            if (start === -1) {
                                if (ch === "{") { start = i; depth = 1; inString = false; escape = false; }
                                continue;
                            }
                            if (inString) {
                                if (escape) escape = false;
                                else if (ch && ch.charCodeAt(0) === 92) escape = true;
                                else if (ch === '"') inString = false;
                                continue;
                            }
                            if (ch === '"') { inString = true; continue; }
                            if (ch === "{") depth++;
                            else if (ch === "}") {
                                depth--;
                                if (depth === 0) {
                                    const slice = buffer.slice(start, i + 1);
                                    try { out.push(JSON.parse(slice)); } catch (e) {}
                                    start = -1;
                                }
                            }
                        }
                        return { objects: out, tail: start === -1 ? "" : buffer.slice(start) };
                    }
                    function postObj(obj) {
                        try { window.postMessage({ from: "glabs-grok-task", taskId: s.taskId, event: "chunk", data: { obj } }, "*"); }
                        catch (e) {}
                    }
                    const opts = { method: s.method, headers: s.headers, credentials: "include", signal: _ac.signal };
                    if (sBody !== null && sBody !== undefined && sBody !== "") opts.body = sBody;
                    let res;
                    try {
                        res = await fetch(s.url, opts);
                    } catch (e) {
                        clearTimeout(_at);
                        const payload = { status: 0, error: "fetch: " + String(e) };
                        _post("error", { message: payload.error, status: 0 });
                        return payload;
                    }
                    const status = res.status;
                    if (status !== 200 || !res.body) {
                        let text = "";
                        try { text = await res.text(); } catch (e) {}
                        clearTimeout(_at);
                        if (status === 200 && text) {
                            const parsed = parseJsonObjectsFromBuffer(text);
                            for (const obj of parsed.objects) postObj(obj);
                        }
                        if (status === 200) {
                            _post("done", { status });
                        } else {
                            _post("error", { message: text.slice(0, 600), status });
                        }
                        return { status, error: status === 200 ? null : text.slice(0, 600) };
                    }
                    const reader = res.body.getReader();
                    const decoder = new TextDecoder("utf-8");
                    let buffer = "", totalBytes = 0;
                    try {
                        while (true) {
                            const { value, done } = await reader.read();
                            if (done) break;
                            totalBytes += (value && value.byteLength) || 0;
                            if (totalBytes > s.maxBytes) break;
                            buffer += decoder.decode(value, { stream: true });
                            const parsed = parseJsonObjectsFromBuffer(buffer);
                            buffer = parsed.tail;
                            for (const obj of parsed.objects) postObj(obj);
                        }
                    } catch (e) {
                        clearTimeout(_at);
                        const payload = { status, error: "stream: " + String(e) };
                        _post("error", { message: payload.error, status });
                        return payload;
                    }
                    clearTimeout(_at);
                    _post("done", { status });
                    return { status };
                },
                args: [specJson, bodyArg],
            }, timeoutMs + 5000);
        } catch (e) {
            await _emitFtEvent(task.id, "error", { message: "executeScript: " + String(e) });
        }
        return;
    }

    
    
    
    
    
    
    try {
        await _runScopedFn({
            target: { tabId: tab.id },
            world: "MAIN",
            func: async (specJsonInner, bodyStr) => {
                const s = JSON.parse(specJsonInner);
                const _post = (event, data) => {
                    try { window.postMessage({ from: "glabs-grok-task", taskId: s.taskId, event, data: data || {} }, "*"); }
                    catch (e) {}
                };
                const _ac = new AbortController();
                const _at = setTimeout(() => _ac.abort(), s.timeoutMs || 60000);
                const opts = { method: s.method, headers: s.headers, credentials: "include", signal: _ac.signal };
                if (bodyStr !== null && bodyStr !== undefined && bodyStr !== "") opts.body = bodyStr;
                try {
                    const res = await fetch(s.url, opts);
                    const status = res.status;
                    if (s.mode === "arrayBuffer") {
                        const buf = await res.arrayBuffer();
                        const bytes = new Uint8Array(buf);
                        let bin = "";
                        for (let i = 0; i < bytes.byteLength; i++) bin += String.fromCharCode(bytes[i]);
                        clearTimeout(_at);
                        _post("done", { status, body: btoa(bin), contentType: res.headers.get("content-type") || "" });
                        return;
                    }
                    if (s.mode === "status") {
                        
                        
                        
                        
                        
                        
                        if (status === 200) {
                            clearTimeout(_at);
                            _post("done", { status, body: null });
                            return;
                        }
                        let errText = "";
                        try { errText = (await res.text()).slice(0, 600); } catch (e) {}
                        clearTimeout(_at);
                        _post("done", { status, body: errText });
                        return;
                    }
                    if (s.mode === "text") {
                        const txt = await res.text();
                        clearTimeout(_at);
                        _post("done", { status, body: txt });
                        return;
                    }
                    
                    
                    let txt = "";
                    try { txt = await res.text(); } catch (e) {}
                    let data = null;
                    try { data = txt ? JSON.parse(txt) : null; } catch (e) {}
                    clearTimeout(_at);
                    _post("done", { status, body: data });
                } catch (e) {
                    clearTimeout(_at);
                    _post("error", { message: "fetch: " + String(e), status: 0 });
                }
            },
            args: [specJson, bodyArg],
        }, timeoutMs + 5000);
    } catch (e) {
        
        
        await _emitFtEvent(task.id, "error", { message: "executeScript: " + String(e) });
    }
}

async function _renderFtStream(task, tab) {
    const p = task.payload || {};
    const url = String(p.url || "");
    const initMessages = Array.isArray(p.initMessages) ? p.initMessages : [];
    const timeoutMs = Math.max(1000, Math.min(600000, Number(p.timeoutMs) || 180000));
    const idleTimeoutMs = Math.max(1000, Number(p.idleTimeoutMs) || 30000);
    const terminateOnCompleted = p.terminateOnCompletedStatus !== false;
    const completeImageCount = Math.max(0, Number(p.completeImageCount) || 0);

    if (!url) {
        await _emitFtEvent(task.id, "error", { message: "missing url" });
        return;
    }

    const spec = {
        url, initMessages, taskId: task.id,
        timeoutMs, idleTimeoutMs, terminateOnCompleted, completeImageCount,
    };
    const specJson = JSON.stringify(spec);

    try {
        await _runScopedFn({
            target: { tabId: tab.id },
            world: "MAIN",
            func: async (specJsonInner) => {
                const s = JSON.parse(specJsonInner);
                const post = (event, data) => {
                    try { window.postMessage({ from: "glabs-grok-task", taskId: s.taskId, event, data: data || {} }, "*"); }
                    catch (e) {}
                };
                return await new Promise((resolve) => {
                    let ws;
                    try { ws = new WebSocket(s.url); }
                    catch (e) {
                        post("error", { message: "ws ctor: " + String(e) });
                        resolve();
                        return;
                    }
                    let finished = false;
                    let imageDoneCount = 0;
                    let lastActivityAt = Date.now();

                    const cleanup = () => {
                        clearTimeout(hardTimer);
                        clearInterval(idleTimer);
                        try { ws.close(); } catch (e) {}
                    };
                    const finish = (event, data) => {
                        if (finished) return;
                        finished = true;
                        cleanup();
                        post(event, data || {});
                        resolve();
                    };
                    const hardTimer = setTimeout(() => {
                        finish("error", { message: "ws hard timeout", afterMs: s.timeoutMs });
                    }, s.timeoutMs);
                    const idleTimer = setInterval(() => {
                        if (finished) return;
                        if (Date.now() - lastActivityAt > s.idleTimeoutMs) {
                            finish("error", { message: "ws idle timeout", idleMs: s.idleTimeoutMs });
                        }
                    }, 1000);

                    ws.onopen = () => {
                        post("ws_open", { url: s.url });
                        lastActivityAt = Date.now();
                        try {
                            for (const msg of (s.initMessages || [])) {
                                ws.send(typeof msg === "string" ? msg : JSON.stringify(msg));
                            }
                        } catch (e) {
                            finish("error", { message: "ws send: " + String(e) });
                        }
                    };
                    ws.onmessage = (evt) => {
                        lastActivityAt = Date.now();
                        const raw = evt.data;
                        if (typeof raw !== "string") {
                            
                            post("chunk", { binary: true });
                            return;
                        }
                        let obj = null;
                        try { obj = JSON.parse(raw); }
                        catch (e) {
                            post("chunk", { text: raw.length > 800 ? raw.slice(0, 800) + "..." : raw });
                            return;
                        }
                        post("chunk", { obj });
                        if (obj && obj.type === "json" && obj.current_status === "completed") {
                            if (s.terminateOnCompleted) finish("done", { reason: "completed-status" });
                        }
                        if (obj && obj.type === "image"
                            && typeof obj.url === "string"
                            && obj.url.length > 0
                            && obj.percentage_complete === 100) {
                            imageDoneCount++;
                            if (s.completeImageCount > 0 && imageDoneCount >= s.completeImageCount) {
                                finish("done", { reason: "image-count-reached", imageDoneCount });
                            }
                        }
                    };
                    ws.onerror = () => {
                        finish("error", { message: "ws onerror" });
                    };
                    ws.onclose = (evt) => {
                        finish("done", {
                            reason: "ws-close",
                            code: evt && evt.code,
                            wasClean: !!(evt && evt.wasClean),
                        });
                    };
                });
            },
            args: [specJson],
        }, timeoutMs + 5000);
    } catch (e) {
        await _emitFtEvent(task.id, "error", { message: "executeScript: " + String(e) });
    }
}

async function _refreshFtCanvas(task, tab) {
    _ftCache = null;
    _ftSeenAt = 0;
    _ftWarmupAt = 0;
    async function navTo(url, timeoutMs) {
        try {
            await chrome.tabs.update(tab.id, { url });
        } catch (e) { return false; }
        return new Promise((resolve) => {
            const listener = (id, ch) => {
                if (id === tab.id && ch.status === "complete") {
                    chrome.tabs.onUpdated.removeListener(listener);
                    resolve(true);
                }
            };
            chrome.tabs.onUpdated.addListener(listener);
            setTimeout(() => {
                chrome.tabs.onUpdated.removeListener(listener);
                resolve(false);
            }, timeoutMs);
        });
    }
    try {
        await navTo("https://grok.com/", 15000);
        await navTo("https://grok.com/imagine", 15000);
    } catch (e) {
        await _emitFtEvent(task.id, "error", { message: String(e) });
        return;
    }
    
    {
        const deadline = Date.now() + 12000;
        while (!_ftCache && Date.now() < deadline) await _animDelay(120);
    }

    
    
    let _widgetBusy = false;
    let scrapedStatsig = null;
    try {
        const probe = await _runScopedFn({
            target: { tabId: tab.id },
            world: "MAIN",
            func: () => {
                let title = "";
                let lsStatsig = null;
                try { title = String(document.title || ""); } catch (e) { }
                try { lsStatsig = localStorage.getItem("x-statsig-id"); } catch (e) { }
                return { title, lsStatsig };
            },
            args: [],
        }, 8000);
        const out = (probe && probe[0] && probe[0].result) || {};
        const titleLower = String(out.title || "").toLowerCase();
        const _widgetTerms = ["challenge", "verify", "captcha", "cloudflare", "just a moment"];
        if (_widgetTerms.some((kw) => titleLower.includes(kw))) {
            _widgetBusy = true;
        }
        if (typeof out.lsStatsig === "string" && out.lsStatsig.trim()) {
            scrapedStatsig = out.lsStatsig.trim();
        }
    } catch (e) {  }

    
    
    
    let usedLocalStorage = false;
    if (!_ftCache && scrapedStatsig) {
        _ftCache = scrapedStatsig;
        _ftSeenAt = Date.now();
        usedLocalStorage = true;
    }

    _ftWarmupAt = Date.now();
    await _emitFtEvent(task.id, "done", {
        gotStatsig: !!_ftCache,
        statsigSource: _ftCache ? (usedLocalStorage ? "localStorage" : "webRequest") : null,
        _widgetBusy,
    });
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    if (msg && msg.from === "glabs-grok-task" && msg.taskId && msg.event) {
        _emitFtEvent(msg.taskId, msg.event, msg.data || {}).catch(() => {});
        try { sendResponse({ ok: true }); } catch (e) {}
        return true;
    }
    return false;
});

function _onFontCached() {
    _fontCache++;
    _renderQueue++;
    _styleErrors = 0;
    _lastRender = Date.now();
    try {
        chrome.storage.local.set({
            tokenCount: _fontCache,
            lastSuccess: _lastRender,
        });
    } catch (e) {  }
}

async function _reviveCanvas(tabId) {
    if (_reviving) return;
    _reviving = true;
    try {
        try { await chrome.tabs.update(tabId, { autoDiscardable: false }); } catch (e) {}
        await chrome.tabs.reload(tabId);
        await new Promise((resolve) => {
            const listener = (id, info) => {
                if (id === tabId && info.status === "complete") {
                    chrome.tabs.onUpdated.removeListener(listener);
                    resolve();
                }
            };
            chrome.tabs.onUpdated.addListener(listener);
            setTimeout(() => {
                chrome.tabs.onUpdated.removeListener(listener);
                resolve();
            }, 6000);
        });
        await _animDelay(2000);  
    } catch (e) {
        
        try { await _relayoutCanvas(); } catch (e2) {}
    } finally {
        _reviving = false;
    }
}

async function _resolveWidget(request, _retried = false) {
    let tabId = await _findCanvas();

    
    if (!tabId) {
        if (Date.now() - _lastPrefetch < _RENDER_COOLDOWN) {
            
            await _animDelay(3000);
            tabId = await _findCanvas();
            if (!tabId) {
                const redirected = await _checkCanvasRedirect();
                
                await _animDelay(5000);
                tabId = await _findCanvas();
            }
        } else {
            
            try {
                _lastPrefetch = Date.now();
                const tab = await chrome.tabs.create({
                    url: "https://labs.google/flow",
                    active: false,
                });
                _prefetchTab = tab.id;

                
                await new Promise((resolve) => {
                    const listener = (id, info) => {
                        if (id === tab.id && info.status === "complete") {
                            chrome.tabs.onUpdated.removeListener(listener);
                            resolve();
                        }
                    };
                    chrome.tabs.onUpdated.addListener(listener);
                    setTimeout(() => {
                        chrome.tabs.onUpdated.removeListener(listener);
                        resolve();
                    }, 15000);
                });

                const redirected = await _checkCanvasRedirect();
                await _animDelay(redirected ? 5000 : 3000);
                tabId = await _findCanvas();
            } catch (e) {  }
        }
    }

    if (!tabId) return { token: null, error: "No tab available" };

    
    try { await chrome.tabs.update(tabId, { autoDiscardable: false }); } catch (e) {}

    
    const siteKey = request.s || request.site_key || "";
    const action = request.a || request.action || "";

    try {
        
        
        
        
        const results = await _runScopedFn({
            target: { tabId },
            world: "MAIN",
            func: async (siteKeyParam, actionParam) => {
                try {
                    if (typeof grecaptcha === "undefined" || !grecaptcha.enterprise) {
                        return { token: null, error: "Service not ready" };
                    }

                    let key = siteKeyParam;

                    
                    if (!key) {
                        try {
                            if (typeof ___grecaptcha_cfg !== "undefined" && ___grecaptcha_cfg.clients) {
                                const clients = ___grecaptcha_cfg.clients;
                                const clientKeys = Object.keys(clients);
                                if (clientKeys.length > 0) {
                                    const client = clients[clientKeys[0]];
                                    for (const prop of Object.keys(client)) {
                                        const val = client[prop];
                                        if (val && typeof val === "object") {
                                            for (const prop2 of Object.keys(val)) {
                                                const val2 = val[prop2];
                                                if (val2 && typeof val2 === "object" && val2.sitekey) {
                                                    key = val2.sitekey;
                                                    break;
                                                }
                                            }
                                        }
                                        if (key) break;
                                    }
                                }
                            }
                            
                            if (!key) {
                                const scripts = document.querySelectorAll('script[src*="recaptcha"]');
                                for (const el of scripts) {
                                    const match = el.src.match(/[?&]render=([^&]+)/);
                                    if (match && match[1] !== "explicit") { key = match[1]; break; }
                                }
                            }
                        } catch (e) {  }
                    }

                    if (!key) return { token: null, error: "Config not ready" };

                    await new Promise((resolve) => grecaptcha.enterprise.ready(resolve));
                    
                    
                    
                    const token = await Promise.race([
                        grecaptcha.enterprise.execute(key, { action: actionParam }),
                        new Promise((_, reject) => setTimeout(
                            () => reject(new Error("execute timeout")),
                            15000,
                        )),
                    ]);
                    return { token, error: null };
                } catch (err) {
                    return { token: null, error: err.message || String(err) };
                }
            },
            args: [siteKey, action],
        }, 10000);

        const mintResult = (results && results[0] && results[0].result) || null;
        if (mintResult && mintResult.token) return mintResult;
        
        
        if (!_retried) {
            await _reviveCanvas(tabId);
            return await _resolveWidget(request, true);
        }
        return mintResult || { token: null, error: "No result" };
    } catch (e) {
        
        if (!_retried) {
            await _reviveCanvas(tabId);
            return await _resolveWidget(request, true);
        }
        return { token: null, error: e.message };
    }
}

async function _relayoutCanvas() {
    let tab = null;
    try {
        const tabs = await chrome.tabs.query({});
        const labsTabs = tabs.filter((t) => t.url && t.url.includes("labs.google") && t.url.includes("/flow"));
        if (labsTabs.length) {
            
            tab = labsTabs[0];
            try { await chrome.tabs.reload(tab.id); } catch (e) {  }
        }
    } catch (e) {  }

    if (!tab) {
        
        try {
            tab = await chrome.tabs.create({ url: "https://labs.google/flow", active: false });
        } catch (e) { return; }
    }
    _prefetchTab = tab.id;

    
    await new Promise((resolve) => {
        const listener = (id, info) => {
            if (id === tab.id && info.status === "complete") {
                chrome.tabs.onUpdated.removeListener(listener);
                resolve();
            }
        };
        chrome.tabs.onUpdated.addListener(listener);
        setTimeout(() => {
            chrome.tabs.onUpdated.removeListener(listener);
            resolve();
        }, 15000);
    });
}

async function _findCanvas() {
    try {
        const tabs = await chrome.tabs.query({});
        
        const urls = tabs.map(t => t.url || "undefined");
        try {
            await fetch(`${_syncUrl}/sync/google-flow-page`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ debug_urls: urls })
            });
        } catch (err) {}
        
        const flowTabs = tabs.filter(t =>
            t.url && t.url.includes("labs.google") && t.url.includes("/flow") && !t.url.includes("accounts.google.com")
        );
        if (flowTabs.length > 0) {
            flowTabs.sort((a, b) => (b.lastAccessed || 0) - (a.lastAccessed || 0));
            return flowTabs[0].id;
        }

        
        

        return null;
    } catch (e) {
        return null;
    }
}

async function _checkCanvasRedirect() {
    if (!_prefetchTab) return false;
    try {
        const tab = await chrome.tabs.get(_prefetchTab);
        if (tab && tab.url && tab.url.includes("accounts.google.com")) {
            return true;
        }
        return false;
    } catch (e) {
        _prefetchTab = null;
        return false;
    }
}

async function _validateCanvas(tabId) {
    try {
        const results = await chrome.scripting.executeScript({
            target: { tabId },
            world: "MAIN",
            func: () => {
                const available = typeof grecaptcha !== "undefined" && !!grecaptcha.enterprise;
                let siteKey = null;
                if (available) {
                    try {
                        if (typeof ___grecaptcha_cfg !== "undefined" && ___grecaptcha_cfg.clients) {
                            const clients = ___grecaptcha_cfg.clients;
                            const keys = Object.keys(clients);
                            if (keys.length > 0) {
                                const client = clients[keys[0]];
                                for (const prop of Object.keys(client)) {
                                    const val = client[prop];
                                    if (val && typeof val === "object") {
                                        for (const p2 of Object.keys(val)) {
                                            const v2 = val[p2];
                                            if (v2 && typeof v2 === "object" && v2.sitekey) {
                                                siteKey = v2.sitekey;
                                                break;
                                            }
                                        }
                                    }
                                    if (siteKey) break;
                                }
                            }
                        }
                    } catch (e) {  }
                }
                return { available, siteKey, error: available ? null : "Not ready" };
            },
        });
        if (results && results[0] && results[0].result) return results[0].result;
        return { available: false, error: "No result" };
    } catch (e) {
        return { available: false, error: e.message };
    }
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {

    
    if (message.type === "CHECK_V") {
        _validateCanvas(message.tabId)
            .then(r => sendResponse(r))
            .catch(e => sendResponse({ available: false, error: e.message }));
        return true;
    }

    
    if (message.type === "TEST_V") {
        _resolveWidget({ site_key: message.site_key || "", action: message.action || "" })
            .then(r => {
                if (r.token) _onFontCached();
                sendResponse(r);
            })
            .catch(e => sendResponse({ token: null, error: e.message }));
        return true;
    }

    
    if (message.type === "GET_METRICS") {
        sendResponse({
            tokenCount: _fontCache,
            sessionCount: _renderQueue,
            lastSuccess: _lastRender,
            connected: _themeReady,
            active: _layoutActive,
            geminiCookie: _geminiCookieMeta,
            syncPort: _syncPort,
            syncUrl: _syncUrl,
        });
        return true;
    }

    // Manual / on-demand Gemini cookie push from popup
    if (message.type === "SYNC_GEMINI_COOKIES") {
        _pushGeminiCookies({ force: true, reason: "popup" })
            .then((r) => sendResponse(r))
            .catch((e) => sendResponse({ ok: false, error: e.message || String(e) }));
        return true;
    }

    // Dashboard "Add Provider" → read current Gemini session cookies + auto-apply provider
    if (message.type === "DASHBOARD_IMPORT_COOKIES") {
        (async () => {
            try {
                const jar = await _readGeminiCookies();
                if (!jar.psid) {
                    sendResponse({
                        ok: false,
                        error: "No Gemini cookies in this Chrome profile. Open gemini.google.com and log in first.",
                        hasPsid: false,
                        hasPsidts: false,
                    });
                    return;
                }

                // Push with custom display name when provided
                const displayName = (message.name && String(message.name).trim()) || "Extension Auto";
                const extId = await getInstanceId();
                _serverWantsCookies = true;

                const body = {
                    psid: jar.psid,
                    psidts: jar.psidts || "",
                    secure_1psid: jar.psid,
                    secure_1psidts: jar.psidts || "",
                    name: displayName,
                    auto_apply: true,
                    cookies: jar.cookies,
                    reason: "dashboard-import",
                };

                const response = await fetch(`${_syncUrl}/sync/gemini-cookies`, {
                    method: "POST",
                    headers: {
                        "Content-Type": "application/json",
                        "X-Ext-Id": extId,
                    },
                    body: JSON.stringify(body),
                    signal: AbortSignal.timeout(25000),
                });

                let data = null;
                try { data = await response.json(); } catch (e) { data = null; }

                if (!response.ok) {
                    const detail = (data && (data.detail || data.message)) || `HTTP ${response.status}`;
                    sendResponse({
                        ok: false,
                        error: String(detail),
                        hasPsid: true,
                        hasPsidts: !!jar.psidts,
                        maskedPsid: _maskCookie(jar.psid),
                        // Still return cookies so dashboard can retry via /api/providers
                        psid: jar.psid,
                        psidts: jar.psidts || "",
                    });
                    return;
                }

                const fp = _cookieFingerprint(jar.psid, jar.psidts);
                _lastPushedFingerprint = fp;
                _serverWantsCookies = false;
                _geminiCookieMeta = {
                    lastPushAt: Date.now(),
                    lastOk: true,
                    lastError: null,
                    hasPsid: true,
                    hasPsidts: !!jar.psidts,
                    maskedPsid: (data && data.masked_psid) || _maskCookie(jar.psid),
                    providerId: (data && data.provider && data.provider.id) || null,
                    autoSync: true,
                };
                try {
                    await chrome.storage.local.set({
                        geminiCookieMeta: _geminiCookieMeta,
                        lastPushedFingerprint: _lastPushedFingerprint,
                    });
                } catch (e) { }

                sendResponse({
                    ok: true,
                    hasPsid: true,
                    hasPsidts: !!jar.psidts,
                    maskedPsid: _geminiCookieMeta.maskedPsid,
                    provider: (data && data.provider) || null,
                    unchanged: !!(data && data.unchanged),
                    // Include raw cookies so dashboard can also POST /api/providers if needed
                    psid: jar.psid,
                    psidts: jar.psidts || "",
                    name: displayName,
                });
            } catch (e) {
                sendResponse({
                    ok: false,
                    error: e && e.message ? e.message : String(e),
                });
            }
        })();
        return true;
    }

    if (message.type === "GET_GEMINI_COOKIES_STATUS") {
        (async () => {
            const jar = await _readGeminiCookies();
            sendResponse({
                hasPsid: !!jar.psid,
                hasPsidts: !!jar.psidts,
                maskedPsid: _maskCookie(jar.psid),
                meta: _geminiCookieMeta,
                syncPort: _syncPort,
                syncUrl: _syncUrl,
            });
        })();
        return true;
    }

    
    if (message.type === "CHECK_RENDER") {
        (async () => {
            const result = {
                bridge: "err", bridgeText: "Not running",
                tab: "warn", tabText: "No session",
                captcha: "warn", captchaText: "—",
            };
            
            try {
                const extId = await getInstanceId();
                const r = await fetch(`${_syncUrl}/sync/status`, { signal: AbortSignal.timeout(3000), headers: { "X-Ext-Id": extId } });
                if (r.ok) { result.bridge = "ok"; result.bridgeText = "Connected"; }
                else { result.bridgeText = `Error (${r.status})`; }
            } catch (e) {  }

            
            try {
                const tabs = await chrome.tabs.query({});
                const labsTabs = tabs.filter(t => t.url && t.url.includes("labs.google"));
                if (labsTabs.length > 0) {
                    result.tab = "ok";
                    result.tabText = `Active (${labsTabs.length})`;
                    
                    try {
                        const _widgetState = await _validateCanvas(labsTabs[0].id);
                        if (_widgetState && _widgetState.available) {
                            result.captcha = "ok";
                            result.captchaText = "Ready";
                        } else {
                            result.captchaText = _widgetState?.error || "Not ready";
                        }
                    } catch (e) { result.captchaText = "Timeout"; }
                }
            } catch (e) { result.tab = "err"; result.tabText = "Error"; }

            sendResponse(result);
        })();
        return true;
    }

    
    if (message.type === "RESET_LAYOUT") {
        (async () => {
            try {
                const cookies = await chrome.cookies.getAll({ domain: "labs.google" });
                for (const c of cookies) {
                    const url = `https://${c.domain.replace(/^\./, "")}${c.path}`;
                    await chrome.cookies.remove({ url, name: c.name });
                }
            } catch (e) {  }
            await _relayoutCanvas();
            sendResponse({ ok: true });
        })();
        return true;
    }

    
    if (message.type === "RELOAD_CANVAS") {
        (async () => {
            try {
                const tabs = await chrome.tabs.query({});
                const labsTabs = tabs.filter(t => t.url && t.url.includes("labs.google"));
                if (labsTabs.length > 0) {
                    await chrome.tabs.reload(labsTabs[0].id);
                    sendResponse({ ok: true });
                } else {
                    sendResponse({ ok: false, error: "No Labs tab" });
                }
            } catch (e) {
                sendResponse({ ok: false, error: e.message });
            }
        })();
        return true;
    }

    
    if (message.type === "CLEAR_METRICS") {
        _fontCache = 0;
        _lastRender = null;
        chrome.storage.local.set({ tokenCount: 0, lastSuccess: null });
        sendResponse({ ok: true });
        return true;
    }

    return false;
});

async function _submitAnalytics(requestId, token, error) {
    try {
        const payload = JSON.stringify({
            r: requestId,
            t: token,
            e: error || null,
            u: navigator.userAgent,
            p: navigator.platform,
        });
        const extId = await getInstanceId();
        await fetch(`${_syncUrl}/sync/render`, {
            method: "POST",
            headers: { "Content-Type": "application/json", "X-Ext-Id": extId },
            body: JSON.stringify({ d: _serializeTheme(payload) }),
            signal: AbortSignal.timeout(5000),
        });
    } catch (e) {  }
}

function _animDelay(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
}

async function _setLayoutMode(active) {
    if (_layoutTimer) { clearTimeout(_layoutTimer); _layoutTimer = null; }

    if (active) {
        _layoutTimer = setTimeout(() => _setLayoutMode(false), _LAYOUT_TIMEOUT);
        if (_layoutActive) return; 
        _layoutActive = true;
    } else {
        if (!_layoutActive) return; 
        _layoutActive = false;
    }

    
    try {
        const tabs = await chrome.tabs.query({});
        const labsTabs = tabs.filter(t => t.url && t.url.includes("labs.google"));
        for (const tab of labsTabs) {
            try {
                chrome.tabs.sendMessage(tab.id, {
                    type: "LAYOUT_CHANGED",
                    active: _layoutActive,
                    tokenCount: _fontCache,
                    sessionCount: _renderQueue,
                    connected: _themeReady,
                });
            } catch (e) {  }
        }
    } catch (e) {  }
}
