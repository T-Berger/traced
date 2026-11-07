/* Dash Img exposes no load/error properties. Forward only this viewer's events. */
(function () {
    "use strict";
    let sequence = 0;
    function report(event) {
        const img = event.target;
        if (!(img instanceof HTMLImageElement) || !img.classList.contains("rv-media-image")) {
            return;
        }
        const context = img.dataset.mediaContext;
        const attempt = img.dataset.mediaAttempt;
        if (!context || !attempt || !window.dash_clientside ||
                typeof window.dash_clientside.set_props !== "function") {
            return;
        }
        window.dash_clientside.set_props("trail-media-event", {
            data: {
                context: context,
                attempt: attempt,
                status: event.type === "load" ? "loaded" : "error",
                sequence: ++sequence
            }
        });
    }
    document.addEventListener("load", report, true);
    document.addEventListener("error", report, true);
    document.addEventListener("click", function (event) {
        if (!(event.target instanceof Element) || event.ctrlKey || event.metaKey ||
                event.shiftKey || event.button !== 0) {
            return;
        }
        const link = event.target.closest("a.rv-media-metadata-link");
        if (!link || link.getAttribute("href") !== "#rv-source") {
            return;
        }
        requestAnimationFrame(function () {
            const target = document.getElementById("rv-source");
            if (target && target.getClientRects().length) {
                target.setAttribute("tabindex", "-1");
                target.focus({preventScroll: true});
                target.scrollIntoView({block: "start", behavior: "auto"});
            }
        });
    });
})();
