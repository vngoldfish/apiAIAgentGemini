/**
 * Floating FAB on gemini.google.com so users can see the extension is active
 * and open the cookie-sync popup without hunting for the toolbar icon.
 */
(() => {
    if (document.getElementById("gemini-api-fab")) return;

    let iconUrl, popupUrl;
    try {
        iconUrl = chrome.runtime.getURL("icon48.png");
        popupUrl = chrome.runtime.getURL("popup.html");
    } catch (e) {
        return;
    }

    function isAlive() {
        try {
            return !!chrome.runtime?.id;
        } catch (e) {
            return false;
        }
    }

    const styles = document.createElement("style");
    styles.textContent = `
        #gemini-api-fab {
            position: fixed;
            bottom: 24px;
            right: 24px;
            z-index: 2147483647;
            width: 52px;
            height: 52px;
            border-radius: 16px;
            background: rgba(15, 17, 24, 0.92);
            backdrop-filter: blur(16px);
            -webkit-backdrop-filter: blur(16px);
            border: 1px solid rgba(255, 255, 255, 0.12);
            box-shadow: 0 6px 28px rgba(0, 0, 0, 0.45), 0 0 0 1px rgba(66, 133, 244, 0.15) inset;
            display: flex;
            align-items: center;
            justify-content: center;
            cursor: grab;
            user-select: none;
            transition: box-shadow 0.25s ease, transform 0.2s ease;
        }
        #gemini-api-fab:hover {
            transform: scale(1.08);
            box-shadow: 0 10px 36px rgba(66, 133, 244, 0.35), 0 0 0 1px rgba(66, 133, 244, 0.25) inset;
        }
        #gemini-api-fab img {
            width: 28px;
            height: 28px;
            border-radius: 8px;
            pointer-events: none;
        }
        #gemini-api-fab-badge {
            position: absolute;
            top: -4px;
            right: -4px;
            min-width: 16px;
            height: 16px;
            border-radius: 8px;
            background: #64748b;
            box-shadow: 0 0 0 2px rgba(15, 17, 24, 0.9);
        }
        #gemini-api-fab.ok #gemini-api-fab-badge {
            background: #22c55e;
            box-shadow: 0 0 8px rgba(34, 197, 94, 0.7), 0 0 0 2px rgba(15, 17, 24, 0.9);
        }
        #gemini-api-fab.warn #gemini-api-fab-badge {
            background: #f59e0b;
            box-shadow: 0 0 8px rgba(245, 158, 11, 0.6), 0 0 0 2px rgba(15, 17, 24, 0.9);
        }
        #gemini-api-fab.err #gemini-api-fab-badge {
            background: #ef4444;
            box-shadow: 0 0 8px rgba(239, 68, 68, 0.6), 0 0 0 2px rgba(15, 17, 24, 0.9);
        }
        #gemini-api-panel {
            position: fixed;
            z-index: 2147483646;
            border-radius: 16px;
            overflow: hidden;
            box-shadow: 0 20px 60px rgba(0, 0, 0, 0.55), 0 0 0 1px rgba(255, 255, 255, 0.08);
            border: 1px solid rgba(255, 255, 255, 0.1);
            animation: geminiApiSlide 0.18s ease-out;
        }
        #gemini-api-panel iframe {
            width: 360px;
            height: 420px;
            border: none;
            display: block;
            background: #12141a;
        }
        @keyframes geminiApiSlide {
            from { opacity: 0; transform: translateY(8px) scale(0.97); }
            to { opacity: 1; transform: translateY(0) scale(1); }
        }
        #gemini-api-toast {
            position: fixed;
            bottom: 90px;
            right: 24px;
            z-index: 2147483645;
            max-width: 280px;
            padding: 10px 14px;
            border-radius: 12px;
            font: 600 12px/1.4 system-ui, -apple-system, sans-serif;
            color: #e2e8f0;
            background: rgba(15, 17, 24, 0.95);
            border: 1px solid rgba(255, 255, 255, 0.1);
            box-shadow: 0 8px 24px rgba(0, 0, 0, 0.4);
            opacity: 0;
            pointer-events: none;
            transition: opacity 0.25s ease;
        }
        #gemini-api-toast.show { opacity: 1; }
    `;
    document.documentElement.appendChild(styles);

    const fab = document.createElement("div");
    fab.id = "gemini-api-fab";
    fab.title = "Gemini Auth Helper — click to open / double-click to sync cookies";
    fab.innerHTML = `<img src="${iconUrl}" alt="Gemini Auth" /><span id="gemini-api-fab-badge"></span>`;

    const mount = () => {
        if (!document.body) return false;
        if (!document.getElementById("gemini-api-fab")) {
            document.body.appendChild(fab);
        }
        return true;
    };
    if (!mount()) {
        const obs = new MutationObserver(() => {
            if (mount()) obs.disconnect();
        });
        obs.observe(document.documentElement, { childList: true, subtree: true });
    }

    const toast = document.createElement("div");
    toast.id = "gemini-api-toast";
    document.documentElement.appendChild(toast);

    function showToast(text, ms = 2800) {
        toast.textContent = text;
        toast.classList.add("show");
        clearTimeout(showToast._t);
        showToast._t = setTimeout(() => toast.classList.remove("show"), ms);
    }

    // Drag
    let isDragging = false;
    let startX, startY, fabX, fabY, rafId = null;
    const BOX = 52;

    fab.addEventListener("mousedown", (e) => {
        if (e.button !== 0) return;
        isDragging = false;
        startX = e.clientX;
        startY = e.clientY;
        const rect = fab.getBoundingClientRect();
        fabX = rect.left;
        fabY = rect.top;
        fab.style.cursor = "grabbing";
        fab.style.transition = "none";
        e.preventDefault();

        const onMove = (ev) => {
            const dx = ev.clientX - startX;
            const dy = ev.clientY - startY;
            if (Math.abs(dx) > 3 || Math.abs(dy) > 3) isDragging = true;
            if (!isDragging) return;
            if (rafId) cancelAnimationFrame(rafId);
            rafId = requestAnimationFrame(() => {
                const x = Math.max(0, Math.min(window.innerWidth - BOX, fabX + dx));
                const y = Math.max(0, Math.min(window.innerHeight - BOX, fabY + dy));
                fab.style.left = `${x}px`;
                fab.style.top = `${y}px`;
                fab.style.right = "auto";
                fab.style.bottom = "auto";
            });
        };
        const onUp = () => {
            document.removeEventListener("mousemove", onMove);
            document.removeEventListener("mouseup", onUp);
            if (rafId) cancelAnimationFrame(rafId);
            fab.style.cursor = "grab";
            fab.style.transition = "box-shadow 0.25s ease, transform 0.2s ease";
        };
        document.addEventListener("mousemove", onMove);
        document.addEventListener("mouseup", onUp);
    });

    let panel = null;

    function closePanel() {
        if (panel) {
            panel.remove();
            panel = null;
            document.removeEventListener("click", closeOnOutside);
        }
    }

    function closeOnOutside(e) {
        if (panel && !panel.contains(e.target) && !fab.contains(e.target)) {
            closePanel();
        }
    }

    function openPanel() {
        if (!isAlive()) {
            showToast("Extension reloaded — refresh this page");
            return;
        }
        if (panel) {
            closePanel();
            return;
        }
        panel = document.createElement("div");
        panel.id = "gemini-api-panel";
        const iframe = document.createElement("iframe");
        iframe.src = popupUrl;
        panel.appendChild(iframe);

        const fabRect = fab.getBoundingClientRect();
        if (fabRect.top > 460) {
            panel.style.bottom = `${window.innerHeight - fabRect.top + 12}px`;
        } else {
            panel.style.top = `${fabRect.bottom + 12}px`;
        }
        panel.style.right = `${Math.max(8, window.innerWidth - fabRect.right)}px`;
        document.body.appendChild(panel);
        setTimeout(() => document.addEventListener("click", closeOnOutside), 80);
    }

    fab.addEventListener("click", () => {
        if (!isDragging) openPanel();
    });

    // Double-click: quick cookie sync without opening panel
    fab.addEventListener("dblclick", (e) => {
        e.preventDefault();
        e.stopPropagation();
        if (!isAlive()) {
            showToast("Extension reloaded — refresh this page");
            return;
        }
        showToast("Syncing Gemini cookies…");
        chrome.runtime.sendMessage({ type: "SYNC_GEMINI_COOKIES" }, (result) => {
            if (chrome.runtime.lastError) {
                showToast("Sync failed: " + chrome.runtime.lastError.message);
                refreshBadge();
                return;
            }
            if (result && result.ok) {
                showToast("✅ Cookies synced to Gemini API (port 8000)");
            } else {
                showToast("❌ " + ((result && result.error) || "Sync failed"));
            }
            refreshBadge();
        });
    });

    function setBadge(state) {
        fab.classList.remove("ok", "warn", "err");
        if (state) fab.classList.add(state);
    }

    function refreshBadge() {
        if (!isAlive()) {
            setBadge("err");
            return;
        }
        chrome.runtime.sendMessage({ type: "GET_GEMINI_COOKIES_STATUS" }, (status) => {
            if (chrome.runtime.lastError || !status) {
                setBadge("warn");
                return;
            }
            if (status.hasPsid && status.meta && status.meta.lastOk) {
                setBadge("ok");
            } else if (status.hasPsid) {
                setBadge("warn");
            } else {
                setBadge("err");
            }
        });
    }

    // Resize popup height from iframe
    window.addEventListener("message", (e) => {
        const d = e.data;
        if (!d || typeof d.__glabsPopupHeight !== "number") return;
        const ifr = document.querySelector("#gemini-api-panel iframe");
        if (ifr) {
            ifr.style.height = Math.max(160, Math.min(560, Math.ceil(d.__glabsPopupHeight))) + "px";
        }
    });

    refreshBadge();
    setInterval(refreshBadge, 5000);

    // Auto-sync cookies of the current Gemini session after page load
    function autoSync(reason) {
        if (!isAlive()) return;
        chrome.runtime.sendMessage({ type: "SYNC_GEMINI_COOKIES" }, () => {
            refreshBadge();
        });
    }
    setTimeout(() => autoSync("page-load"), 1500);
    setTimeout(() => autoSync("page-load-retry"), 8000);
    // Periodic soft refresh while tab is open
    setInterval(() => autoSync("page-interval"), 60000);
})();
