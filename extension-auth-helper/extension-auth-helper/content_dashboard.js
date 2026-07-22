/**
 * Bridge: Gemini-API Dashboard (localhost:8000) ↔ extension background.
 * Dashboard posts window messages; we forward to the service worker to
 * read Gemini cookies of the current Chrome session and push to the server.
 */
(() => {
    if (window.__geminiApiDashboardBridge) return;
    window.__geminiApiDashboardBridge = true;

    function isAlive() {
        try {
            return !!chrome.runtime?.id;
        } catch (e) {
            return false;
        }
    }

    function reply(requestId, payload) {
        window.postMessage(
            {
                type: "GEMINI_API_DASHBOARD_COOKIES_RESULT",
                requestId,
                source: "gemini-auth-helper",
                ...payload,
            },
            "*"
        );
    }

    window.addEventListener("message", (event) => {
        if (event.source !== window) return;
        const data = event.data;
        if (!data || typeof data !== "object") return;

        if (data.type === "GEMINI_API_DASHBOARD_PING") {
            window.postMessage(
                {
                    type: "GEMINI_API_EXTENSION_READY",
                    source: "gemini-auth-helper",
                    requestId: data.requestId || null,
                    ok: isAlive(),
                },
                "*"
            );
            return;
        }

        if (data.type !== "GEMINI_API_DASHBOARD_REQUEST_COOKIES") return;

        const requestId = data.requestId || null;
        const name = (data.name && String(data.name).trim()) || "Extension Auto";

        if (!isAlive()) {
            reply(requestId, {
                ok: false,
                error: "Extension context invalidated. Reload the extension and refresh this page.",
            });
            return;
        }

        chrome.runtime.sendMessage(
            {
                type: "DASHBOARD_IMPORT_COOKIES",
                name,
                force: true,
            },
            (response) => {
                if (chrome.runtime.lastError) {
                    reply(requestId, {
                        ok: false,
                        error: chrome.runtime.lastError.message || "No response from extension background",
                    });
                    return;
                }
                reply(requestId, response || { ok: false, error: "Empty response from extension" });
            }
        );
    });

    // Announce bridge is available for the dashboard
    window.postMessage(
        {
            type: "GEMINI_API_EXTENSION_READY",
            source: "gemini-auth-helper",
            ok: isAlive(),
        },
        "*"
    );
})();
