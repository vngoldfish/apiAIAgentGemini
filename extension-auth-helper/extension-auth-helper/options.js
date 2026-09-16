
const DEFAULT_TARGET = "8000";
const inputTarget = document.getElementById("syncTarget");
const inputToken = document.getElementById("syncToken");
const btnSave = document.getElementById("btnSave");
const statusMsg = document.getElementById("statusMsg");

// Load settings
document.addEventListener("DOMContentLoaded", () => {
    try {
        chrome.storage.local.get(["syncPort", "syncTarget", "syncToken"], (data) => {
            if (chrome.runtime.lastError) {
                console.error("Error loading settings:", chrome.runtime.lastError);
                return;
            }
            // Prefer new syncTarget, fall back to old syncPort
            if (data.syncTarget) {
                inputTarget.value = data.syncTarget;
            } else {
                inputTarget.value = data.syncPort || DEFAULT_TARGET;
            }
            if (data.syncToken) {
                inputToken.value = data.syncToken;
            }
        });
    } catch (e) {
        console.error("Storage API error:", e);
    }
});

// Save settings
btnSave.addEventListener("click", () => {
    const targetVal = (inputTarget.value || "").trim();
    const tokenVal = (inputToken.value || "").trim();

    if (!targetVal) {
        showStatus("Please enter a port number or server URL.", "error");
        return;
    }

    // Validate: must be a port number (1-65535) or a URL starting with http:// or https://
    const isUrl = targetVal.startsWith("http://") || targetVal.startsWith("https://");
    const portNum = parseInt(targetVal, 10);
    const isPort = !isNaN(portNum) && portNum >= 1 && portNum <= 65535 && String(portNum) === targetVal;

    if (!isUrl && !isPort) {
        showStatus("Enter a valid port (1-65535) or full URL (http:// or https://).", "error");
        return;
    }

    // Warn if using remote URL without token
    if (isUrl && !tokenVal) {
        showStatus("⚠️ Remote URL without Sync Token — cookies will be sent unprotected!", "error");
        // Still allow saving — user might set token later
    }

    try {
        const saveData = {
            syncTarget: targetVal,
            syncToken: tokenVal,
        };
        // Also set syncPort for backward compatibility if it's a port number
        if (isPort) {
            saveData.syncPort = portNum;
        }

        chrome.storage.local.set(saveData, async () => {
            if (chrome.runtime.lastError) {
                showStatus(`Failed to save: ${chrome.runtime.lastError.message}`, "error");
                return;
            }

            const targetUrl = isUrl ? targetVal.replace(/\/+$/, "") : `http://127.0.0.1:${portNum}`;
            showStatus(`Saved! Testing connection to ${targetUrl}...`, "info", 0);

            // Test connection to backend
            try {
                const headers = {};
                if (tokenVal) headers["X-Sync-Token"] = tokenVal;

                const resp = await fetch(`${targetUrl}/sync/status`, {
                    headers,
                    signal: AbortSignal.timeout(5000),
                });

                if (resp.ok) {
                    showStatus(`✅ Connected successfully to ${targetUrl}!`, "success", 4500);
                } else if (resp.status === 401) {
                    showStatus(`❌ Connected, but 401 Unauthorized! Check Sync Token.`, "error", 5000);
                } else {
                    showStatus(`⚠️ Server responded with HTTP ${resp.status}.`, "error", 5000);
                }
            } catch (err) {
                showStatus(`⚠️ Saved, but cannot reach server at ${targetUrl}: ${err.message}`, "error", 6000);
            }
        });
    } catch (e) {
        showStatus(`Storage API error: ${e.message}`, "error");
    }
});

function showStatus(text, type, duration = 3500) {
    statusMsg.textContent = text;
    statusMsg.className = `status-msg show ${type}`;
    
    if (window.statusTimeout) {
        clearTimeout(window.statusTimeout);
    }
    if (duration > 0) {
        window.statusTimeout = setTimeout(() => {
            statusMsg.classList.remove("show");
            setTimeout(() => {
                statusMsg.className = "status-msg";
            }, 300);
        }, duration);
    }
}
