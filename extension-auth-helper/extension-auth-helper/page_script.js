
(async () => {
    const scriptTag = document.querySelector("script[data-glabs-nonce]");
    let nonce = scriptTag ? scriptTag.dataset.glabsNonce : null;

    if (!nonce) {
        const containers = document.querySelectorAll("[id^='glabs-bridge-']");
        if (containers.length === 0) return;
        const container = containers[containers.length - 1];
        nonce = container.dataset.nonce;
    }

    const container = document.getElementById(`glabs-bridge-${nonce}`);
    if (!container) return;

    const siteKey = container.dataset.siteKey;
    const action = container.dataset.action;
    const checkOnly = container.dataset.checkOnly === "1";

    container.remove();

    if (checkOnly) {
        const available =
            typeof grecaptcha !== "undefined" && !!grecaptcha.enterprise;
        window.postMessage(
            {
                type: "GLABS_RENDER_PROBE",
                nonce,
                available,
                error: available ? null : "widget not found",
            },
            "*"
        );
        return;
    }

    try {
        if (typeof grecaptcha === "undefined" || !grecaptcha.enterprise) {
            throw new Error("widget not loaded");
        }

        await new Promise((res) => grecaptcha.enterprise.ready(res));

        const token = await grecaptcha.enterprise.execute(siteKey, { action });

        window.postMessage(
            { type: "GLABS_RENDER_DONE", nonce, token },
            "*"
        );
    } catch (err) {
        window.postMessage(
            {
                type: "GLABS_RENDER_DONE",
                nonce,
                error: err.message || String(err),
            },
            "*"
        );
    }
})();
