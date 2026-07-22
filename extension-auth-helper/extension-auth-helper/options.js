
const DEFAULT_PORT = 8000;
const inputPort = document.getElementById("syncPort");
const btnSave = document.getElementById("btnSave");
const statusMsg = document.getElementById("statusMsg");

// Load settings
document.addEventListener("DOMContentLoaded", () => {
    try {
        chrome.storage.local.get(["syncPort"], (data) => {
            if (chrome.runtime.lastError) {
                console.error("Error loading settings:", chrome.runtime.lastError);
                return;
            }
            inputPort.value = data.syncPort || DEFAULT_PORT;
        });
    } catch (e) {
        console.error("Storage API error:", e);
    }
});

// Save settings
btnSave.addEventListener("click", () => {
    const portVal = parseInt(inputPort.value, 10);

    if (isNaN(portVal) || portVal < 1 || portVal > 65535) {
        showStatus("Please enter a valid port number between 1 and 65535.", "error");
        return;
    }

    try {
        chrome.storage.local.set({ syncPort: portVal }, () => {
            if (chrome.runtime.lastError) {
                showStatus(`Failed to save settings: ${chrome.runtime.lastError.message}`, "error");
            } else {
                showStatus("Settings saved successfully! Port updated dynamically.", "success");
            }
        });
    } catch (e) {
        showStatus(`Storage API error: ${e.message}`, "error");
    }
});

function showStatus(text, type) {
    statusMsg.textContent = text;
    statusMsg.className = `status-msg show ${type}`;
    
    // Auto hide after 3 seconds
    if (window.statusTimeout) {
        clearTimeout(window.statusTimeout);
    }
    window.statusTimeout = setTimeout(() => {
        statusMsg.classList.remove("show");
        setTimeout(() => {
            statusMsg.className = "status-msg";
        }, 300);
    }, 3000);
}
