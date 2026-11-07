// Same-page planning links reveal existing controls; they never save or reset a draft.
(function () {
    document.addEventListener("click", function (event) {
        const link = event.target.closest('a[href^="#trail-"]');
        if (!link) return;
        const target = document.getElementById(link.getAttribute("href").slice(1));
        if (!target) return;
        if (target.tagName === "DETAILS") target.open = true;
        for (let parent = target.parentElement; parent; parent = parent.parentElement) {
            if (parent.tagName === "DETAILS") parent.open = true;
        }
        requestAnimationFrame(function () {
            const focus = target.tagName === "DETAILS" ? target.querySelector("summary") : target;
            if (!focus.matches("button, input, textarea, select, a, [tabindex], summary")) {
                focus.setAttribute("tabindex", "-1");
            }
            focus.focus({preventScroll: true});
            target.scrollIntoView({block: "start", behavior: "auto"});
        });
    });
})();
