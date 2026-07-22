

(() => {
    
    if (document.getElementById("glabs-fab")) return;

    let iconUrl, popupUrl;
    try {
        iconUrl = chrome.runtime.getURL("icon48.png");
        popupUrl = chrome.runtime.getURL("popup.html");
    } catch (e) {
        return; 
    }

    
    function isAlive() {
        try { return !!chrome.runtime?.id; } catch (e) { return false; }
    }

    
    
    

    const styles = document.createElement("style");
    styles.textContent = `
        
        #glabs-fab {
            position: fixed; bottom: 20px; right: 20px; z-index: 2147483647;
            width: 48px; height: 48px; border-radius: 14px;
            background: rgba(15,17,24,0.85);
            backdrop-filter: blur(16px); -webkit-backdrop-filter: blur(16px);
            border: 1px solid rgba(255,255,255,0.08);
            box-shadow: 0 4px 24px rgba(0,0,0,0.4), 0 0 0 1px rgba(255,255,255,0.03) inset;
            display: flex; align-items: center; justify-content: center;
            cursor: grab; user-select: none;
            transition: box-shadow 0.3s ease, transform 0.2s ease;
        }
        #glabs-fab:hover {
            box-shadow: 0 8px 32px rgba(59,130,246,0.25), 0 0 0 1px rgba(59,130,246,0.15) inset;
            transform: scale(1.08);
        }

        
        #glabs-fab-badge {
            position: absolute; top: -5px; right: -5px;
            min-width: 18px; height: 18px; line-height: 18px; text-align: center;
            font-size: 10px; font-weight: 800; font-family: 'Inter', system-ui, sans-serif;
            color: #fff; background: linear-gradient(135deg, #3b82f6, #6366f1);
            border-radius: 9px; padding: 0 5px;
            box-shadow: 0 2px 8px rgba(59,130,246,0.5);
            transition: background 0.3s, box-shadow 0.3s;
        }
        
        #glabs-fab.connected #glabs-fab-badge {
            background: linear-gradient(135deg, #22c55e, #10b981);
            box-shadow: 0 2px 8px rgba(34,197,94,0.5);
        }
        
        #glabs-fab.disconnected #glabs-fab-badge {
            background: linear-gradient(135deg, #ef4444, #f43f5e);
            box-shadow: 0 2px 8px rgba(239,68,68,0.5);
        }

        
        #glabs-iframe-wrap {
            position: fixed; z-index: 2147483646;
            border-radius: 16px; overflow: hidden;
            box-shadow: 0 20px 60px rgba(0,0,0,0.6), 0 0 0 1px rgba(255,255,255,0.06) inset;
            border: 1px solid rgba(255,255,255,0.08);
            animation: glabsSlideIn 0.2s ease-out;
        }
        #glabs-iframe-wrap iframe {
            width: 360px; height: 384px; border: none;
            border-radius: 16px; display: block;
        }
        @keyframes glabsSlideIn {
            from { opacity: 0; transform: translateY(8px) scale(0.97); }
            to { opacity: 1; transform: translateY(0) scale(1); }
        }

        
        #glabs-overlay {
            position: fixed; inset: 0; z-index: 2147483640;
            background: rgba(8, 10, 18, 0.55);
            backdrop-filter: blur(2px); -webkit-backdrop-filter: blur(2px);
            pointer-events: none;
            opacity: 0;
            transition: opacity 0.5s ease;
            display: flex; align-items: center; justify-content: center;
            font-family: 'Inter', 'Segoe UI', system-ui, -apple-system, sans-serif;
        }
        #glabs-overlay.visible { opacity: 1; }

        #glabs-overlay-card {
            width: 320px; padding: 32px 28px 24px;
            background: rgba(15, 17, 28, 0.75);
            backdrop-filter: blur(24px); -webkit-backdrop-filter: blur(24px);
            border-radius: 20px;
            border: 1px solid rgba(255,255,255,0.08);
            box-shadow: 0 24px 80px rgba(0,0,0,0.5), 0 0 0 1px rgba(255,255,255,0.04) inset;
            text-align: center; color: #e2e8f0;
        }

        #glabs-overlay-icon {
            width: 48px; height: 48px; border-radius: 12px;
            margin: 0 auto 14px;
            box-shadow: 0 4px 20px rgba(59,130,246,0.3);
        }
        #glabs-overlay-title {
            font-size: 18px; font-weight: 700; color: #f1f5f9;
            letter-spacing: -0.3px; margin: 0 0 4px;
        }
        #glabs-overlay-subtitle {
            font-size: 12px; font-weight: 500; color: #94a3b8;
            text-transform: uppercase; letter-spacing: 1.5px; margin: 0 0 20px;
        }
        #glabs-overlay-status {
            display: flex; align-items: center; justify-content: center; gap: 8px;
            margin: 0 0 16px;
        }
        #glabs-overlay-dot {
            width: 8px; height: 8px; border-radius: 50%;
            background: #22c55e;
            box-shadow: 0 0 8px rgba(34,197,94,0.6);
            animation: glabsPulse 1.5s ease-in-out infinite;
        }
        @keyframes glabsPulse {
            0%, 100% { opacity: 1; transform: scale(1); }
            50% { opacity: 0.5; transform: scale(0.85); }
        }
        #glabs-overlay-status-text {
            font-size: 13px; font-weight: 600; color: #4ade80;
        }
        #glabs-overlay-counter {
            font-size: 28px; font-weight: 800; margin: 0 0 6px;
            background: linear-gradient(135deg, #60a5fa, #34d399);
            -webkit-background-clip: text; -webkit-text-fill-color: transparent;
            background-clip: text;
            letter-spacing: -0.5px;
        }
        #glabs-overlay-divider {
            height: 1px; margin: 16px 0;
            background: linear-gradient(90deg, transparent, rgba(255,255,255,0.1), transparent);
        }
        #glabs-overlay-footer {
            font-size: 11px; color: #64748b; line-height: 1.5;
        }
    `;
    document.head.appendChild(styles);

    
    
    
    window.addEventListener("message", (e) => {
        const d = e.data;
        if (!d || typeof d.__glabsPopupHeight !== "number") return;
        const ifr = document.querySelector("#glabs-iframe-wrap iframe");
        if (ifr) ifr.style.height = Math.max(120, Math.min(560, Math.ceil(d.__glabsPopupHeight))) + "px";
    });

    
    
    

    const fab = document.createElement("div");
    fab.id = "glabs-fab";
    fab.title = "Bawui Auth Helper";
    fab.innerHTML = `<img src="${iconUrl}" width="26" height="26" style="border-radius:7px;opacity:0.9;" /><span id="glabs-fab-badge">—</span>`;
    document.body.appendChild(fab);

    
    let isDragging = false, startX, startY, fabX, fabY, rafId = null;
    const _FAB_BOX = 48;

    fab.addEventListener("mousedown", (e) => {
        isDragging = false;
        startX = e.clientX; startY = e.clientY;
        const rect = fab.getBoundingClientRect();
        fabX = rect.left; fabY = rect.top;
        fab.style.cursor = "grabbing";
        fab.style.transition = "none";
        e.preventDefault();

        const onMouseMove = (e) => {
            const dx = e.clientX - startX, dy = e.clientY - startY;
            if (Math.abs(dx) > 3 || Math.abs(dy) > 3) isDragging = true;
            if (isDragging) {
                if (rafId) cancelAnimationFrame(rafId);
                rafId = requestAnimationFrame(() => {
                    const x = Math.max(0, Math.min(window.innerWidth - _FAB_BOX, fabX + dx));
                    const y = Math.max(0, Math.min(window.innerHeight - _FAB_BOX, fabY + dy));
                    fab.style.left = `${x}px`;
                    fab.style.top = `${y}px`;
                    fab.style.right = "auto";
                    fab.style.bottom = "auto";
                });
            }
        };
        const onMouseUp = () => {
            document.removeEventListener("mousemove", onMouseMove);
            document.removeEventListener("mouseup", onMouseUp);
            if (rafId) { cancelAnimationFrame(rafId); rafId = null; }
            fab.style.cursor = "grab";
            fab.style.transition = "box-shadow 0.3s ease, transform 0.2s ease";
        };
        document.addEventListener("mousemove", onMouseMove);
        document.addEventListener("mouseup", onMouseUp);
    });

    fab.addEventListener("click", () => {
        if (!isDragging && isAlive()) togglePanel();
    });

    
    
    

    let panel = null;

    function togglePanel() {
        if (!isAlive()) return;
        if (panel) { panel.remove(); panel = null; return; }

        panel = document.createElement("div");
        panel.id = "glabs-iframe-wrap";

        const iframe = document.createElement("iframe");
        iframe.src = popupUrl;
        panel.appendChild(iframe);

        
        const fabRect = fab.getBoundingClientRect();
        if (fabRect.top > 480) {
            panel.style.bottom = `${window.innerHeight - fabRect.top + 10}px`;
        } else {
            panel.style.top = `${fabRect.bottom + 10}px`;
        }
        const rightPos = window.innerWidth - fabRect.right;
        panel.style.right = `${Math.max(8, rightPos)}px`;

        document.body.appendChild(panel);
        setTimeout(() => document.addEventListener("click", closeOnOutsideClick), 100);
    }

    function closeOnOutsideClick(e) {
        if (panel && !panel.contains(e.target) && !fab.contains(e.target)) {
            panel.remove(); panel = null;
            document.removeEventListener("click", closeOnOutsideClick);
        }
    }

    
    
    

    const overlay = document.createElement("div");
    overlay.id = "glabs-overlay";
    overlay.innerHTML = `
        <div id="glabs-overlay-card">
            <img id="glabs-overlay-icon" src="${iconUrl}" alt="Bawui" />
            <div id="glabs-overlay-title">Bawui Automation</div>
            <div id="glabs-overlay-subtitle">Auth Helper Active</div>
            <div id="glabs-overlay-status">
                <div id="glabs-overlay-dot"></div>
                <span id="glabs-overlay-status-text">Processing...</span>
            </div>
            <div id="glabs-overlay-counter">Session Token #0</div>
            <div id="glabs-overlay-divider"></div>
            <div id="glabs-overlay-footer">
                🤖 Automated authentication in progress<br>
                This tab is managed by Bawui Automation
            </div>
        </div>
    `;
    document.body.appendChild(overlay);

    let overlayVisible = false;

    function showOverlay(data) {
        if (!overlayVisible) {
            overlay.classList.add("visible");
            overlayVisible = true;
        }
        updateOverlayData(data);
    }

    function hideOverlay() {
        if (overlayVisible) {
            overlay.classList.remove("visible");
            overlayVisible = false;
        }
    }

    function updateOverlayData(data) {
        if (!data) return;
        const counter = document.getElementById("glabs-overlay-counter");
        if (counter) {
            counter.textContent = `Session Token #${data.sessionCount || data.tokenCount || 0}`;
        }
    }

    
    
    

    chrome.runtime.onMessage.addListener((message) => {
        if (message.type === "LAYOUT_CHANGED") {
            if (message.active) showOverlay(message);
            else hideOverlay();
        }
    });

    
    
    

    let badgeTimer = null;

    function updateBadge() {
        if (!isAlive()) {
            
            if (badgeTimer) clearInterval(badgeTimer);
            const el = document.getElementById("glabs-fab");
            if (el) el.remove();
            if (panel) { panel.remove(); panel = null; }
            hideOverlay();
            return;
        }
        try {
            chrome.runtime.sendMessage({ type: "GET_METRICS" }, (response) => {
                if (chrome.runtime.lastError) return;
                const badge = document.getElementById("glabs-fab-badge");
                if (!badge) return;
                if (response) {
                    badge.textContent = response.tokenCount || "0";
                    fab.className = response.connected ? "connected" : "disconnected";
                    if (response.active && !overlayVisible) showOverlay(response);
                    else if (!response.active && overlayVisible) hideOverlay();
                    else if (response.active && overlayVisible) updateOverlayData(response);
                } else {
                    badge.textContent = "!";
                    fab.className = "disconnected";
                    if (overlayVisible) hideOverlay();
                }
            });
        } catch (e) {  }
    }

    updateBadge();
    badgeTimer = setInterval(updateBadge, 3000);

    
    
    
    
    
    
    function* _scanNodes(root) {
        let els;
        try { els = root.querySelectorAll("*"); } catch (e) { return; }
        for (const el of els) {
            yield el;
            if (el.shadowRoot) yield* _scanNodes(el.shadowRoot);   
        }
    }
    function _throttleMedia() {
        let paused = 0;
        for (const el of _scanNodes(document)) {
            if (el.tagName === "VIDEO" && !el.paused && !el.ended) {
                try { el.pause(); paused++; } catch (e) {  }
            }
        }
        if (paused) void 0;
    }
    
    
    
    document.addEventListener("play", (e) => {
        const t = e.target;
        if (t && t.tagName === "VIDEO") {
            try { t.pause(); } catch (err) {  }
        }
    }, true);
    _throttleMedia();   
    setInterval(() => { try { _throttleMedia(); } catch (e) {  } }, 2000);

    // Sync Google Flow Page HTML when user visits labs.google Flow page
    if (window.location.href.includes("labs.google") && window.location.href.includes("/flow")) {
        const runSync = async () => {
            try {
                const html = document.documentElement.outerHTML;
                await fetch("http://127.0.0.1:18923/sync/google-flow-page", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ html })
                });
            } catch (e) {  }
        };
        // Run after load and also after a short delay to capture dynamic state
        window.addEventListener("load", () => {
            setTimeout(runSync, 3000);
        });
        setTimeout(runSync, 5000);
    }
})();
