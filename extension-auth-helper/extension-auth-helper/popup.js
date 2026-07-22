

let _syncPort = 8000;
let _syncUrl = `http://127.0.0.1:${_syncPort}`;

function _updateSyncPort(port) {
    if (port && typeof port === "number" && port >= 1 && port <= 65535) {
        _syncPort = port;
        _syncUrl = `http://127.0.0.1:${_syncPort}`;
    }
}
const _RENDER_URL = "https://labs.google/fx/tools/flow";
const _GEMINI_URL = "https://gemini.google.com/app";

const dotBridge = document.getElementById("dotBridge");
const valBridge = document.getElementById("valBridge");
const dotTab = document.getElementById("dotTab");
const valTab = document.getElementById("valTab");
const dotCaptcha = document.getElementById("dotRecaptcha");
const valCaptcha = document.getElementById("valRecaptcha");
const dotGeminiCookie = document.getElementById("dotGeminiCookie");
const valGeminiCookie = document.getElementById("valGeminiCookie");

const btnCheck = document.getElementById("btnTest");
const btnVerify = document.getElementById("btnTestCaptcha");
const btnRefresh = document.getElementById("btnRefresh");
const btnOpenTab = document.getElementById("btnOpenTab");
const btnSyncGemini = document.getElementById("btnSyncGemini");
const btnOpenGemini = document.getElementById("btnOpenGemini");
const resultBox = document.getElementById("resultBox");

const statTokens = document.getElementById("statTokens");
const statStatus = document.getElementById("statStatus");
const statLast = document.getElementById("statLast");

let currentTabId = null;

function init() {
    chrome.storage.local.get(["syncPort"], (data) => {
        if (data && data.syncPort) {
            _updateSyncPort(data.syncPort);
        }
        _pullSnapshot();
        _pingTheme().catch(() => {});
        _pingCanvas().catch(() => {});
        _pingGeminiCookies().catch(() => {});

        setInterval(() => {
            _pingTheme().catch(() => {});
            _pullSnapshot();
            _pingGeminiCookies().catch(() => {});
        }, 3000);
    });

    chrome.storage.onChanged.addListener((changes, areaName) => {
        if (areaName === "local" && changes.syncPort) {
            _updateSyncPort(changes.syncPort.newValue);
            _pullSnapshot();
            _pingTheme().catch(() => {});
            _pingGeminiCookies().catch(() => {});
        }
    });
}

function _pullSnapshot() {
    
    try {
        if (chrome.storage && chrome.storage.local) {
            chrome.storage.local.get(["tokenCount", "lastSuccess"], (data) => {
                if (chrome.runtime.lastError) return;
                statTokens.textContent = data.tokenCount || 0;
                statLast.textContent = data.lastSuccess ? _humanizeAt(data.lastSuccess) : "—";
            });
        }
    } catch (e) {  }

    
    try {
        chrome.runtime.sendMessage({ type: "GET_METRICS" }, (response) => {
            if (chrome.runtime.lastError) return;
            if (response) {
                statTokens.textContent = response.tokenCount || 0;
                statStatus.textContent = response.connected ? "🟢" : "🔴";
                statStatus.style.color = response.connected ? "#22c55e" : "#ef4444";
                if (response.lastSuccess) statLast.textContent = _humanizeAt(response.lastSuccess);
            }
        });
    } catch (e) {  }
}

function _humanizeAt(timestamp) {
    const date = new Date(timestamp);
    const now = new Date();
    const diffSeconds = Math.floor((now - date) / 1000);

    if (diffSeconds < 60) return `${diffSeconds}s ago`;
    if (diffSeconds < 3600) return `${Math.floor(diffSeconds / 60)}m ago`;
    if (diffSeconds < 86400) return `${Math.floor(diffSeconds / 3600)}h ago`;

    return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

async function _pingAll() {
    await Promise.all([_pingTheme(), _pingCanvas(), _pingGeminiCookies()]);
    _pullSnapshot();
}

async function _pingGeminiCookies() {
    if (!dotGeminiCookie || !valGeminiCookie) return;
    try {
        const status = await Promise.race([
            chrome.runtime.sendMessage({ type: "GET_GEMINI_COOKIES_STATUS" }),
            new Promise((_, reject) => setTimeout(() => reject(new Error("timeout")), 3000)),
        ]);
        if (!status) {
            _setIndicator(dotGeminiCookie, "warn");
            valGeminiCookie.textContent = "No response";
            return;
        }
        if (status.hasPsid) {
            const parts = [];
            parts.push(status.hasPsidts ? "PSID+TS" : "PSID only");
            if (status.meta && status.meta.lastOk) parts.push("synced");
            else if (status.meta && status.meta.lastError) parts.push("push failed");
            else parts.push("local");
            _setIndicator(dotGeminiCookie, status.meta && status.meta.lastOk ? "ok" : "warn");
            valGeminiCookie.textContent = parts.join(" · ");
            if (status.meta && status.meta.lastOk && status.meta.lastPushAt) {
                statLast.textContent = _humanizeAt(status.meta.lastPushAt);
            }
        } else {
            _setIndicator(dotGeminiCookie, "err");
            valGeminiCookie.textContent = "Login Gemini first";
        }
    } catch (e) {
        _setIndicator(dotGeminiCookie, "warn");
        valGeminiCookie.textContent = "Checking…";
    }
}

async function _pingTheme() {
    try {
        const response = await fetch(`${_syncUrl}/sync/status`, { signal: AbortSignal.timeout(3000) });
        if (response.ok) {
            _setIndicator(dotBridge, "ok");
            valBridge.textContent = "Connected";
        } else {
            _setIndicator(dotBridge, "err");
            valBridge.textContent = `Error (${response.status})`;
        }
    } catch (e) {
        _setIndicator(dotBridge, "err");
        valBridge.textContent = "Not running";
    }
}

async function _pingCanvas() {
    try {
        const tabs = await chrome.tabs.query({});
        const labsTabs = tabs.filter(t => t.url && t.url.includes("labs.google"));

        if (labsTabs.length > 0) {
            currentTabId = labsTabs[0].id;
            _setIndicator(dotTab, "ok");
            valTab.textContent = `Active (${labsTabs.length})`;
            await _pingWidget(currentTabId);
        } else {
            currentTabId = null;
            _setIndicator(dotTab, "warn"); valTab.textContent = "No session";
            _setIndicator(dotCaptcha, "warn"); valCaptcha.textContent = "—";
        }
    } catch (e) {
        _setIndicator(dotTab, "err"); valTab.textContent = "Error";
    }
}

async function _pingWidget(tabId) {
    try {
        const result = await Promise.race([
            chrome.runtime.sendMessage({ type: "CHECK_V", tabId }),
            new Promise((_, reject) => setTimeout(() => reject(new Error("timeout")), 3000)),
        ]);
        if (result && result.available) {
            _setIndicator(dotCaptcha, "ok");
            valCaptcha.textContent = "Ready";
        } else {
            _setIndicator(dotCaptcha, "warn");
            valCaptcha.textContent = result?.error || "Not ready";
        }
    } catch (e) {
        _setIndicator(dotCaptcha, "warn");
        valCaptcha.textContent = "Checking…";
    }
}

function _setIndicator(element, status) {
    element.className = "status-dot";
    if (status === "ok") element.classList.add("dot-ok");
    else if (status === "warn") element.classList.add("dot-warn");
    else if (status === "err") element.classList.add("dot-err");
    else element.classList.add("dot-loading");
}

function _displayMessage(text, type) {
    resultBox.textContent = text;
    resultBox.className = "result-box show " + type;
}

btnCheck.addEventListener("click", async () => {
    btnCheck.disabled = true;
    btnCheck.textContent = "⏳ ...";
    _setIndicator(dotBridge, "loading"); _setIndicator(dotTab, "loading");
    _setIndicator(dotCaptcha, "loading");
    valBridge.textContent = "..."; valTab.textContent = "...";
    valCaptcha.textContent = "...";
    await _pingAll();
    btnCheck.disabled = false;
    btnCheck.textContent = "🔍 Check";
});

btnVerify.addEventListener("click", async () => {
    btnVerify.disabled = true;
    btnVerify.textContent = "⏳ ...";
    resultBox.className = "result-box";

    try {
        
        
        
        
        
        let siteKey = "", action = "";
        try {
            let extId = "";
            try { extId = (await chrome.storage.local.get(["instanceId"])).instanceId || ""; } catch (e) {  }
            const response = await fetch(`${_syncUrl}/sync/config`, {
                signal: AbortSignal.timeout(3000),
                headers: extId ? { "X-Ext-Id": extId } : {},
            });
            if (response.ok) {
                const config = await response.json();
                siteKey = config.recaptcha_ent_key || config.site_key || "";
                action = config.recaptcha_action || "";
            }
        } catch (e) {  }

        const result = await chrome.runtime.sendMessage({
            type: "TEST_V",
            site_key: siteKey,
            action: action,
        });

        if (result && result.token) {
            _displayMessage(`✅ Verified (${result.token.length} chars)`, "success");
            _pullSnapshot();
        } else {
            _displayMessage(`❌ ${result ? result.error || "Failed" : "No response"}`, "error");
        }
    } catch (e) {
        _displayMessage(`❌ ${e.message}`, "error");
    } finally {
        btnVerify.disabled = false;
        btnVerify.textContent = "⚡ Verify";
    }
});

btnRefresh.addEventListener("click", async () => {
    btnRefresh.disabled = true;
    btnRefresh.textContent = "⏳ ...";

    try {
        const tabs = await chrome.tabs.query({});
        const labsTabs = tabs.filter(t => t.url && t.url.includes("labs.google"));

        if (labsTabs.length > 0) {
            let clearedCount = 0;
            try {
                const cookies = await chrome.cookies.getAll({ domain: "labs.google" });
                for (const c of cookies) {
                    const url = `https://${c.domain.replace(/^\./, "")}${c.path}`;
                    await chrome.cookies.remove({ url, name: c.name });
                    clearedCount++;
                }
            } catch (e) {  }

            await chrome.tabs.reload(labsTabs[0].id);
            _displayMessage(`✅ Cleared ${clearedCount} cookies & refreshed`, "success");

            setTimeout(async () => {
                await _pingAll();
                btnRefresh.disabled = false;
                btnRefresh.textContent = "🔄 Refresh";
            }, 3000);
        } else {
            _displayMessage("❌ No Labs tab open", "error");
            btnRefresh.disabled = false;
            btnRefresh.textContent = "🔄 Refresh";
        }
    } catch (e) {
        _displayMessage(`❌ ${e.message}`, "error");
        btnRefresh.disabled = false;
        btnRefresh.textContent = "🔄 Refresh";
    }
});

btnOpenTab.addEventListener("click", async () => {
    const tabs = await chrome.tabs.query({});
    const flowTabs = tabs.filter(t =>
        t.url && t.url.includes("labs.google") && t.url.includes("tools/flow")
    );
    if (flowTabs.length > 0) {
        chrome.tabs.update(flowTabs[0].id, { active: true });
        chrome.windows.update(flowTabs[0].windowId, { focused: true });
    } else {
        chrome.tabs.create({ url: _RENDER_URL });
    }
});

if (btnOpenGemini) {
    btnOpenGemini.addEventListener("click", async () => {
        const tabs = await chrome.tabs.query({});
        const geminiTabs = tabs.filter(t => t.url && t.url.includes("gemini.google.com"));
        if (geminiTabs.length > 0) {
            chrome.tabs.update(geminiTabs[0].id, { active: true });
            chrome.windows.update(geminiTabs[0].windowId, { focused: true });
        } else {
            chrome.tabs.create({ url: _GEMINI_URL });
        }
    });
}

if (btnSyncGemini) {
    btnSyncGemini.addEventListener("click", async () => {
        btnSyncGemini.disabled = true;
        btnSyncGemini.textContent = "⏳ Syncing…";
        resultBox.className = "result-box";
        try {
            const result = await chrome.runtime.sendMessage({ type: "SYNC_GEMINI_COOKIES" });
            if (result && result.ok) {
                const pid = result.data && result.data.provider && result.data.provider.id;
                _displayMessage(
                    `✅ Cookies pushed to Gemini API${pid ? ` (provider ${pid})` : ""}. Dashboard → Providers.`,
                    "success"
                );
            } else {
                _displayMessage(`❌ ${result && result.error ? result.error : "Sync failed"}`, "error");
            }
        } catch (e) {
            _displayMessage(`❌ ${e.message}`, "error");
        } finally {
            await _pingGeminiCookies();
            await _pingTheme();
            btnSyncGemini.disabled = false;
            btnSyncGemini.textContent = "🍪 Sync Cookies";
        }
    });
}

init();

function reportPopupHeight() {
    try { parent.postMessage({ __glabsPopupHeight: document.body.getBoundingClientRect().height }, "*"); } catch (e) {  }
}
try { new ResizeObserver(reportPopupHeight).observe(document.body); } catch (e) {  }
window.addEventListener("load", reportPopupHeight);
reportPopupHeight();
