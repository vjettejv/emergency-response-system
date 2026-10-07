(function () {
    "use strict";
    const E = window.Emergency, links = document.querySelectorAll("[data-entry]"), menu = document.getElementById("public-menu"), navigation = document.getElementById("public-navigation");
    let revision = 0;
    document.getElementById("public-year").textContent = String(new Date().getFullYear());
    function closeMenu() {menu.setAttribute("aria-expanded", "false"); navigation.removeAttribute("data-open");}
    menu.hidden = false;
    menu.addEventListener("click", () => {
        const open = menu.getAttribute("aria-expanded") !== "true";
        menu.setAttribute("aria-expanded", String(open));
        if (open) navigation.setAttribute("data-open", ""); else closeMenu();
    });
    navigation.addEventListener("click", event => {if (event.target.closest("a")) closeMenu();});
    document.addEventListener("keydown", event => {if (event.key === "Escape" && menu.getAttribute("aria-expanded") === "true") {closeMenu(); menu.focus();}});
    function entry(role) {
        for (const link of links) {
            link.href = role ? E.homeURL(role) : "/login";
            link.querySelector("[data-entry-label]").textContent = role ? "Vào hệ thống" : link.dataset.entry;
        }
    }
    async function syncSession() {
        const version = ++revision, token = sessionStorage.getItem("emergency-token");
        entry(null);
        if (!token) return; // Guest landing makes no API requests.
        try {
            const response = await fetch("/realtime/config.json");
            if (!response.ok) return;
            const config = await response.json();
            const api = E.apiClient({base: config.apiBase, fetcher: fetch, token: () => token, unauthorized: () => {
                if (version === revision && sessionStorage.getItem("emergency-token") === token) sessionStorage.removeItem("emergency-token");
            }});
            const user = await api.request("auth/me/");
            if (version === revision && sessionStorage.getItem("emergency-token") === token && Object.hasOwn(E.homes, user.role)) entry(user.role);
        } catch (_) {
            // Keep the public page usable. /login will validate the session again.
        }
    }
    window.addEventListener("pageshow", event => {if (event.persisted) syncSession();}); // Recheck on browser back/bfcache, including logout.
    window.addEventListener("storage", event => {if (event.key === "emergency-token") syncSession();});
    window.addEventListener("pagehide", () => {revision++;});
    syncSession();
})();
