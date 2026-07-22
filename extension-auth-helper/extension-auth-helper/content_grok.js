

(() => {
    if (document.getElementById("glabs-grok-fab")) return;

    let iconUrl;
    try {
        iconUrl = chrome.runtime.getURL("icon48.png");
    } catch (e) {
        return;
    }

    function isAlive() {
        try { return !!chrome.runtime?.id; } catch (e) { return false; }
    }

    
    
    
    window.addEventListener("message", (e) => {
        if (e.source !== window) return;
        const d = e.data;
        if (!d || d.from !== "glabs-grok-task" || !d.taskId || !d.event) return;
        try {
            if (!isAlive()) return;
            chrome.runtime.sendMessage(d, () => {  });
        } catch (err) {  }
    });

    
    
    try {
        chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
            if (msg && msg.type === "GROK_CONTENT_PING") {
                try { sendResponse({ ready: true }); } catch (e) {}
                return false;
            }
            return false;
        });
    } catch (e) {  }

    const styles = document.createElement("style");
    styles.textContent = `
        #glabs-grok-fab {
            position: fixed; bottom: 20px; right: 20px; z-index: 2147483647;
            width: 48px; height: 48px; border-radius: 14px;
            background: rgba(15,17,24,0.85);
            backdrop-filter: blur(16px); -webkit-backdrop-filter: blur(16px);
            border: 1px solid rgba(255,255,255,0.08);
            box-shadow: 0 4px 24px rgba(0,0,0,0.4);
            display: flex; align-items: center; justify-content: center;
            cursor: grab; user-select: none;
            transition: box-shadow 0.3s ease, transform 0.2s ease;
        }
        #glabs-grok-fab:hover {
            box-shadow: 0 8px 32px rgba(99,102,241,0.25);
            transform: scale(1.08);
        }
        #glabs-grok-fab-badge {
            position: absolute; top: -5px; right: -5px;
            min-width: 18px; height: 18px; line-height: 18px; text-align: center;
            font-size: 10px; font-weight: 800; color: #fff;
            background: linear-gradient(135deg, #6366f1, #3b82f6);
            border-radius: 9px; padding: 0 5px;
            box-shadow: 0 2px 8px rgba(99,102,241,0.5);
        }
        #glabs-grok-fab.connected #glabs-grok-fab-badge {
            background: linear-gradient(135deg, #22c55e, #10b981);
        }
        #glabs-grok-fab.disconnected #glabs-grok-fab-badge {
            background: linear-gradient(135deg, #ef4444, #f43f5e);
        }
    `;
    document.head.appendChild(styles);

    const fab = document.createElement("div");
    fab.id = "glabs-grok-fab";
    fab.title = "Bawui Auth Helper — Grok mode";
    fab.innerHTML = `<img src="${iconUrl}" width="26" height="26" style="border-radius:7px;opacity:0.9;" /><span id="glabs-grok-fab-badge">G</span>`;
    document.body.appendChild(fab);

    
    let isDragging = false, startX, startY, fabX, fabY, rafId = null;
    const _FAB_BOX = 48;
    fab.addEventListener("mousedown", (e) => {
        isDragging = false;
        startX = e.clientX; startY = e.clientY;
        const r = fab.getBoundingClientRect();
        fabX = r.left; fabY = r.top;
        fab.style.cursor = "grabbing";
        e.preventDefault();
        const onMove = (e2) => {
            const dx = e2.clientX - startX, dy = e2.clientY - startY;
            if (Math.abs(dx) > 3 || Math.abs(dy) > 3) isDragging = true;
            if (!isDragging) return;
            if (rafId) cancelAnimationFrame(rafId);
            rafId = requestAnimationFrame(() => {
                const x = Math.max(0, Math.min(window.innerWidth - _FAB_BOX, fabX + dx));
                const y = Math.max(0, Math.min(window.innerHeight - _FAB_BOX, fabY + dy));
                fab.style.left = `${x}px`; fab.style.top = `${y}px`;
                fab.style.right = "auto"; fab.style.bottom = "auto";
            });
        };
        const onUp = () => {
            document.removeEventListener("mousemove", onMove);
            document.removeEventListener("mouseup", onUp);
            if (rafId) { cancelAnimationFrame(rafId); rafId = null; }
            fab.style.cursor = "grab";
        };
        document.addEventListener("mousemove", onMove);
        document.addEventListener("mouseup", onUp);
    });

    function updateBadge() {
        if (!isAlive()) { fab.remove(); clearInterval(timer); return; }
        try {
            chrome.runtime.sendMessage({ type: "GET_METRICS" }, (response) => {
                if (chrome.runtime.lastError || !response) {
                    fab.className = "disconnected";
                    return;
                }
                fab.className = response.connected ? "connected" : "disconnected";
            });
        } catch (e) {  }
    }

    updateBadge();
    const timer = setInterval(updateBadge, 3000);
})();
