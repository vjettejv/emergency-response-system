(async function () {
    "use strict";
    const E = window.Emergency, app = document.getElementById("app");
    const labels = {citizen: "Người dân", dispatcher: "Điều phối viên", rescue_team: "Đội ứng cứu", admin: "Quản trị viên", pending: "Chờ xác minh", accepted: "Đã chấp nhận", rejected: "Đã từ chối", new: "Mới", verified: "Đã xác minh", dispatched: "Đã điều phối", in_progress: "Đang xử lý", resolved: "Đã giải quyết", cancelled: "Đã hủy", assigned: "Đã giao", en_route: "Đang di chuyển", on_scene: "Tại hiện trường", responding: "Đang ứng cứu", completed: "Hoàn thành", available: "Sẵn sàng", busy: "Đang làm nhiệm vụ", offline: "Ngoại tuyến", ready: "Sẵn sàng", deleting: "Đang xóa", failed: "Lỗi", recorded: "Đã ghi nhận", draft: "Bản nháp · chưa gửi"};
    const state = {token: sessionStorage.getItem("emergency-token") || "", user: null, categories: [], map: null, socket: null, gps: null, generation: 0, refresh: null, refreshJob: null, connection: "Đang tải…", connectionState: "idle", gpsText: "Chưa chia sẻ vị trí", gpsState: "idle", currentPosition: null, teamName: "", teamStatus: null, follow: true, sound: false, liveMessages: []};
    let config, api;
    Object.assign(state, {syncEpoch: 0, displayState: "loading", lastUpdated: null, snapshotReady: false, refreshError: false});
    function setDisplayState(mode, message = "") {
        state.displayState = mode;
        const shell = document.getElementById("ui-shell"); if (shell) {shell.dataset.viewState = mode; shell.dataset.hasSnapshot = String(!!state.lastUpdated);}
        const content = document.getElementById("content"); content?.setAttribute("aria-busy", String(mode === "loading"));
        const editableReport = location.hash.split("?")[0] === "#/citizen/report";
        if (content) content.inert = mode !== "success" && !editableReport;
        if (shell) shell.dataset.editableReport = String(editableReport);
        if (editableReport && content) {const submit = content.querySelector(".report-submit"); if (submit) submit.disabled = navigator.onLine === false;}
        if (!state.notice) return;
        state.notice.hidden = mode === "success";
        state.notice.dataset.state = mode;
        state.notice.setAttribute("role", ["error", "offline", "stale"].includes(mode) ? "alert" : "status");
        state.noticeTitle.textContent = message || (mode === "loading" ? "Đang tải…" : mode === "error" ? "Không tải được dữ liệu. Vui lòng thử lại." : state.connectionState === "pending" || state.connectionState === "syncing" ? state.connection : "Mất kết nối");
        state.noticeMeta.textContent = state.lastUpdated ? `Dữ liệu gần nhất · ${E.friendlyTime(state.lastUpdated)}` : "";
        state.noticeRetry.hidden = mode === "loading" || state.connectionState === "syncing";
    }
    function snapshotCompleted() {
        state.lastUpdated = new Date().toISOString(); state.snapshotReady = true; state.refreshError = false;
        const live = state.user?.role !== "citizen";
        setDisplayState(live && state.connectionState !== "online" ? "stale" : "success");
    }
    function node(tag, attrs = {}, ...children) {
        const element = document.createElement(tag);
        for (const [key, value] of Object.entries(attrs)) {
            if (key === "class") element.className = value;
            else if (key.startsWith("on")) element.addEventListener(key.slice(2), value);
            else if (key in element) element[key] = value;
            else element.setAttribute(key, value);
        }
        for (const child of children.flat()) if (child !== null && child !== undefined) element.append(child.nodeType ? child : document.createTextNode(String(child)));
        return element;
    }
    const button = (text, action, secondary = true) => node("button", {type: "button", class: secondary ? "secondary" : "", onclick: action}, text);
    const badge = status => node("span", {class: `badge ${Object.hasOwn(labels, status) ? status : "unknown"}`}, labels[status] || "Chưa cập nhật");
    const errorBox = () => node("p", {class: "error", role: "alert"});
    const businessNote = value => /^Created from accepted report \d+\.$/.test(value || "") || ["Response team assigned.", "Response team arrived on scene.", "No active assignment remains; awaiting dispatch."].includes(value) ? "" : value || "";
    const noteSuffix = value => businessNote(value) ? " · " + businessNote(value) : "";
    const date = value => value ? E.friendlyTime(value) : "Chưa ghi nhận";
    const incidentTitle = incident => (incident.title || categoryName(incident.category)).replace(/\s*[·—–-]\s*báo cáo #\d+\s*$/i, "");
    const categoryName = id => state.categories.find(category => category.id === id)?.name || "Sự cố";
    function icon(name) {
        const paths = {home: 'm3 10 9-7 9 7M5 9v12h14V9M9 21v-8h6v8', tasks: 'M8 4H5v17h14V4h-3M8 3h8v4H8zM8 11h8M8 15h6', bell: 'M18 8a6 6 0 0 0-12 0v5l-2 4h16l-2-4V8M10 21h4', history: 'M3 11a9 9 0 1 1 2 7M3 4v7h7M12 7v5l3 2', user: 'M16 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0M4 21v-2a8 8 0 0 1 16 0v2', locate: 'M12 3v3m0 12v3M3 12h3m12 0h3M18 12a6 6 0 1 1-12 0 6 6 0 0 1 12 0', layers: 'm3 8 9-5 9 5-9 5-9-5m0 5 9 5 9-5m-18 5 9 5 9-5', shield: 'M12 3 4 6v6c0 5 8 9 8 9s8-4 8-9V6l-8-3m0 4v6m0 3v1', sound: 'M3 9h4l5-5v16l-5-5H3zM16 8a6 6 0 0 1 0 8m3-11a10 10 0 0 1 0 14', arrow: 'M5 12h14m-6-6 6 6-6 6', pin: 'M19 9c0 5-7 12-7 12S5 14 5 9a7 7 0 0 1 14 0M15 9a3 3 0 1 1-6 0 3 3 0 0 1 6 0', close: 'm6 6 12 12M6 18 18 6', refresh: 'M20 8a9 9 0 1 0 1 6M20 3v5h-5', plus: 'M12 4v16M4 12h16', check: 'm5 12 4 4L19 6', map: 'm3 5 6-2 6 2 6-2v16l-6 2-6-2-6 2V5m6-2v16m6-14v16'};
        const categoryPaths = {
            medical: 'M9 3h6v6h6v6h-6v6H9v-6H3V9h6z',
            fire: 'M12 3c1 5 6 6 6 11a6 6 0 0 1-12 0c0-3 2-5 4-7v5c2-2 3-5 2-9z',
            traffic: 'm5 10 2-6h10l2 6M3 10h18v8H3zM5 18v3m14-3v3M6 14h2m8 0h2',
            flood: 'M3 7c3-3 6 3 9 0s6 3 9 0M3 12c3-3 6 3 9 0s6 3 9 0M3 17c3-3 6 3 9 0s6 3 9 0',
            other: 'M5 12a1 1 0 1 0-2 0 1 1 0 0 0 2 0M13 12a1 1 0 1 0-2 0 1 1 0 0 0 2 0M21 12a1 1 0 1 0-2 0 1 1 0 0 0 2 0',
        };
        const element = node("span", {class: "icon", "aria-hidden": "true"});
        element.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="${paths[name] || categoryPaths[name] || paths.tasks}"/></svg>`;
        return element;
    }
    function quickAction(name, title, action) { const control = button("", action); control.append(icon(name), node("span", {}, title)); return control; }
    function disclosure(title, ...children) { return node("details", {class: "disclosure"}, node("summary", {}, title), ...children); }
    function bottomSheet(body, onSnap = () => {}) {
        const sheet = node("section", {class: "mission-sheet", "data-snap": "half", "aria-label": "Thông tin và thao tác nhiệm vụ"});
        const handle = button("", () => {if (moved) {moved = false; return;} setSnap(sheet.dataset.snap === "expanded" ? "half" : "expanded");});
        handle.className = "sheet-handle"; handle.setAttribute("aria-label", "Mở rộng hoặc thu gọn nhiệm vụ"); handle.setAttribute("aria-expanded", "false");
        handle.append(node("span", {class: "grip"}), node("span", {class: "sheet-label"}, "Nhiệm vụ & thao tác"));
        let start = null, moved = false;
        function setSnap(value) { sheet.dataset.snap = value; handle.setAttribute("aria-expanded", String(value === "expanded")); onSnap(); }
        handle.addEventListener("pointerdown", event => {start = event.clientY; moved = false; handle.setPointerCapture?.(event.pointerId);});
        handle.addEventListener("pointermove", event => {if (start !== null && Math.abs(event.clientY - start) > 10) moved = true;});
        handle.addEventListener("pointerup", event => {if (start !== null && moved) {setSnap(E.sheetSnap(sheet.dataset.snap, event.clientY - start)); event.preventDefault();} start = null;});
        handle.addEventListener("pointercancel", () => {start = null;});
        const footer = node("div", {class: "sheet-footer", hidden: true}); sheet.append(handle, body, footer); return {element: sheet, setSnap, footer};
    }
    function signal(text) { return node("span", {class: `signal${text === "Đã cập nhật" ? " signal-live" : ""}`}, text); }
    function missionTone() {
        if (!state.sound || state.audio?.state !== "running") return;
        const sound = state.audio.createOscillator(), gain = state.audio.createGain(), at = state.audio.currentTime;
        sound.frequency.value = 660; gain.gain.setValueAtTime(.05, at); gain.gain.exponentialRampToValueAtTime(.001, at + .18);
        sound.connect(gain); gain.connect(state.audio.destination); sound.start(at); sound.stop(at + .2); sound.onended = () => {sound.disconnect(); gain.disconnect();};
    }
    function notify(message, error = false) { const toast = node("div", {class: `toast${error ? " error" : ""}`}, message); document.getElementById("notifications").append(toast); setTimeout(() => toast.remove(), error ? 12000 : 6000); }
    function go(route) { if (location.hash.slice(1) === route) render(); else location.hash = route; }
    function confirm(message, information = false) {
        const dialog = document.getElementById("confirm-dialog"); document.getElementById("confirm-text").textContent = message;
        document.getElementById("confirm-title").textContent = information ? "Hỗ trợ & liên lạc" : "Xác nhận thao tác";
        const accept = dialog.querySelector('button[value="confirm"]'), cancel = dialog.querySelector('button[value="cancel"]');
        if (accept) accept.textContent = information ? "Đã hiểu" : "Xác nhận"; if (cancel) cancel.textContent = information ? "Đóng" : "Quay lại";
        return new Promise(resolve => { dialog.returnValue = "cancel"; dialog.addEventListener("close", () => resolve(dialog.returnValue === "confirm"), {once: true}); dialog.showModal(); });
    }
    async function perform(control, error, operation) {
        control.disabled = true; error.textContent = "";
        try { await operation(); } catch (failure) { if (failure.status === 0) {state.socket?.pause(); updateConnection("Mất kết nối", "offline");} error.textContent = E.publicError(failure); error.scrollIntoView?.({block: "nearest"}); }
        finally { control.disabled = false; }
    }
    function field(title, name, type = "text", options = {}) {
        const input = node(type === "textarea" ? "textarea" : type === "select" ? "select" : "input", {name, ...(type === "textarea" || type === "select" ? {} : {type}), ...options});
        return node("label", {class: `field${type === "textarea" ? " full" : ""}`}, node("span", {}, title), input);
    }
    function select(title, name, choices, value = "", blank = "Tất cả") {
        const label = field(title, name, "select"), input = label.querySelector("select");
        if (blank !== null) input.append(node("option", {value: ""}, blank));
        choices.forEach(([id, text]) => input.append(node("option", {value: id}, text))); input.value = value; return label;
    }
    const categoryChoices = () => state.categories.map(category => [category.id, category.name]);
    const formData = form => Object.fromEntries(new FormData(form));
    function heading(title, description, action) { return node("div", {class: "page-heading"}, node("div", {}, node("p", {class: "eyebrow"}, "Trung tâm ứng phó"), node("h1", {}, title), node("p", {}, description)), action ? node("div", {class: "actions"}, action) : null); }
    function panel(title, ...contents) { return node("section", {class: "panel"}, title ? node("div", {class: "panel-header"}, node("h2", {}, title)) : null, ...contents); }
    function details(values) { return node("dl", {class: "detail-list"}, ...values.flatMap(([title, value]) => [node("dt", {}, title), node("dd", {}, value)])); }
    function record(title, status, description, actions = []) { return node("article", {class: "record"}, node("div", {class: "record-top"}, node("h3", {}, title), badge(status)), node("p", {}, description), node("div", {class: "actions"}, ...actions)); }
    function records(container, items, renderer, empty = "Chưa có dữ liệu phù hợp.") { container.dataset.state = items.length ? "success" : "empty"; container.replaceChildren(...(items.length ? items.map(renderer) : [node("p", {class: "empty", role: "status"}, empty)])); }
    function mountMap(container, small = false) {
        if (state.drawerMap) return state.drawerMap;
        const element = node("div", {class: `map${small ? " small" : ""}`, "aria-label": "Bản đồ hiện trường"}); container.append(element);
        const mapStatus = node("p", {class: "map-status", role: "status"}, "Đang tải bản đồ nền…"); container.append(mapStatus);
        state.map = window.EmergencyMap.createMap(element, window.L, {tileUrl: config.tileUrl, showCoordinates: false, onStatus: text => {mapStatus.textContent = text;}}); return state.map;
    }
    function clearSession(message) {
        if (state.socket) state.socket.stop(); if (state.gps) state.gps.stop();
        state.socket = null; state.gps = null; state.token = ""; state.user = null; state.categories = []; sessionStorage.removeItem("emergency-token");
        state.currentPosition = null; state.teamName = ""; state.teamStatus = null; state.liveMessages = [];
        state.sound = false; state.audio?.close(); state.audio = null;
        go("/login"); if (message) notify(message, true);
    }
    function updateConnection(text, status) {
        if (status !== "online" && status !== state.connectionState) state.syncEpoch++;
        state.connection = text; state.connectionState = status;
        const element = document.getElementById("connection"); if (element) { element.textContent = text; element.dataset.state = status; }
        if (status !== "online" && status !== "idle") {
            if (state.gps && state.gpsState !== "idle") state.gps.stop(); state.map?.freeze?.();
            document.querySelectorAll?.("dialog[open]")?.forEach(dialog => dialog.close());
            if (state.refreshError && state.displayState === "error") {state.noticeRetry.hidden = status === "syncing"; return;}
            setDisplayState(state.lastUpdated ? "stale" : status === "offline" ? "offline" : "loading");
        } else if (state.snapshotReady && !state.refreshError) setDisplayState("success");
    }
    function gpsStatus(text, status) {
        state.gpsText = text; state.gpsState = status;
        if (["offline", "idle"].includes(status) && state.user?.role === "rescue_team") {
            state.currentPosition = null; state.map?.replace("team", [], () => "");
            const distance = document.getElementById("mission-distance"); if (distance) distance.textContent = "Chia sẻ vị trí để biết khoảng cách";
        }
        const element = document.getElementById("gps-status"); if (element) {element.textContent = text; element.dataset.state = status;}
        const start = document.getElementById("gps-start"), stop = document.getElementById("gps-stop"); if (start) start.disabled = status === "pending" || status === "online"; if (stop) stop.hidden = status === "idle";
        const chip = document.getElementById("gps-chip"); if (chip) {chip.hidden = status === "idle"; chip.textContent = status === "online" ? "Đang chia sẻ vị trí" : status === "pending" ? "Đang lấy vị trí…" : status === "offline" ? "Vị trí chưa cập nhật" : "Chưa chia sẻ vị trí"; chip.dataset.state = status;}
    }
    async function refresh(force = false) {
        if (!state.refresh) return true;
        const active = document.activeElement;
        // Background snapshots must not replace a field or move a clicked action
        // while the user is editing or deciding in a confirmation dialog.
        if (!force && (document.querySelector("dialog[open]")?.open || (active?.closest?.("#content") && ["INPUT", "TEXTAREA", "SELECT"].includes(active.tagName)))) {state.refreshPending = true; return false;}
        state.refreshPending = false;
        if (state.refreshJob && state.refreshJob.generation === state.generation) { const existing = state.refreshJob; existing.queued = true; return existing.promise.then(() => existing.ok); }
        const job = {generation: state.generation, queued: false, ok: false}, handler = state.refresh;
        state.refreshJob = job;
        if (!state.lastUpdated || force && state.connectionState === "online") setDisplayState("loading");
        job.promise = (async () => {
            do {
                job.queued = false; const epoch = state.syncEpoch;
                try { await handler(); if (job.generation === state.generation && epoch === state.syncEpoch) {job.ok = true; state.refreshError = false; if (!state.rendering) snapshotCompleted(); if (state.mediaGuard) state.mediaGuard();} else {job.ok = false; if (job.generation === state.generation) state.refreshError = true;} }
                catch (error) {if (job.generation === state.generation) {job.ok = false; state.refreshError = true; if (error.name !== "ObsoleteSnapshot") { setDisplayState(error.status === 0 ? state.lastUpdated ? "stale" : "offline" : "error", E.publicError(error));}}}
            } while (job.queued && job.generation === state.generation);
        })();
        try { await job.promise; return job.ok; } finally { if (state.refreshJob === job) state.refreshJob = null; }
    }
    let deferredRefreshTimer;
    document.addEventListener?.("focusout", () => {
        clearTimeout(deferredRefreshTimer);
        deferredRefreshTimer = setTimeout(() => {if (state.refreshPending) refresh();}, 150);
    });
    function connectRealtime() {
        if (state.user.role === "citizen") { updateConnection("Cập nhật mỗi 30 giây", "idle"); return; }
        if (state.user.role === "rescue_team" && !state.user.response_team) { updateConnection("Chưa được gán đội", "offline"); notify("Tài khoản chưa thuộc đội ứng cứu. Liên hệ quản trị viên để gán đội trước khi nhận nhiệm vụ hoặc chia sẻ GPS.", true); return; }
        const url = new URL(state.user.role === "rescue_team" ? config.rescueSocket : config.dispatcherSocket, location.origin); url.protocol = location.protocol === "https:" ? "wss:" : "ws:";
        state.socket = E.realtime({url: url.href, token: () => state.token, Socket: WebSocket, timers: window, now: Date.now, state: updateConnection, ready: () => refresh(true), revoked: () => clearSession("Phiên hoặc quyền đã thay đổi. Vui lòng đăng nhập lại."), event: event => {
            if (event.type === "team.location_updated" && state.displayState === "success" && state.map && state.user.role !== "rescue_team" && (!state.visibleTeamIds || state.visibleTeamIds.has(event.data.team_id))) state.map.update("team", event.data, event.data.name || "Đội ứng cứu");
            else if (["incident.status_changed", "assignment.status_changed", "report.changed", "reports.linked", "assignment.signal_created", "media.confirmed"].includes(event.type)) {
                const ended = (event.type === "assignment.status_changed" && ["completed", "cancelled", "rejected"].includes(event.data.status)) || (event.type === "incident.status_changed" && ["resolved", "cancelled"].includes(event.data.status));
                if (ended) {
                    state.contactEpoch++; state.contactNodes?.forEach(card => card.remove()); state.contactNodes?.clear();
                    for (const dialog of document.querySelectorAll?.("dialog[open]") || []) dialog.close();
                }
                refresh(ended);
            }
            if (event.type === "assignment.signal_created" && state.user.role !== "rescue_team") {
                const notice = node("div", {class: "toast", role: "status"}, `Đội ứng cứu gửi ${event.data.kind === "support" ? "yêu cầu thêm lực lượng" : "báo vấn đề"}.`, button("Xem sự cố", () => go(`/dispatcher/incidents/${event.data.incident_id}`)));
                document.getElementById("notifications").append(notice); setTimeout(() => notice.remove(), 15000);
            }
            if (event.type === "assignment.status_changed" && state.user.role === "rescue_team") {state.teamStatus = null; state.liveMessages.unshift({at: new Date().toISOString(), text: `Nhiệm vụ: ${labels[event.data.status] || "Đã cập nhật"}`}); state.liveMessages = state.liveMessages.slice(0, 20); if (event.data.status === "assigned") missionTone();}
        }});
    }
    function shell(route) {
        const role = state.user.role;
        const links = role === "citizen" ? [["/citizen/report", "Báo cáo mới", "plus"], ["/citizen/reports", "Báo cáo của tôi", "history"]] : role === "rescue_team" ? [["/rescue", "Trang chủ", "home"], ["/rescue?view=tasks", "Nhiệm vụ", "tasks"], ["/rescue?view=updates", "Cập nhật", "bell"], ["/rescue?view=history", "Lịch sử", "history"], ["/rescue?view=profile", "Hồ sơ", "user"]] : role === "dispatcher" ? [["/dispatcher", "Bản đồ tác chiến", "map"], ["/dispatcher?view=reports", "Tiếp nhận báo cáo", "tasks"]] : [["/admin/users", "Tài khoản & phân quyền", "user"], ["/admin/categories", "Loại sự cố", "layers"], ["/admin/teams", "Đội ứng cứu", "shield"], ["/admin/config", "Cấu hình", "tasks"], ["/dispatcher", "Điều phối", "map"]];
        const active = path => route === path || (path === "/citizen/reports" && route.startsWith("/citizen/reports/")) || (path === "/dispatcher" && route.startsWith("/dispatcher/")) || (path === "/rescue?view=tasks" && route.startsWith("/rescue/assignments/"));
        const nav = node("nav", {"aria-label": "Điều hướng theo vai trò"}, ...links.map(([path, title, name]) => node("a", {href: "#" + path, "aria-label": title, title, class: active(path) ? "active" : "", ...(active(path) ? {"aria-current": "page"} : {})}, icon(name), node("span", {}, title))));
        const logoutSession = async () => {try {await api.request("auth/logout/", "POST", {}); clearSession();} catch (_) {clearSession("Đã đăng xuất trên thiết bị này.");}};
        state.logout = logoutSession;
        const logout = button("Đăng xuất", logoutSession), exit = button("Thoát", logoutSession); exit.className = `secondary account-exit${role !== "citizen" ? " mobile-exit" : ""}`;
        const content = node("main", {class: "content", id: "content", tabIndex: -1});
        state.noticeTitle = node("strong"); state.noticeMeta = node("small");
        state.noticeRetry = button("Thử lại", () => {if (state.socket && state.connectionState !== "online") state.socket.retry(); else if (state.refresh) refresh(true); else render();});
        state.notice = node("aside", {class: "view-status", "aria-live": "polite"}, node("div", {}, state.noticeTitle, state.noticeMeta), state.noticeRetry);
        const connection = node("span", {id: "connection", class: "connection", "data-state": state.connectionState, role: "status"}, state.connection);
        const isDispatch = route.startsWith("/dispatcher"), isRescue = role === "rescue_team";
        app.replaceChildren(node("div", {id: "ui-shell", class: `shell role-${role}${isDispatch ? " dispatch-shell" : ""}${isRescue ? " rescue-shell" : ""}`}, node("aside", {class: "sidebar"}, brand(), nav, node("div", {class: "account"}, node("div", {}, node("strong", {}, state.user.username), node("br"), node("small", {}, labels[role])), logout)), node("div", {class: "workspace"}, node("header", {class: "topbar"}, node("div", {class: "workspace-title"}, icon(isRescue ? "shield" : isDispatch ? "map" : "home"), node("strong", {}, isRescue ? "Ứng cứu hiện trường" : isDispatch ? "Trung tâm điều phối" : "Trung tâm ứng phó"), node("small", {}, labels[role])), node("div", {class: "actions"}, connection, button("Làm mới", () => refresh(true)), !isRescue ? exit : null)), state.notice, content)));
        return content;
    }
    function brand(tagline = "KẾT NỐI - ĐIỀU PHỐI") {
        const mark = node("span", {class: "brand-icon", "aria-hidden": "true"});
        mark.innerHTML = '<svg viewBox="0 0 24 28" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round"><path d="m12 2 8 3.5v7c0 5-8 10-8 10s-8-5-8-10v-7L12 2Z" stroke-width="2.1"/><path d="M12 7.5v6" stroke-width="2.5"/><circle cx="12" cy="17.5" r="1.25" fill="currentColor" stroke="none"/></svg>';
        return node("div", {class: "brand"}, mark, node("div", {}, node("strong", {}, "ỨNG PHÓ"), node("small", {}, tagline)));
    }
    function login() {
        let registering = false, busy = false, visible = false;
        const form = node("form", {class: "auth-form"}), error = node("p", {class: "error auth-error", id: "login-error", role: "alert", "aria-live": "polite"});
        const title = node("h2", {id: "login-title"}, "Chào mừng trở lại"), subtitle = node("p", {class: "auth-subtitle"}, "Đăng nhập để tiếp tục vào hệ thống.");
        const username = field("Tên đăng nhập", "username", "text", {id: "login-username", required: true, autoComplete: "username", autoCapitalize: "none", spellcheck: false, "aria-describedby": "login-error"});
        const usernameInput = username.querySelector("input"), passwordInput = node("input", {id: "login-password", name: "password", type: "password", required: true, autoComplete: "current-password", "aria-describedby": "login-error"});
        const visibility = button("", () => {visible = !visible; updateVisibility();}); visibility.className = "auth-visibility";
        function updateVisibility() {
            passwordInput.type = visible ? "text" : "password";
            visibility.setAttribute("aria-label", visible ? "Ẩn mật khẩu" : "Hiện mật khẩu");
            visibility.setAttribute("aria-pressed", String(visible)); visibility.setAttribute("aria-controls", "login-password");
            const eye = node("span", {class: "icon", "aria-hidden": "true"});
            eye.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/>${visible ? '<path d="m3 3 18 18"/>' : ""}</svg>`;
            visibility.replaceChildren(eye);
        }
        updateVisibility();
        const password = node("div", {class: "field auth-password"}, node("label", {htmlFor: "login-password"}, "Mật khẩu"), node("div", {class: "auth-password-control"}, passwordInput, visibility));
        const spinner = node("span", {class: "auth-spinner", hidden: true, "aria-hidden": "true"}), submitLabel = node("span", {}, "Đăng nhập");
        const submit = node("button", {type: "submit", class: "auth-submit"}, spinner, submitLabel);
        const switchPrompt = node("span", {}, "Chưa có tài khoản?"), toggle = button("Đăng ký", () => {if (busy) return; registering = !registering; updateMode(); error.textContent = "";}); toggle.className = "auth-link";
        function updateMode() {
            title.textContent = registering ? "Tạo tài khoản người dân" : "Chào mừng trở lại";
            subtitle.textContent = registering ? "Đăng ký để gửi và theo dõi báo cáo sự cố." : "Đăng nhập để tiếp tục vào hệ thống.";
            submitLabel.textContent = registering ? "Tạo tài khoản" : "Đăng nhập";
            switchPrompt.textContent = registering ? "Đã có tài khoản?" : "Chưa có tài khoản?"; toggle.textContent = registering ? "Đăng nhập" : "Đăng ký";
            if (registering) passwordInput.setAttribute("autocomplete", "new-password");
            else passwordInput.setAttribute("autocomplete", "current-password");
            visible = false; updateVisibility();
        }
        form.append(username, password, error, submit);
        form.onsubmit = async event => {
            event.preventDefault(); if (busy) return; busy = true;
            form.setAttribute("aria-busy", "true"); toggle.disabled = true; usernameInput.readOnly = true; passwordInput.readOnly = true;
            spinner.hidden = false; submitLabel.textContent = registering ? "Đang tạo tài khoản…" : "Đang đăng nhập…";
            try {
                await perform(submit, error, async () => {
                    const data = formData(form);
                    if (registering) {await api.request("auth/register/", "POST", data); notify("Đã tạo tài khoản. Bạn có thể đăng nhập."); registering = false; updateMode(); return;}
                    const result = await api.request("auth/login/", "POST", data); state.token = result.token; state.user = result.user; sessionStorage.setItem("emergency-token", state.token);
                    state.categories = await api.all("incident-categories/?page_size=100"); go(E.homes[state.user.role]); connectRealtime();
                });
            } finally {
                busy = false; form.setAttribute("aria-busy", "false"); toggle.disabled = false; usernameInput.readOnly = false; passwordInput.readOnly = false;
                spinner.hidden = true; submitLabel.textContent = registering ? "Tạo tài khoản" : "Đăng nhập";
            }
        };
        const mobileBrand = node("div", {class: "auth-mobile-brand"}, brand("KẾT NỐI · ĐIỀU PHỐI"));
        app.replaceChildren(node("div", {class: "login-layout auth-layout"},
            node("section", {class: "login-story"}, brand("KẾT NỐI · ĐIỀU PHỐI"), node("div", {class: "auth-story-content"}, node("p", {class: "eyebrow"}, "Hệ thống quản lý sự cố"), node("h1", {}, "Đúng thông tin.", node("br"), "Kịp thời ứng phó."), node("p", {class: "auth-story-description"}, "Kết nối người dân, điều phối viên và đội ứng cứu trong một quy trình thống nhất."))),
            node("main", {class: "login-form", id: "content", "aria-labelledby": "login-title"}, node("div", {class: "login-inner"}, mobileBrand, title, subtitle, form, node("p", {class: "auth-switch"}, switchPrompt, toggle)))));
    }
    function pagination(container, data, path, setPage, reload) {
        container.replaceChildren(); if (!data.count) return;
        container.append(node("span", {}, `${data.count} kết quả`));
        const controls = node("div", {class: "actions"});
        if (data.previous) controls.append(button("← Trước", () => { setPage(data.previous); reload(); }));
        if (data.next) controls.append(button("Tiếp →", () => { setPage(data.next); reload(); }));
        container.append(controls);
    }
    function makeFilters(kind) {
        const form = node("form", {class: "filters"});
        const statuses = kind === "report" ? ["pending", "accepted", "rejected"] : kind === "assignment" ? ["assigned", "accepted", "en_route", "on_scene", "responding", "completed", "cancelled", "rejected"] : ["new", "verified", "dispatched", "in_progress", "resolved", "cancelled"];
        form.append(select("Trạng thái", "status", statuses.map(status => [status, labels[status]])));
        if (kind !== "assignment") form.append(select("Loại sự cố", "category", categoryChoices()), field("Tìm kiếm", "search"), field("Từ thời điểm", "created_after", "datetime-local"), field("Đến thời điểm", "created_before", "datetime-local"));
        form.append(node("div", {class: "actions"}, node("button", {type: "submit"}, "Áp dụng bộ lọc"), button("Xóa bộ lọc", () => { form.reset(); form.dispatchEvent(new Event("submit", {cancelable: true})); })));
        return form;
    }
    function query(form) {
        const data = formData(form), params = new URLSearchParams();
        for (const [key, value] of Object.entries(data)) if (value) params.set(key, key.startsWith("created_") ? new Date(value).toISOString() : value);
        return params.toString();
    }
    function cameraPicker(position = () => null) {
        const generation = state.generation, error = errorBox(), box = node("div", {class: "camera-picker"}), video = node("video", {autoplay: true, muted: true, playsInline: true, hidden: true}), preview = node("img", {alt: "Ảnh hiện trường vừa chụp", hidden: true}), clip = node("video", {controls: true, playsInline: true, hidden: true, "aria-label": "Video hiện trường vừa quay"}), status = node("p", {class: "context-note", role: "status"});
        const camera = E.camera({devices: navigator.mediaDevices, canvas: () => document.createElement("canvas"), now: Date.now, makeFile: (blob, name) => new File([blob], name, {type: blob.type})});
        let pending = null, selected = null, previewURL = null, captureRevision = 0;
        function release() {if (previewURL) URL.revokeObjectURL(previewURL); previewURL = null; preview.removeAttribute("src"); clip.removeAttribute("src"); clip.load?.();}
        function reset() {captureRevision++; camera.stop(); release(); pending = null; selected = null; video.hidden = true; preview.hidden = true; clip.hidden = true; recordStop.hidden = true; recordOpen.hidden = !window.MediaRecorder; capture.hidden = true; use.hidden = true; retake.hidden = true; close.hidden = true; open.hidden = false; status.textContent = "";}
        const open = button("CHỤP ẢNH", event => perform(event.currentTarget, error, async () => {
            selected = null; pending = null; release(); preview.hidden = true; clip.hidden = true; recordOpen.hidden = true; use.hidden = true; retake.hidden = true;
            try {if (await camera.start(video) && generation === state.generation) {video.hidden = false; capture.hidden = false; close.hidden = false; open.hidden = true; status.textContent = "";}}
            catch (_) {camera.stop(); recordOpen.hidden = !window.MediaRecorder; throw new Error("Không thể mở camera. Bạn vẫn có thể gửi báo cáo không kèm ảnh.");}
        }), false);
        const capture = button("Chụp ảnh", event => perform(event.currentTarget, error, async () => {
            const version = captureRevision, captured = await camera.capture(video, position());
            if (!captured || generation !== state.generation || version !== captureRevision) return;
            pending = captured;
            release(); previewURL = URL.createObjectURL(pending.file); preview.src = previewURL; preview.hidden = false; video.hidden = true; capture.hidden = true; use.hidden = false; retake.hidden = false;
            status.textContent = "Xem trước";
        }), false);
        const retake = button("Chụp / quay lại", () => {const isVideo = (pending || selected)?.file.type.startsWith("video/"); reset(); (isVideo ? recordOpen : open).click();});
        const use = button("Sử dụng", () => {selected = pending; pending = null; use.hidden = true; status.textContent = "✓ Đã chọn media";}, false);
        const close = button("Bỏ media", reset); close.hidden = true;
        const recordOpen = button("QUAY VIDEO", event => perform(event.currentTarget, error, async () => {
            reset(); recordOpen.hidden = true; open.hidden = true;
            try {
            if (!await camera.start(video) || generation !== state.generation) return;
            video.hidden = false; recordStop.hidden = false; close.hidden = false; status.textContent = "Đang quay…";
            const version = captureRevision, recorded = await camera.record(video, position(), {Recorder: window.MediaRecorder, timers: window, maxBytes: config.mediaMaxBytes, maxSeconds: config.cameraVideoMaxSeconds});
            if (!recorded || generation !== state.generation || version !== captureRevision) return;
            pending = recorded;
            release(); previewURL = URL.createObjectURL(pending.file); clip.src = previewURL; clip.hidden = false; video.hidden = true; recordStop.hidden = true; use.hidden = false; retake.hidden = false; status.textContent = "Xem trước";
            } catch (_) {reset(); throw new Error("Không quay được video. Hãy thử chụp ảnh.");}
        }));
        recordOpen.hidden = !window.MediaRecorder;
        const recordStop = button("Dừng quay", () => camera.finishRecording(), false); recordStop.hidden = true;
        capture.hidden = true; use.hidden = true; retake.hidden = true;
        box.append(open, recordOpen, video, preview, clip, node("div", {class: "actions"}, capture, recordStop, retake, use, close), status, error);
        state.cleanups.push(() => {camera.stop(); release();});
        return {element: box, selected: () => selected, reset};
    }
    function missionTimeline(timeline = {}) {
        const names = {report_created_at: "Người dân gửi báo cáo", verified_at: "Điều phối viên xác minh", dispatched_at: "Điều đội", accepted_at: "Đội nhận nhiệm vụ", en_route_at: "Bắt đầu di chuyển", arrived_at: "Đã đến hiện trường", responding_at: "Bắt đầu xử lý", completed_at: "Hoàn thành"};
        return node("ol", {class: "timeline"}, ...Object.entries(names).filter(([key]) => timeline[key]).map(([key, text]) => node("li", {class: timeline[key] ? "done" : ""}, node("strong", {}, text), node("small", {}, timeline[key] ? date(timeline[key]) : "Chưa ghi nhận"))));
    }
    async function sourceReport(id) {
        const generation = state.generation, dialog = node("dialog", {class: "source-report"}), body = node("div"), error = errorBox();
        body.setAttribute("aria-busy", "true"); const loading = node("p", {role: "status"}, "Đang tải…"); body.append(loading);
        dialog.append(node("h2", {}, "Báo cáo gốc"), body, error, button("Đóng", () => dialog.close())); document.body.append(dialog); dialog.showModal();
        const guard = state.mediaGuard;
        dialog.addEventListener("close", () => {dialog.remove(); state.mediaGuard = guard;}, {once: true});
        state.cleanups.push(() => {dialog.close(); dialog.remove();});
        try {const report = await api.request(`incident-reports/${id}/`); if (generation !== state.generation || !dialog.isConnected) return;
            body.append(details([["Loại", categoryName(report.category)], ["Địa chỉ", report.address || "Chưa có"], ["Đã gửi", date(report.reported_at)], ["Vị trí", "Trên bản đồ hiện trường"]]), node("p", {class: "detail-description"}, report.description));
            await mediaSection(body, {report_id: Number(id)}, false, generation, false);
        } catch (failure) {body.replaceChildren(); error.textContent = E.publicError(failure);}
        finally {loading.remove(); body.setAttribute("aria-busy", "false");}
    }
    function callLink(contact, title = "Gọi người báo tin") {
        const link = node("a", {href: `tel:${E.normalizePhone(contact.reporter_phone)}`, class: "call-button", "aria-label": `${title}: ${contact.reporter_name || "người báo tin"}`}, icon("user"), title);
        if (state.user.role === "rescue_team") state.contactNodes.add(link);
        return link;
    }
    function ReporterContactCard(contact, map) {
        const actions = node("div", {class: "actions"}), card = node("article", {class: "reporter-contact"}, icon("user"), node("div", {}, node("small", {}, "Người báo tin"), node("strong", {}, contact.reporter_name || "Chưa cung cấp tên"), node("p", {class: "contact-phone"}, contact.reporter_phone || "Chưa cung cấp số"), !contact.allow_contact ? node("small", {}, "Không cho phép gọi trực tiếp") : null, actions));
        if (contact.can_call && !contact.phone_masked && contact.allow_contact) actions.append(callLink(contact));
        actions.append(button("Xem vị trí", () => {map?.update("report", {id: contact.report_id, latitude: contact.latitude, longitude: contact.longitude}, "Vị trí hiện trường"); map?.focus(contact.latitude, contact.longitude);}), button("Báo cáo gốc", () => sourceReport(contact.report_id)));
        if (state.user.role === "rescue_team") {for (const old of state.contactNodes) if (!old.isConnected) state.contactNodes.delete(old); state.contactNodes.add(card);}
        return card;
    }
    async function contactCards(endpoint, map, generation) {
        const data = await api.request(endpoint); if (generation !== state.generation) return [];
        const contacts = Array.isArray(data) ? data : [data], cards = contacts.map(contact => ReporterContactCard(contact, map));
        if (state.user.role === "admin") {
            const box = node("div"), purpose = field("Mục đích quản trị để xem liên hệ đầy đủ", "purpose", "text", {maxLength: 200, required: true}), error = errorBox();
            box.append(purpose, error, button("Xem liên hệ cho quản trị", event => perform(event.currentTarget, error, async () => {
                const reason = purpose.querySelector("input").value.trim(); if (!reason) throw new Error("Nhập mục đích xem thông tin liên hệ.");
                const revealed = await api.request(endpoint + (endpoint.includes("?") ? "&" : "?") + "purpose=" + encodeURIComponent(reason));
                if (generation === state.generation) box.replaceChildren(...(Array.isArray(revealed) ? revealed : [revealed]).map(contact => ReporterContactCard(contact, map)));
            })));
            cards.push(box);
        }
        return cards;
    }
    async function mediaSection(container, target, canWrite, generation, subscribe = true) {
        const box = panel("Ảnh & video hiện trường"), list = node("div"), error = errorBox(), files = field("Thêm ảnh hoặc video", "media", "file", {multiple: true, accept: "image/jpeg,image/png,image/webp,video/mp4,video/webm,audio/mpeg,audio/wav"});
        const writable = () => typeof canWrite === "function" ? canWrite() : canWrite;
        const uploadArea = node("div"); let deleteControls = [];
        state.mediaGuard = () => {uploadArea.hidden = !writable(); deleteControls.forEach(control => {control.hidden = !writable();});};
        container.append(box); box.append(list, error);
        async function load() { const data = await api.all("media/?" + new URLSearchParams(target)); if (generation !== state.generation) return;
            deleteControls = [];
            records(list, data, item => { const actions = node("div", {class: "actions"}), preview = node("div", {class: "media-preview"});
                if (item.status === "ready") actions.append(button("Tải xuống", controlEvent => perform(controlEvent.currentTarget, error, async () => { const download = await api.request(`media/${item.id}/download/`); const link = node("a", {href: download.url, rel: "noreferrer", target: "_blank"}); document.body.append(link); link.click(); link.remove(); })));
                if (item.status === "ready" && /^(image|video|audio)\//.test(item.content_type)) actions.append(button("Xem", event => perform(event.currentTarget, error, async () => {
                    const download = await api.request(`media/${item.id}/download/`); if (generation !== state.generation) return;
                    const kind = item.content_type.startsWith("image/") ? "img" : item.content_type.startsWith("video/") ? "video" : "audio";
                    const media = node(kind, {src: download.url, ...(kind === "img" ? {alt: "Ảnh hiện trường", loading: "lazy"} : {controls: true, preload: "metadata"}), onerror: () => {preview.replaceChildren(node("p", {class: "error"}, "Chưa xem được tệp. Vui lòng thử lại."));}});
                    preview.replaceChildren(media, button("Đóng", () => preview.replaceChildren()));
                })));
                if (canWrite && (["dispatcher", "admin"].includes(state.user.role) || item.uploaded_by === state.user.id)) { const remove = button("Xóa", event => perform(event.currentTarget, error, async () => { if (!await confirm("Xóa tệp hiện trường này?")) return; await api.request(`media/${item.id}/`, "DELETE"); notify("Đã yêu cầu xóa tệp."); await load(); })); deleteControls.push(remove); actions.append(remove); }
                return node("div", {class: "file-row"}, node("div", {}, node("strong", {}, item.content_type?.startsWith("video/") ? "Video hiện trường" : "Ảnh hiện trường"), item.status !== "ready" ? badge(item.status) : null), actions, preview);
            }, "Chưa có ảnh hoặc video.");
            state.mediaGuard();
        }
        if (canWrite) { const citizenCamera = state.user.role === "citizen" ? cameraPicker() : null;
            const upload = button("Gửi tệp", event => perform(event.currentTarget, error, async () => {
            const captured = citizenCamera?.selected(), selected = citizenCamera ? captured ? [captured.file] : [] : [...files.querySelector("input").files]; if (!selected.length) throw new Error("Chụp và sử dụng ảnh, hoặc chọn file được phép.");
            for (const file of selected) { await E.upload(api, target, file, {maxBytes: config.mediaMaxBytes, crypto, fetcher: fetch, metadata: captured?.metadata || {}}); notify("Đã gửi tệp hiện trường."); }
            if (citizenCamera) citizenCamera.reset(); else files.querySelector("input").value = ""; await load();
        }), false); uploadArea.append(citizenCamera ? citizenCamera.element : files, upload, node("p", {class: "muted", style: "margin-top:12px"}, `Tệp tối đa ${Math.round(config.mediaMaxBytes / 1048576)} MB.`)); box.append(uploadArea); }
        state.mediaGuard();
        const previousRefresh = state.refresh;
        if (subscribe && previousRefresh) state.refresh = async () => {await previousRefresh(); if (generation === state.generation && box.isConnected) await load();};
        try { await load(); } catch (failure) { if (failure.status === 0) {state.socket?.pause(); updateConnection("Mất kết nối", "offline");} error.textContent = E.publicError(failure); }
    }
    function citizenCreate(content) {
        const generation = state.generation, error = errorBox(), form = node("form", {class: "report-form"}), submit = node("button", {type: "submit", class: "report-submit"}, "GỬI BÁO CÁO");
        let devicePosition = null, current = null, candidate = null, editing = false, revision = 0, submitting = false;
        let draftId = null, draftKey = null, lastIntent = null, lastCapture = null;
        const geocoder = E.geocoding({api, timers: window}); state.cleanups.push(() => geocoder.destroy());
        const draftStatus = node("p", {class: "report-status", role: "status"});
        const media = cameraPicker(() => devicePosition), locationStatus = node("p", {class: "location-state", role: "status"}, "Đang xác định vị trí…"), address = node("p", {class: "site-address"}, "Chưa chọn vị trí"), warning = node("p", {class: "location-warning", role: "status", hidden: true});
        content.append(node("div", {class: "page-heading"}, node("h1", {}, "Báo cáo sự cố"))); content.className += " citizen-create";
        const categoryLabels = {fire: "Cháy nổ", traffic: "Tai nạn", flood: "Ngập lụt", medical: "Y tế", other: "Khác"};
        const categoryOrder = Object.keys(categoryLabels);
        const categories = [...state.categories].sort((a, b) => (categoryOrder.indexOf(a.code) < 0 ? categoryOrder.length : categoryOrder.indexOf(a.code)) - (categoryOrder.indexOf(b.code) < 0 ? categoryOrder.length : categoryOrder.indexOf(b.code)));
        const categoryTiles = node("fieldset", {class: "category-tiles"}, node("legend", {}, "Chuyện gì đang xảy ra?"), ...categories.map(category => node("label", {class: "category-tile"}, node("input", {type: "radio", name: "category", value: category.id, required: true, "aria-label": categoryLabels[category.code] || category.name}), icon(categoryLabels[category.code] ? category.code : "other"), node("span", {}, categoryLabels[category.code] || category.name))));
        const locationPanel = node("section", {class: "report-section site-section"}, node("div", {class: "site-heading"}, node("h2", {}, "Vị trí hiện trường")), locationStatus, address);
        const searchInput = field("Tìm địa chỉ hoặc địa điểm", "", "search", {maxLength: 200, placeholder: "Đường, xã/phường, tỉnh/thành…", autoComplete: "off"}), searchStatus = node("p", {role: "status", class: "search-status"}), results = node("div", {class: "address-results", "aria-label": "Kết quả tìm địa chỉ"});
        const searchControl = searchInput.querySelector("input");
        const searchButton = button("Tìm", searchAddress);
        const editTools = node("div", {class: "location-editor", hidden: true}, node("div", {class: "address-search"}, searchInput, searchButton), searchStatus, results);
        const editButton = button("Chỉnh vị trí", () => beginEdit()); editButton.className = "secondary edit-location";
        const locateButton = button("Dùng vị trí hiện tại", () => locateDevice(true));
        const confirmLocation = button("XÁC NHẬN VỊ TRÍ", () => {if (candidate) {current = {...candidate}; finishEdit(); if (!current.address) resolveAddress(current);}}, false);
        const cancelLocation = button("Hủy", () => finishEdit());
        const editActions = node("div", {class: "location-edit-actions", hidden: true}, cancelLocation, confirmLocation);
        locationPanel.append(editButton, editTools);
        const description = field("Mô tả", "description", "textarea", {required: true, maxLength: 10000, placeholder: "Mô tả ngắn tình hình hiện trường…"});
        const fullName = [state.user.first_name, state.user.last_name].filter(Boolean).join(" ") || state.user.username;
        const contact = node("section", {class: "report-section"}, node("h2", {}, "Thông tin liên hệ"), node("div", {class: "grid contact-fields"}, field("Họ và tên", "reporter_name", "text", {required: true, maxLength: 120, autoComplete: "name", value: fullName}), field("Số điện thoại", "reporter_phone", "tel", {required: true, maxLength: 30, autoComplete: "tel", inputMode: "tel", value: state.user.phone || "", placeholder: "09xx xxx xxx"})), node("small", {}, "Dùng để liên hệ khi cần xác minh."));
        form.append(node("section", {class: "report-section"}, categoryTiles), locationPanel, node("section", {class: "report-section"}, description, disclosure("Sự cố xảy ra trước đó", field("Thời điểm", "occurred_at", "datetime-local"))), node("section", {class: "report-section"}, node("h2", {}, "Ảnh / video hiện trường"), media.element), contact, draftStatus, error, node("div", {class: "submit-bar"}, submit)); content.append(form);
        const map = mountMap(locationPanel, true);
        locationPanel.append(warning, editActions);
        editTools.append(locateButton, node("small", {class: "geocoder-credit"}, "Địa chỉ: ", node("a", {href: "https://www.openstreetmap.org/copyright", target: "_blank", rel: "noreferrer"}, "© OpenStreetMap")));
        function showPoint(point = editing ? candidate : current, focus = false) {
            if (point) {map.update("report", {id: "draft", ...point}, "Vị trí hiện trường"); if (focus) map.focus(point.latitude, point.longitude);}
            else map.replace("report", [], () => "");
            address.textContent = point?.address || (point ? "Đã chọn vị trí trên bản đồ" : "Chưa chọn vị trí");
            warning.hidden = !point || !devicePosition || E.distance(devicePosition, point) <= config.incidentLocationDistanceWarningMeters;
            warning.textContent = warning.hidden ? "" : "Vị trí sự cố cách khá xa vị trí hiện tại của bạn. Vui lòng kiểm tra lại.";
            confirmLocation.disabled = !point;
        }
        async function resolveAddress(point) {
            const version = revision;
            try {
                const result = await geocoder.reverse(point);
                if (generation !== state.generation || version !== revision || ![current, candidate].includes(point)) return;
                point.address = result.address || point.address || ""; showPoint();
            } catch (failure) {
                if (generation === state.generation && version === revision && failure.name !== "AbortError") showPoint();
            }
        }
        function selectPoint(latitude, longitude, selectedAddress = "", focus = false) {
            if (!editing || submitting || !E.validPoint({latitude, longitude})) return;
            revision++; candidate = {latitude, longitude, address: selectedAddress, source: "manual"};
            locationStatus.textContent = "Đã chọn vị trí trên bản đồ"; showPoint(candidate, focus); resolveAddress(candidate);
        }
        function beginEdit() {
            if (submitting) return;
            revision++; geocoder.cancelReverse(); editing = true; candidate = current ? {...current} : null;
            editTools.hidden = false; editActions.hidden = false; editButton.hidden = true;
            map.pick?.(selectPoint); map.edit?.("report", "draft", selectPoint); showPoint(); searchControl.focus?.();
        }
        function finishEdit() {
            revision++; geocoder.cancelReverse(); geocoder.cancelSearch(); editing = false; candidate = null;
            editTools.hidden = true; editActions.hidden = true; editButton.hidden = false; results.replaceChildren(); searchStatus.textContent = "";
            map.pick?.(null); map.edit?.("report", "draft", null);
            locationStatus.textContent = current ? "Đã xác định vị trí" : "Không thể lấy vị trí hiện tại. Hãy chọn vị trí sự cố trên bản đồ.";
            showPoint();
        }
        async function searchAddress() {
            if (!editing || submitting) return;
            const query = searchControl.value.trim(); if (query.length < 3) {searchStatus.textContent = "Nhập ít nhất 3 ký tự."; return;}
            results.replaceChildren(); searchStatus.textContent = "Đang tìm địa chỉ…";
            try {
                const found = await geocoder.search(query);
                if (generation !== state.generation || !editing || searchControl.value.trim() !== query) return;
                searchStatus.textContent = found.length ? "" : "Không tìm thấy địa chỉ. Hãy chọn trên bản đồ.";
                results.replaceChildren(...found.filter(E.validPoint).map(place => button(place.address || "Chọn địa điểm này", () => {geocoder.cancelSearch(); selectPoint(place.latitude, place.longitude, place.address, true); results.replaceChildren();})));
            } catch (failure) {if (failure.name !== "AbortError" && generation === state.generation && editing && searchControl.value.trim() === query) searchStatus.textContent = "Không tìm được địa chỉ. Hãy chọn trên bản đồ.";}
        }
        searchControl.addEventListener("input", () => {geocoder.cancelSearch(); results.replaceChildren(); searchStatus.textContent = "";});
        searchControl.addEventListener("keydown", event => {if (event.key === "Enter") {event.preventDefault(); searchAddress();}});
        async function locateDevice(explicit = false) {
            const requestedRevision = revision;
            locationStatus.textContent = "Đang xác định vị trí…";
            try {
                if (!navigator.geolocation) throw new Error("unavailable");
                const position = await new Promise((resolve, reject) => navigator.geolocation.getCurrentPosition(resolve, reject, {enableHighAccuracy: true, timeout: 15000, maximumAge: 0}));
                if (generation !== state.generation) return;
                const coords = {latitude: position.coords.latitude, longitude: position.coords.longitude}; if (!E.validPoint(coords)) throw new Error("invalid");
                devicePosition = {...coords, accuracy: position.coords.accuracy, timestamp: new Date(position.timestamp).toISOString()};
                if (requestedRevision !== revision) {showPoint(); return;}
                if (editing && explicit) {selectPoint(coords.latitude, coords.longitude, "", true); return;}
                if (editing || current) return;
                current = {...coords, address: "", source: "gps"};
                locationStatus.textContent = position.coords.accuracy > 100 ? "Vị trí chưa chính xác" : "✓ Đã xác định vị trí";
                showPoint(current, true); resolveAddress(current);
            } catch (_) {
                if (generation === state.generation && requestedRevision === revision) {locationStatus.textContent = "Không thể lấy vị trí hiện tại. Hãy chọn vị trí sự cố trên bản đồ."; if (!current && !editing) beginEdit();}
            }
        }
        locateDevice();
        form.onsubmit = event => { event.preventDefault(); if (submitting) return; submitting = true; submit.textContent = "Đang gửi…"; perform(submit, error, async () => {
            if (editing) throw new Error("Xác nhận vị trí đã chỉnh trước khi gửi.");
            if (!E.validPoint(current)) throw new Error("Chọn vị trí sự cố trên bản đồ.");
            const captured = media.selected(), selected = captured ? [captured.file] : []; if (selected.some(file => file.size > config.mediaMaxBytes || !file.size)) throw new Error("Ảnh rỗng hoặc vượt giới hạn kích thước.");
            const values = formData(form); delete values.media; if (!values.occurred_at) delete values.occurred_at; else values.occurred_at = new Date(values.occurred_at).toISOString();
            values.category = Number(values.category); values.latitude = current.latitude; values.longitude = current.longitude; values.address = current.address || "";
            values.reporter_phone = E.normalizePhone(values.reporter_phone); values.allow_contact = true;
            values.gps_location = devicePosition ? {latitude: devicePosition.latitude, longitude: devicePosition.longitude} : null;
            values.location_accuracy = devicePosition?.accuracy ?? null;
            if (!values.reporter_phone || !values.reporter_name.trim()) throw new Error("Nhập họ tên và số điện thoại hợp lệ.");
            let report;
            if (captured || draftId) {
                draftKey ||= crypto.randomUUID();
                report = await api.request("incident-reports/drafts/", "POST", {...values, request_id: draftKey}); draftId = report.id;
                if (generation !== state.generation) return;
                if (report.is_draft) {
                    draftStatus.textContent = "Đang gửi ảnh hiện trường…";
                    draftStatus.append(node("a", {href: `#/citizen/reports/${report.id}`}, " Mở bản nháp"));
                    if (lastIntent && lastCapture !== captured?.file) {await api.request(`media/${lastIntent}/`, "DELETE"); lastIntent = null;}
                    if (captured) {
                        let confirmed = false;
                        if (lastIntent) {
                            try {await api.request(`media/${lastIntent}/confirm/`, "POST", {}); confirmed = true;}
                            catch (failure) {if (failure.status !== 409) throw failure; await api.request(`media/${lastIntent}/`, "DELETE"); lastIntent = null;}
                        }
                        if (!confirmed) {
                            lastCapture = captured.file;
                            try {await E.upload(api, {report_id: report.id}, captured.file, {maxBytes: config.mediaMaxBytes, crypto, fetcher: fetch, metadata: captured.metadata, intent: id => {lastIntent = id;}});}
                            catch (failure) {draftStatus.textContent = "Chưa gửi được ảnh. Thử lại hoặc bỏ ảnh để gửi báo cáo."; throw failure;}
                        }
                    }
                    if (generation !== state.generation) return;
                    // A lost presign response can leave an intent whose ID was never
                    // received. Abandon those unfinished own intents before publishing.
                    const assets = await api.all(`media/?report_id=${report.id}`);
                    if (generation !== state.generation) return;
                    for (const asset of assets) if (asset.status === "pending" && asset.id !== lastIntent) await api.request(`media/${asset.id}/`, "DELETE");
                    report = await api.request(`incident-reports/${report.id}/submit/`, "POST", {});
                }
            } else report = await api.request("incident-reports/", "POST", values);
            if (generation !== state.generation) return;
            notify("Đã tiếp nhận báo cáo.");
            go(`/citizen/reports/${report.id}`);
        }).finally(() => {submitting = false; submit.textContent = "GỬI BÁO CÁO";}); };
    }
    async function reportDetail(content, id, manager) {
        const generation = state.generation, error = errorBox(), main = panel("Thông tin báo cáo"), body = node("div"), tools = node("div", {class: "actions"}), duplicatePanel = panel("Báo cáo có thể trùng"), duplicates = node("div");
        error.id = "page-error"; content.append(heading("Chi tiết báo cáo", "Theo dõi xác minh, vị trí và bằng chứng hiện trường."), error, main); main.append(body, tools);
        const map = mountMap(main, true); let report;
        if (manager) { duplicatePanel.append(node("p", {class: "muted"}, "Kiểm tra trước khi liên kết báo cáo."), duplicates); content.append(duplicatePanel); }
        async function act(path, data, text) { await api.request(path, "POST", data); notify(text); await refresh(); }
        state.refresh = async () => {
            const current = await api.request(`incident-reports/${id}/`); if (generation !== state.generation) return; report = current;
            body.replaceChildren(...(!manager && !report.is_draft ? [node("div", {class: "success-box"}, icon("check"), node("div", {}, node("strong", {}, "Đã tiếp nhận báo cáo"), node("p", {}, "Bạn có thể theo dõi tiến độ bên dưới.")))] : []), details([["Trạng thái", badge(report.is_draft ? "draft" : report.review_status)], ["Loại sự cố", categoryName(report.category)], ["Đã gửi", date(report.reported_at)], ["Thời điểm xảy ra", date(report.occurred_at)], ["Địa chỉ", report.address || "—"], ...(report.incident ? [["Xử lý hiện trường", labels[report.incident_status] || "Đang xử lý"]] : []), ...(report.review_note ? [["Ghi chú xác minh", report.review_note]] : [])]), node("p", {class: "detail-description"}, report.description));
            if (manager) {const cards = await contactCards(`incident-reports/${id}/contact/`, map, generation); if (generation !== state.generation) return; body.append(...cards);}
            if (!manager && !report.is_draft) body.append(node("ol", {class: "timeline report-progress"}, node("li", {class: "done"}, node("strong", {}, "Đã tiếp nhận báo cáo"), node("small", {}, date(report.reported_at))), node("li", {class: report.review_status === "accepted" ? "done" : ""}, node("strong", {}, report.review_status === "rejected" ? "Báo cáo đã bị từ chối" : report.review_status === "accepted" ? "Đã xác minh" : "Đang chờ xác minh")), node("li", {class: report.incident_status === "resolved" ? "done" : ""}, node("strong", {}, report.incident ? labels[report.incident_status] || "Đã liên kết sự cố" : "Chờ liên kết và điều phối"))));
            map.replace("report", [report], () => "Chi tiết báo cáo"); tools.replaceChildren();
            if (!manager && report.is_draft) tools.append(button("Gửi bản nháp", event => perform(event.currentTarget, error, async () => {await api.request(`incident-reports/${id}/submit/`, "POST", {}); notify("Đã gửi báo cáo cho điều phối viên."); await refresh();}), false), node("p", {class: "context-note"}, "Hoàn tất hoặc xóa tệp đang gửi trước khi gửi báo cáo."));
            if (manager && report.review_status === "pending") for (const status of ["accepted", "rejected"]) tools.append(button(status === "accepted" ? "Xác minh / chấp nhận" : "Từ chối báo cáo", event => perform(event.currentTarget, error, async () => { if (await confirm(`${labels[status]} báo cáo này?`)) await act(`incident-reports/${id}/verify/`, {review_status: status}, "Đã cập nhật xác minh."); }), status !== "accepted"));
            if (manager && report.incident) tools.append(button("Xem sự cố", () => go(`/dispatcher/incidents/${report.incident}`)));
            if (manager && report.review_status === "accepted" && !report.incident) {
                const title = field("Tên sự cố", "title", "text", {required: true, maxLength: 200, value: categoryName(report.category)});
                const create = button("Tạo sự cố", event => perform(event.currentTarget, error, async () => { const name = title.querySelector("input").value.trim(); if (!name) throw new Error("Nhập tên sự cố."); if (!await confirm("Tạo sự cố mới từ báo cáo này?")) return; const incident = await api.request(`incident-reports/${id}/create-incident/`, "POST", {title: name}); notify("Đã tạo sự cố."); go(`/dispatcher/incidents/${incident.id}`); }), false);
                tools.append(title, create);
            }
            if (!manager) return;
            const hints = await api.request(`incident-reports/${id}/potential-duplicates/`); if (generation !== state.generation) return;
            duplicates.replaceChildren();
            if (!hints.reports.length && !hints.incidents.length) duplicates.append(node("p", {class: "empty"}, "Không có báo cáo liên quan."));
            hints.incidents.forEach(incident => duplicates.append(record(incidentTitle(incident), incident.status, `${Math.round(incident.distance_meters)} m · ${categoryName(incident.category)}`, [button("Xem", () => go(`/dispatcher/incidents/${incident.id}`)), ...(report.review_status === "accepted" && !report.incident ? [button("Liên kết vào sự cố này", event => perform(event.currentTarget, error, async () => { if (await confirm(`Liên kết báo cáo vào ${incidentTitle(incident)}?`)) { await act(`incidents/${incident.id}/reports/`, {report_ids: [Number(id)]}, "Đã liên kết báo cáo."); } }), false)] : []), button("Bỏ qua đề xuất", event => perform(event.currentTarget, error, () => act(`incident-reports/${id}/dismiss-duplicate/`, {incident_id: incident.id, reason: "Dispatcher xác nhận không cùng sự cố."}, "Đã bỏ qua đề xuất.")))])));
            hints.reports.forEach(candidate => duplicates.append(record(categoryName(candidate.category), candidate.review_status, `${Math.round(candidate.distance_meters)} m · ${candidate.description}`, [button("Xem báo cáo", () => go(`/dispatcher/reports/${candidate.id}`)), button("Bỏ qua đề xuất", event => perform(event.currentTarget, error, () => act(`incident-reports/${id}/dismiss-duplicate/`, {candidate_report_id: candidate.id, reason: "Dispatcher xác nhận không cùng sự cố."}, "Đã bỏ qua đề xuất.")))])));
            if (hints.more_reports || hints.more_incidents) duplicates.append(node("small", {}, "Còn đề xuất khác; mở danh sách báo cáo để kiểm tra thêm."));
        };
        await refresh(); if (generation !== state.generation || !report) return;
        await mediaSection(content, {report_id: Number(id)}, () => manager || (report.review_status === "pending" && !report.incident), generation);
    }
    async function reportList(content) {
        const generation = state.generation, filters = makeFilters("report"), list = node("div"), pages = node("div", {class: "pagination"}), error = errorBox(); error.id = "page-error"; let page;
        content.append(heading("Báo cáo của tôi", "Theo dõi tiến độ xử lý.", button("＋ Báo cáo mới", () => go("/citizen/report"), false)), panel("Tìm báo cáo", filters), error, panel("Lịch sử báo cáo", list, pages));
        state.refresh = async () => { const data = await api.request(page || "incident-reports/?" + query(filters)); if (generation !== state.generation) return;
            records(list, data.results, item => record(categoryName(item.category), item.is_draft ? "draft" : item.review_status, `${date(item.reported_at)} · ${item.description}`, [button("Xem chi tiết", () => go(`/citizen/reports/${item.id}`))])); pagination(pages, data, "", value => {page = value;}, refresh); error.textContent = "";
        }; filters.onsubmit = event => { event.preventDefault(); page = null; refresh(true); }; await refresh();
    }
    async function dispatcher(content, view, selected) {
        const generation = state.generation, isReport = view === "reports" || selected?.kind === "reports", kind = isReport ? "report" : "incident", filters = makeFilters(kind), list = node("div"), pages = node("div", {class: "pagination"}), error = errorBox(), metrics = node("div", {class: "metrics"}); error.id = selected ? "dashboard-error" : "page-error"; let page;
        content.className += " dispatch-content";
        const rail = node("aside", {class: "incident-rail", "aria-label": "Danh sách hiện trường"}, heading("Trung tâm điều phối", "Tiếp nhận và điều phối ứng cứu"), node("nav", {class: "tabs"}, node("a", {href: "#/dispatcher", class: !isReport ? "active" : ""}, "Sự cố"), node("a", {href: "#/dispatcher?view=reports", class: isReport ? "active" : ""}, "Báo cáo")), metrics, disclosure("Tìm kiếm & bộ lọc", filters), error, node("div", {class: "rail-title"}, node("h2", {}, isReport ? "Báo cáo tiếp nhận" : "Sự cố theo trạng thái"), signal("Đã cập nhật")), list, pages);
        const mapPanel = node("section", {class: "dispatch-map-panel", "aria-label": "Bản đồ vận hành"});
        const workspace = node("div", {class: "dispatch-workspace"}, rail, mapPanel); content.append(workspace); const map = mountMap(mapPanel);
        mapPanel.append(node("div", {class: "map-caption"}, icon("map"), node("div", {}, node("strong", {}, "Bản đồ tác chiến"), node("small", {}, "Chọn sự cố để xử lý"))), node("div", {class: "map-legend"}, node("span", {}, "■ Sự cố"), node("span", {}, "● Báo cáo"), node("span", {}, "✚ Đội ứng cứu"), node("small", {}, "Theo bộ lọc đang chọn")));
        const dashboardRefresh = async () => {
            const endpoint = isReport ? "incident-reports/" : "incidents/";
            const [data, other, teams] = await Promise.all([api.request(page || endpoint + "?" + query(filters)), api.request(isReport ? "incidents/" : "incident-reports/"), api.all("teams/locations/?page_size=100")]);
            if (generation !== state.generation) return; error.textContent = "";
            metrics.replaceChildren(...[[isReport ? "Báo cáo" : "Sự cố", data.count], ["Đội trên bản đồ", teams.filter(team => team.latitude != null).length], ["Đội sẵn sàng", teams.filter(team => team.status === "available").length]].map(([name, value]) => node("div", {class: "metric"}, node("strong", {}, value), node("small", {}, name))));
            const incidents = isReport ? other.results : data.results, reports = isReport ? data.results : other.results;
            map.replace("incident", incidents, item => incidentTitle(item), item => go(`/dispatcher/incidents/${item.id}`));
            map.replace("report", reports, item => categoryName(item.category), item => go(`/dispatcher/reports/${item.id}`));
            map.replace("team", teams, item => item.name);
            records(list, data.results, item => {
                const card = record(isReport ? categoryName(item.category) : incidentTitle(item), isReport ? item.review_status : item.status, `${date(item.created_at || item.reported_at)} · ${isReport ? item.description : item.address || categoryName(item.category)}`, [button(isReport ? "Xác minh báo cáo" : "Xem & điều phối", () => go(`/dispatcher/${isReport ? "reports" : "incidents"}/${item.id}`))]);
                if (selected && selected.id === String(item.id)) card.className += " selected";
                return card;
            });
            pagination(pages, data, "", value => {page = value;}, refresh);
        };
        state.refresh = dashboardRefresh;
        filters.onsubmit = event => {event.preventDefault(); page = null; refresh(true);};
        // A slow map snapshot must not block the selected detail drawer.
        const initial = dashboardRefresh().catch(failure => {if (generation === state.generation) {state.refreshError = true; if (failure.name !== "ObsoleteSnapshot") setDisplayState(failure.status === 0 ? "offline" : "error", E.publicError(failure));}});
        if (selected) {
            const close = () => go(isReport ? "/dispatcher?view=reports" : "/dispatcher");
            const drawerBody = node("div", {class: "drawer-body"});
            const drawer = node("aside", {class: "incident-drawer", "aria-label": selected.kind === "reports" ? "Chi tiết báo cáo" : "Chi tiết sự cố"}, node("div", {class: "drawer-top"}, node("small", {}, "HIỆN TRƯỜNG / CHI TIẾT"), quickAction("close", "Đóng", close)), drawerBody); workspace.append(drawer);
            const drawerInset = () => map.inset?.(window.innerWidth < 768 ? Math.max(0, mapPanel.getBoundingClientRect().bottom - drawer.getBoundingClientRect().top) : 0, window.innerWidth >= 768 ? drawer.getBoundingClientRect().width : 0);
            drawerInset(); window.addEventListener("resize", drawerInset); state.cleanups.push(() => window.removeEventListener("resize", drawerInset));
            let focused = false;
            const proxy = {isOverview: true, replace(kind, items, title, click) {if (kind === "incident" || kind === "report") {items.forEach(item => map.update(kind, item, title(item), click ? () => click(item) : null)); if (!focused && items[0]?.latitude != null) {map.focus(items[0].latitude, items[0].longitude); focused = true;}}}, update: map.update, focus: map.focus};
            state.drawerMap = proxy;
            try {if (selected.kind === "reports") await reportDetail(drawerBody, selected.id, true); else await incidentDetail(drawerBody, selected.id);} finally {if (state.drawerMap === proxy) state.drawerMap = null;}
            if (generation !== state.generation) return;
            const detailRefresh = state.refresh;
            state.visibleTeamIds = null;
            state.refresh = async () => {await Promise.all([dashboardRefresh(), detailRefresh()]); if (generation === state.generation) state.visibleTeamIds = null;};
            const escape = event => {if (event.key === "Escape" && !document.getElementById("confirm-dialog").open) close();};
            window.addEventListener("keydown", escape); state.cleanups.push(() => window.removeEventListener("keydown", escape));
        }
        await initial;
    }
    async function incidentDetail(content, id) {
        const generation = state.generation, error = errorBox(), body = node("div"), statusActions = node("div", {class: "actions"}), main = panel("Thông tin sự cố", body, statusActions), teams = node("div"), assignments = node("div"), reports = node("div"), history = node("ol", {class: "timeline"}); error.id = "page-error"; let incident;
        content.append(heading("Chi tiết sự cố", "Xác minh, gom báo cáo và điều phối lực lượng."), error, main); const map = mountMap(main, true);
        const timelineArea = node("div"), signalsArea = node("div"); content.append(disclosure("Tiến độ & thời gian phản hồi", panel(null, timelineArea)), panel("Yêu cầu từ hiện trường", signalsArea));
        const candidates = node("div"), candidatePanel = panel("Báo cáo gần hiện trường", candidates), picked = new Set();
        const linkPicked = button("Liên kết báo cáo đã chọn", event => perform(event.currentTarget, error, async () => {const report_ids = [...picked]; if (!report_ids.length) throw new Error("Chọn ít nhất một báo cáo đã xác minh."); if (!await confirm(`Liên kết ${report_ids.length} báo cáo đã chọn vào sự cố này?`)) return; await api.request(`incidents/${id}/reports/`, "POST", {report_ids}); picked.clear(); notify("Đã liên kết các báo cáo đã chọn."); await refresh();}), false);
        candidatePanel.append(linkPicked);
        content.append(node("div", {class: "grid"}, panel("Đội được gợi ý", node("p", {class: "muted"}, "Ưu tiên đội sẵn sàng ở gần hiện trường."), teams), panel("Phân công hiện tại", assignments)), candidatePanel, panel("Báo cáo liên kết", reports), disclosure("Lịch sử xử lý", panel(null, history)));
        state.refresh = async () => {
            const [current, tasks, linked, audit, linkAudit, locations] = await Promise.all([api.request(`incidents/${id}/`), api.all(`assignments/?incident=${id}`), api.all(`incidents/${id}/reports/`), api.all(`incidents/${id}/status-history/`), api.all(`incidents/${id}/report-link-history/`), api.all("teams/locations/?page_size=100")]);
            if (generation !== state.generation) return; incident = current;
            body.replaceChildren(node("h2", {}, incidentTitle(incident)), details([["Trạng thái", badge(incident.status)], ["Loại sự cố", categoryName(incident.category)], ["Địa chỉ", incident.address || "—"]]), node("p", {class: "detail-description"}, incident.description)); map.replace("incident", [incident], () => incidentTitle(incident)); statusActions.replaceChildren();
            const [cards, lifecycle, fieldSignals] = await Promise.all([contactCards(`incidents/${id}/contacts/`, map, generation), api.request(`incidents/${id}/timeline/`), api.all(`incidents/${id}/signals/`)]); if (generation !== state.generation) return;
            body.append(...cards);
            const duration = value => value < 60 ? "Dưới 1 phút" : `${Math.round(value / 60)} phút`;
            timelineArea.replaceChildren(details([["Thời gian đội nhận nhiệm vụ", lifecycle.time_to_accept_seconds], ["Thời gian giải quyết", lifecycle.time_to_resolve_seconds]].filter(([, value]) => value != null).map(([name, value]) => [name, duration(value)])), missionTimeline(lifecycle.milestones || {}), ...((lifecycle.assignments || []).map(task => disclosure(locations.find(team => team.team_id === task.team_id)?.name || "Tiến độ đội ứng cứu", missionTimeline(task)))));
            const signalLabels = {missing_location: "Không tìm thấy vị trí", unreachable_contact: "Không liên hệ được người báo", blocked_road: "Đường bị chặn", extra_forces: "Cần thêm lực lượng", more_severe: "Nghiêm trọng hơn dự kiến", fire: "Cứu hỏa", medical: "Y tế", rescue: "Cứu hộ", other: "Lực lượng khác"};
            records(signalsArea, fieldSignals, item => record(item.kind === "support" ? "Yêu cầu thêm lực lượng" : "Báo vấn đề", "recorded", `${signalLabels[item.problem || item.support_type] || "Yêu cầu hỗ trợ"} · ${item.note || "Không có ghi chú"} · ${date(item.reported_at)}`, Number.isFinite(item.latitude) && Number.isFinite(item.longitude) ? [button("Xem vị trí yêu cầu", () => map.focus(item.latitude, item.longitude))] : []), "Chưa có yêu cầu hỗ trợ.");
            const statusSelect = select("Trạng thái cần cập nhật", "status", ["verified", "in_progress", "resolved", "cancelled"].map(status => [status, labels[status]]), "", "Chọn trạng thái");
            if (!["resolved", "cancelled"].includes(incident.status)) statusActions.append(statusSelect, button("Cập nhật trạng thái", event => perform(event.currentTarget, error, async () => { const status = statusSelect.querySelector("select").value, expected_status = incident.status; if (!status) throw new Error("Chọn trạng thái."); if (!await confirm(`Chuyển sự cố sang ${labels[status]}?`)) return; await api.request(`incidents/${id}/status/`, "PATCH", {status, expected_status}); notify("Đã cập nhật trạng thái."); await refresh(); }), false));
            records(reports, linked, report => record(categoryName(report.category), report.review_status, report.description, [button("Xem", () => go(`/dispatcher/reports/${report.id}`))]));
            history.replaceChildren(...audit.map(entry => node("li", {}, `${date(entry.changed_at)} · ${labels[entry.from_status] || "Khởi tạo"} → ${labels[entry.to_status] || "Đã cập nhật"}${noteSuffix(entry.note)}`)));
            history.append(...linkAudit.map(entry => node("li", {}, `${date(entry.created_at)} · ${({create: "Tạo sự cố từ báo cáo", link: "Liên kết báo cáo", merge: "Gom báo cáo"})[entry.operation] || "Cập nhật liên kết báo cáo"}${noteSuffix(entry.note)}`)));
            candidatePanel.hidden = ["resolved", "cancelled"].includes(incident.status) || !linked.length;
            if (!candidatePanel.hidden) {
                const hints = await api.request(`incident-reports/${linked[0].id}/potential-duplicates/`); if (generation !== state.generation) return;
                const eligible = new Set(hints.reports.filter(report => report.review_status === "accepted" && !report.incident).map(report => report.id));
                for (const reportId of picked) if (!eligible.has(reportId)) picked.delete(reportId);
                records(candidates, hints.reports, report => node("div", {class: "candidate-row"}, report.review_status === "accepted" && !report.incident ? node("label", {}, node("input", {type: "checkbox", checked: picked.has(report.id), onchange: event => {if (event.target.checked) picked.add(report.id); else picked.delete(report.id);}}), categoryName(report.category)) : node("strong", {}, categoryName(report.category)), badge(report.review_status), node("small", {}, `${Math.round(report.distance_meters)} m · ${report.description}`), button("Xem / xác minh", () => go(`/dispatcher/reports/${report.id}`))), "Chưa có báo cáo liên quan.");
                linkPicked.hidden = !eligible.size;
            }
            let suggestions = [];
            if (!["resolved", "cancelled", "new"].includes(incident.status)) { const result = await api.request(`incidents/${id}/suggested-teams/`); suggestions = result.teams; }
            if (generation !== state.generation) return;
            const relevantTeams = new Set([...tasks.map(task => task.team), ...suggestions.map(team => team.id)]);
            state.visibleTeamIds = map.isOverview ? null : relevantTeams;
            map.replace("team", locations.filter(team => relevantTeams.has(team.team_id)), team => team.name);
            records(teams, suggestions, team => record(team.name, team.status, `Cách hiện trường ${Math.round(team.distance_meters)} m`, [button("Phân công đội", event => perform(event.currentTarget, error, async () => { if (!await confirm(`Phân công ${team.name} vào sự cố này?`)) return; await api.request(`incidents/${id}/assignments/`, "POST", {team_id: team.id}); notify("Đã giao nhiệm vụ."); await refresh(); }), false)]), "Chưa có đội phù hợp ở gần hiện trường.");
            records(assignments, tasks, task => { const actions = []; if (["assigned", "accepted", "en_route", "on_scene", "responding"].includes(task.status)) {
                actions.push(button("Hủy phân công", event => perform(event.currentTarget, error, async () => { if (!await confirm("Hủy phân công này?")) return; await api.request(`assignments/${task.id}/cancel/`, "POST", {expected_status: task.status}); notify("Đã hủy phân công."); await refresh(); })));
                const replacement = select("Đội thay thế", "team_id", suggestions.map(team => [team.id, team.name]), "", "Chọn đội");
                actions.push(replacement, button("Đổi đội", event => perform(event.currentTarget, error, async () => { const teamId = Number(replacement.querySelector("select").value); if (!teamId) throw new Error("Chọn đội thay thế."); if (!await confirm("Thay đổi đội ứng cứu cho phân công này?")) return; await api.request(`assignments/${task.id}/reassign/`, "POST", {team_id: teamId, expected_status: task.status}); notify("Đã đổi đội và lưu lịch sử."); await refresh(); })));
            } actions.push(button("Lịch sử phân công", event => { const control = event.currentTarget; perform(control, error, async () => { const data = await api.all(`assignments/${task.id}/history/`); const list = node("ol", {class: "timeline"}, ...data.map(entry => node("li", {}, `${date(entry.created_at)} · ${labels[entry.to_status] || "Đã cập nhật"}`))); if (control.isConnected) { control.closest("article").append(list); control.remove(); } }); }));
                return record(suggestions.find(team => team.id === task.team)?.name || "Đội ứng cứu đã phân công", task.status, date(task.updated_at), actions);
            }); error.textContent = "";
        }; await refresh(); if (generation === state.generation && incident) await mediaSection(content, {incident_id: Number(id)}, () => !["resolved", "cancelled"].includes(incident.status), generation);
    }
    function rescueGPS(content) {
        const start = quickAction("locate", "Chia sẻ vị trí", () => {
            if (!state.user.response_team) {notify("Chưa được gán đội. Liên hệ quản trị viên.", true); return;}
            if (state.connectionState !== "online") {notify("Chờ kết nối phục hồi để chia sẻ vị trí.", true); return;}
            state.gps.start();
        }); start.className = "primary-wide"; start.id = "gps-start";
        content.append(node("section", {class: "gps-box"}, start, node("div", {class: "gps-status-row"}, node("p", {id: "gps-status", role: "status", "data-state": state.gpsState}, state.gpsText), node("button", {id: "gps-stop", type: "button", class: "secondary", hidden: state.gpsState === "idle", onclick: () => state.gps.stop()}, "Dừng chia sẻ"))));
        if (!state.gps) {
            const userId = state.user.id, teamId = state.user.response_team, token = state.token;
            state.gps = E.gps({geo: navigator.geolocation, timers: window, now: Date.now, intervalMs: config.gpsIntervalMs, state: gpsStatus, send: async position => {
                const epoch = state.syncEpoch;
                const data = await api.request("teams/me/location/", "POST", position);
                if (epoch !== state.syncEpoch || state.displayState !== "success" || !E.freshPosition(position)) return data;
                if (state.token === token && state.user?.id === userId && state.user.response_team === teamId) {
                    state.currentPosition = {...position, team_id: teamId, status: data.status}; state.teamName = data.name; state.teamStatus = data.status;
                    if (state.map) {state.map.update("team", state.currentPosition, data.name || "Đội của bạn"); if (state.follow) state.map.focus(position.latitude, position.longitude, false);}
                    const name = document.getElementById("team-name"); if (name) name.textContent = data.name || "Đội của bạn";
                    const readiness = document.getElementById("team-readiness"); if (readiness) {readiness.hidden = false; readiness.replaceChildren(badge(data.status));}
                    const distance = document.getElementById("mission-distance");
                    if (distance && state.destination) distance.textContent = `${(E.distance(position, state.destination) / 1000).toFixed(1)} km (ước tính)`;
                }
                return data;
            }});
        }
    }
    async function rescue(content, id, view = "home") {
        const generation = state.generation, error = errorBox(); error.id = "page-error";
        content.className += " rescue-content";
        const stage = node("div", {class: "rescue-stage"}), mapPanel = node("div", {class: "rescue-map"}); stage.append(mapPanel); content.append(stage);
        const map = mountMap(mapPanel);
        if (state.currentPosition) map.update("team", state.currentPosition, state.teamName || "Đội của bạn");
        const readiness = node("span", {id: "team-readiness", hidden: !state.teamStatus}, state.teamStatus ? badge(state.teamStatus) : null);
        const identity = node("div", {class: "team-identity"}, node("span", {class: "avatar"}, icon("shield")), node("div", {}, node("strong", {id: "team-name"}, state.teamName || "Đội của bạn"), readiness), node("span", {id: "gps-chip", hidden: state.gpsState === "idle", class: "signal", "data-state": state.gpsState}, state.gpsText));
        stage.append(node("div", {class: "rescue-overlay"}, identity));
        const layerPanel = node("div", {class: "layer-panel", hidden: true}, node("h3", {}, "Lớp bản đồ"));
        for (const [kind, title] of [["team", "Vị trí đội"], ["incident", "Hiện trường"]]) {
            const check = node("input", {type: "checkbox", checked: true, onchange: event => map.visibility?.(kind, event.target.checked)});
            layerPanel.append(node("label", {}, check, title));
        }
        const center = quickAction("locate", "Theo vị trí", () => {
            if (!state.currentPosition) {sheet.setSnap("expanded"); document.getElementById("gps-start")?.scrollIntoView?.({block: "center"}); notify("Bật chia sẻ vị trí để xác định vị trí đội."); return;}
            state.follow = !state.follow; center.setAttribute("aria-pressed", String(state.follow)); if (state.follow) map.focus(state.currentPosition.latitude, state.currentPosition.longitude);
        }); center.setAttribute("aria-pressed", String(state.follow));
        const sound = quickAction("sound", "Âm báo", async () => {
            if (state.sound) state.sound = false;
            else {const Audio = window.AudioContext || window.webkitAudioContext; if (!Audio) {notify("Thiết bị không hỗ trợ âm báo."); return;} try {state.audio ||= new Audio(); await state.audio.resume(); state.sound = true;} catch (_) {notify("Không bật được âm báo trên thiết bị.", true);}}
            sound.setAttribute("aria-pressed", String(state.sound)); if (state.sound) missionTone();
        }); sound.setAttribute("aria-pressed", String(state.sound));
        stage.append(node("div", {class: "map-tools"}, center, quickAction("layers", "Lớp bản đồ", () => {layerPanel.hidden = !layerPanel.hidden;}), quickAction("shield", "Hỗ trợ", () => support()), sound), layerPanel);
        function support() {if (selected && E.missionAction(selected.status)) {fieldRequest(selected, "support"); return;} confirm("Dùng kênh liên lạc của đơn vị để được hỗ trợ.", true);}
        const sheetBody = node("div", {class: "sheet-body"}), mission = node("div", {class: "mission-card"}), collection = node("div"), audit = node("ol", {class: "timeline"}), mediaArea = node("div");
        mission.hidden = !["home", "tasks"].includes(view);
        let insetTimer;
        const updateInset = () => {clearTimeout(insetTimer); insetTimer = setTimeout(() => {if (generation === state.generation) map.inset?.(window.innerWidth < 900 ? sheet.element.getBoundingClientRect().height : 0);}, 240);};
        const sheet = bottomSheet(sheetBody, updateInset); stage.append(sheet.element); if (id || view !== "home") sheet.setSnap("expanded");
        if (window.innerWidth < 900) map.inset?.(sheet.element.getBoundingClientRect().height);
        window.addEventListener("resize", updateInset); state.cleanups.push(() => {clearTimeout(insetTimer); window.removeEventListener("resize", updateInset);});
        const sheetHeading = node("div", {class: "sheet-heading"}, node("p", {class: "eyebrow"}, "Ứng cứu hiện trường"), node("h1", {}, id ? "Chi tiết nhiệm vụ" : view === "history" ? "Lịch sử nhiệm vụ" : view === "profile" ? "Hồ sơ đội" : view === "updates" ? "Cập nhật trực tiếp" : "Nhiệm vụ được giao"));
        sheetBody.append(sheetHeading, error, mission);
        const gpsArea = node("div"); rescueGPS(gpsArea);
        const detailBox = disclosure("Chi tiết hiện trường & lịch sử", node("div", {id: "mission-details"}), audit, mediaArea);
        detailBox.addEventListener("toggle", () => {if (detailBox.open) sheet.setSnap("expanded");});
        const secondaryArea = node("div"); sheetBody.append(detailBox, secondaryArea);
        let selected = null, mediaKey = null, sheetTask = null;
        async function fieldRequest(task, kind) {
            const dialog = node("dialog"), form = node("form"), error = errorBox(), submit = node("button", {type: "submit"}, "Gửi về điều phối"), type = select(kind === "support" ? "Lực lượng cần hỗ trợ" : "Vấn đề gặp phải", kind === "support" ? "support_type" : "problem", kind === "support" ? [["fire", "Cứu hỏa"], ["medical", "Y tế"], ["rescue", "Cứu hộ"], ["other", "Lực lượng khác"]] : [["missing_location", "Không tìm thấy vị trí"], ["unreachable_contact", "Không liên hệ được người báo"], ["blocked_road", "Đường bị chặn"], ["extra_forces", "Cần thêm lực lượng"], ["more_severe", "Nghiêm trọng hơn dự kiến"]], kind === "support" ? "rescue" : "missing_location");
            // Reuse an ID only for identical retries. A changed form is a new field request.
            let pending = null;
            form.append(type, field("Ghi chú", "note", "textarea", {maxLength: 2000}), node("p", {class: "context-note"}, "Chia sẻ vị trí để yêu cầu thêm lực lượng."), error, node("div", {class: "actions"}, submit, button("Đóng", () => dialog.close())));
            dialog.append(node("h2", {}, kind === "support" ? "Yêu cầu thêm lực lượng" : "Báo vấn đề"), form); document.body.append(dialog); dialog.showModal();
            dialog.addEventListener("close", () => dialog.remove(), {once: true}); state.cleanups.push(() => {dialog.close(); dialog.remove();});
            form.onsubmit = event => {event.preventDefault(); perform(submit, error, async () => {
                const fields = {kind, ...formData(form)}, signature = JSON.stringify(fields);
                if (!pending || pending.signature !== signature) {
                    const position = state.currentPosition, fresh = position && Date.now() - Date.parse(position.timestamp) <= 60000;
                    if (kind === "support" && !fresh) throw new Error("Chia sẻ vị trí và chờ xác định vị trí trước khi gửi.");
                    pending = {signature, body: {...fields, request_id: crypto.randomUUID(), reported_at: new Date().toISOString(), ...(fresh ? {latitude: position.latitude, longitude: position.longitude, accuracy: position.accuracy} : {})}};
                }
                await api.request(`assignments/${task.id}/signals/`, "POST", pending.body); dialog.close(); notify("Đã gửi yêu cầu đến điều phối viên."); await refresh();
            });};
        }
        async function transition(task, next, control) {
            await perform(control, error, async () => {
                const expected_status = task.status;
                if (!await confirm(next === "rejected" ? "Từ chối nhiệm vụ này?" : `${E.missionAction(task.status)?.label || labels[next]}?`)) return;
                await api.request(`assignments/${task.id}/${next === "accepted" ? "accept" : "status"}/`, next === "accepted" ? "POST" : "PATCH", {expected_status, ...(next === "accepted" ? {} : {status: next})});
                notify("Đã cập nhật tiến độ."); await refresh();
            });
        }
        function taskCard(task) {return record(incidentTitle(task.incident), task.status, task.incident.address || "Chưa có địa chỉ", [button("Mở trên bản đồ", () => go(`/rescue/assignments/${task.id}`), false)]);}
        state.refresh = async () => {
            const data = id ? await api.request(`assignments/${id}/`) : await api.all("assignments/");
            if (generation !== state.generation) return;
            const all = id ? [data] : data, active = all.filter(task => E.missionAction(task.status)), previous = selected;
            selected = id ? data : active[0] || (view === "home" && previous && all.find(task => task.id === previous.id && task.status === "completed"));
            const contactEpoch = state.contactEpoch;
            const [contacts, entries] = selected ? await Promise.all([
                E.missionAction(selected.status) ? api.request(`assignments/${selected.id}/contacts/`) : [],
                api.all(`assignments/${selected.id}/history/`),
            ]) : [[], []];
            if (generation !== state.generation || contactEpoch !== state.contactEpoch) return;
            mission.replaceChildren(); secondaryArea.replaceChildren(); sheet.footer.replaceChildren(); sheet.footer.hidden = true; error.textContent = "";
            if (["tasks", "history"].includes(view)) {
                records(collection, view === "history" ? all.filter(task => !E.missionAction(task.status)) : all, taskCard, view === "history" ? "Chưa có nhiệm vụ đã kết thúc." : "Chưa có nhiệm vụ được giao."); secondaryArea.append(collection);
            } else if (view === "updates") {
                secondaryArea.append(panel("Cập nhật mới", ...state.liveMessages.map(item => node("p", {}, `${date(item.at)} · ${item.text}`)), state.liveMessages.length ? null : node("p", {class: "empty"}, "Chưa có cập nhật mới.")));
            } else if (view === "profile") {
                secondaryArea.append(panel("Thông tin tài khoản", details([["Tài khoản", state.user.username], ["Đội", state.teamName || "Đội của bạn"], ["Quyền", "Đội ứng cứu"]]), node("p", {class: "muted"}, "Liên hệ quản trị viên để thay đổi thông tin đội."), button("Đăng xuất", () => state.logout())), gpsArea);
            }
            detailBox.hidden = !selected || !["home", "tasks"].includes(view);
            if (selected) {
                const task = selected, incident = task.incident, action = E.missionAction(task.status);
                const taskState = `${task.id}:${task.status}`;
                if (sheetTask !== taskState && view === "home" && !id) {if (["accepted", "en_route", "on_scene", "responding"].includes(task.status)) sheet.setSnap("half"); else if (task.status === "completed") sheet.setSnap("half");} sheetTask = taskState;
                sheet.element.dataset.status = task.status;
                state.destination = {latitude: incident.latitude, longitude: incident.longitude};
                map.replace("incident", [incident], () => incidentTitle(incident));
                readiness.hidden = !action && !state.teamStatus; readiness.replaceChildren(...(action ? [badge("busy")] : state.teamStatus ? [badge(state.teamStatus)] : []));
                const distance = state.currentPosition ? `${(E.distance(state.currentPosition, incident) / 1000).toFixed(1)} km (ước tính)` : "Chưa xác định khoảng cách";
                const controls = node("div", {class: "mission-actions"});
                if (action) {
                    const primary = button(action.label, event => transition(task, action.status, event.currentTarget), false); primary.className = "primary-wide"; primary.append(icon("arrow")); controls.append(primary);
                    if (task.status === "assigned") controls.append(button("Từ chối nhiệm vụ", event => transition(task, "rejected", event.currentTarget)));
                }
                mission.append(node("div", {class: "mission-kicker"}, signal(action ? "NHIỆM VỤ ĐANG XỬ LÝ" : "NHIỆM VỤ ĐÃ KẾT THÚC"), badge(task.status)), node("h2", {}, incidentTitle(incident)), node("p", {class: "destination"}, icon("pin"), incident.address || "Vị trí hiện trường trên bản đồ"));
                if (action) {sheet.footer.hidden = false; sheet.footer.append(controls);}
                if (task.status === "completed") mission.append(node("div", {class: "success-box"}, icon("check"), node("div", {}, node("strong", {}, "Nhiệm vụ đã hoàn thành"), node("p", {}, `Kết thúc ${date(task.ended_at)}.`))), button("Về màn hình chờ", () => go("/rescue")));
                if (["en_route", "accepted"].includes(task.status)) mission.append(node("p", {class: "context-note"}, "Di chuyển đến hiện trường theo tuyến đường an toàn."));
                mission.append(...contacts.map(contact => ReporterContactCard(contact, map)));
                if (action && ["assigned", "accepted", "en_route"].includes(task.status)) mission.append(node("div", {class: "mission-metrics"}, node("strong", {id: "mission-distance"}, distance), null));
                if (action) {
                    const callable = contacts.find(contact => contact.can_call && contact.allow_contact && !contact.phone_masked);
                    const call = callable ? callLink(callable, "Gọi") : node("span", {hidden: true});
                    sheet.footer.replaceChildren(node("div", {class: "quick-row mission-quick"}, button("Chi tiết", () => {detailBox.open = true; sheet.setSnap("expanded");}), call, button("Ảnh/video", () => {detailBox.open = true; sheet.setSnap("expanded"); mediaArea.scrollIntoView?.({block: "center"});}), button("Báo vấn đề", () => fieldRequest(task, "problem"))), controls);
                    mission.append(button("YÊU CẦU THÊM LỰC LƯỢNG", () => fieldRequest(task, "support")));
                }
                if (task.status === "on_scene") mission.append(node("p", {class: "context-note"}, "Sẵn sàng triển khai ứng cứu."));
                if (task.status === "responding") mission.append(node("p", {class: "context-note"}, "Hoàn thành nhiệm vụ khi công tác ứng cứu kết thúc."));
                document.getElementById("mission-details").replaceChildren(details([["Sự cố", badge(incident.status)], ["Loại", categoryName(incident.category)], ["Điều phối lúc", date(task.created_at)], ["Ghi chú", task.note || "—"]]), node("p", {class: "detail-description"}, incident.description), node("div", {class: "quick-row"}, quickAction("shield", "Liên hệ / hỗ trợ", support), button("Xem đầy đủ", () => go(`/rescue/assignments/${task.id}`))));
                audit.replaceChildren(...Array.from(missionTimeline(task.timeline).children));
                audit.append(node("li", {}, disclosure("Lịch sử phân công", node("ol", {class: "timeline"}, ...entries.map(entry => node("li", {}, node("strong", {}, labels[entry.to_status] || "Đã cập nhật"), node("small", {}, date(entry.created_at)), businessNote(entry.note) ? node("p", {}, businessNote(entry.note)) : null))))));
                if (!action) {map.replace("report", [], () => ""); mediaKey = null; state.mediaGuard = null; mediaArea.replaceChildren(node("p", {class: "context-note"}, "Ảnh/video không còn khả dụng sau khi kết thúc nhiệm vụ."));}
                else if (mediaKey !== task.incident.id) {mediaKey = task.incident.id; mediaArea.replaceChildren(); await mediaSection(mediaArea, {incident_id: task.incident.id}, () => selected && !!E.missionAction(selected.status), generation);}
            } else {
                state.destination = null; map.replace("incident", [], () => "");
                mission.append(node("div", {class: "empty-mission"}, icon("shield"), node("h2", {}, "Chờ nhiệm vụ tiếp theo"), node("p", {}, "Bạn sẽ nhận nhiệm vụ khi được điều phối.")));
                if (view === "home") secondaryArea.append(gpsArea, node("div", {class: "quick-row"}, quickAction("tasks", "Nhiệm vụ", () => go("/rescue?view=tasks")), quickAction("history", "Lịch sử", () => go("/rescue?view=history")), quickAction("shield", "Hỗ trợ", support)));
            }
            if (selected && view === "home") secondaryArea.append(gpsArea);
        };
        await refresh();
    }
    async function admin(content, section) {
        const generation = state.generation, error = errorBox(); error.id = "page-error";
        const titles = {users: "Tài khoản & phân quyền", categories: "Loại sự cố", teams: "Đội ứng cứu", config: "Cấu hình hệ thống"};
        if (!titles[section]) { content.append(heading("Không tìm thấy trang", "Chọn một mục trong thanh điều hướng.")); return; }
        content.append(heading(titles[section], "Quản lý thông tin vận hành."), error);
        if (section === "config") {
            const rules = node("div"); content.append(panel("Quy tắc vận hành", node("p", {class: "muted"}, "Liên hệ người quản lý để thay đổi."), rules));
            state.refresh = async () => {
                const data = await api.request("admin/configuration/"); if (generation !== state.generation) return;
                const values = data.values || {}, quantity = (key, unit, divisor = 1) => values[key] == null ? "Chưa cập nhật" : `${Math.round(values[key] / divisor)} ${unit}`;
                rules.replaceChildren(details([["Báo cáo gần nhau", quantity("CLUSTER_RADIUS_METERS", "m")], ["Khoảng thời gian liên quan", quantity("CLUSTER_TIME_WINDOW_MINUTES", "phút")], ["Phạm vi tìm đội", quantity("DISPATCH_RADIUS_METERS", "m")], ["Giới hạn tệp hiện trường", quantity("MEDIA_MAX_BYTES", "MB", 1048576)]]));
            }; await refresh(); return;
        }
        const search = node("form", {class: "filters"}), list = node("div"), pages = node("div", {class: "pagination"}), editor = panel("Tạo / chỉnh sửa", node("p", {class: "empty"}, "Chọn tạo mới hoặc sửa một bản ghi.")); let page, teamChoices = [], categories = [];
        search.append(field("Tìm kiếm", "search")); if (section === "users") search.append(select("Vai trò", "role", Object.entries(labels).filter(([key]) => E.homes[key]))); if (section === "teams") search.append(select("Trạng thái", "status", ["available", "busy", "offline"].map(key => [key, labels[key]])));
        search.append(node("div", {class: "actions"}, node("button", {type: "submit"}, "Tìm kiếm"), button("＋ Tạo mới", () => edit(), false)));
        content.append(panel("Tìm kiếm & bộ lọc", search), panel("Danh sách", list, pages), editor);
        [teamChoices, categories] = await Promise.all([api.all("admin/teams/?page_size=100"), api.all("admin/categories/?page_size=100")]); if (generation !== state.generation) return;
        function edit(item = null) {
            const form = node("form", {class: "editor-form"}), formError = errorBox(), save = node("button", {type: "submit"}, item ? "Lưu thay đổi" : "Tạo mới");
            if (section === "users") {
                form.append(field("Tên đăng nhập *", "username", "text", {required: true, value: item?.username || "", maxLength: 150}), field("Email", "email", "email", {value: item?.email || ""}), field("Tên", "first_name", "text", {value: item?.first_name || ""}), field("Họ", "last_name", "text", {value: item?.last_name || ""}), select("Vai trò", "role", Object.keys(E.homes).map(key => [key, labels[key]]), item?.role || "citizen", null), select("Đội ứng cứu", "response_team", teamChoices.map(team => [team.id, team.name]), item?.response_team || "", "Không có đội"), field(item ? "Mật khẩu mới (để trống nếu giữ nguyên)" : "Mật khẩu *", "password", "password", {required: !item, autoComplete: "new-password"}), select("Trạng thái tài khoản", "is_active", [["true", "Hoạt động"], ["false", "Vô hiệu hóa"]], String(item?.is_active ?? true), null));
            } else if (section === "categories") {
                form.append(field("Tên loại sự cố *", "name", "text", {required: true, value: item?.name || ""}), field("Mã *", "code", "text", {required: true, value: item?.code || "", pattern: "[a-zA-Z0-9_-]+"}), field("Mô tả", "description", "textarea", {value: item?.description || ""}), select("Hiển thị khi gửi báo cáo", "is_active", [["true", "Hoạt động"], ["false", "Ngừng sử dụng"]], String(item?.is_active ?? true), null));
            } else {
                form.append(field("Tên đội *", "name", "text", {required: true, value: item?.name || ""}), field("Mã đội *", "code", "text", {required: true, value: item?.code || "", pattern: "[a-zA-Z0-9_-]+"}), select("Trạng thái", "status", ["available", "offline", ...(item?.status === "busy" ? ["busy"] : [])].map(key => [key, labels[key]]), item?.status || "offline", null));
                const categoryField = field("Loại sự cố hỗ trợ (giữ Ctrl/Cmd để chọn nhiều)", "categories", "select", {multiple: true, size: 4}); categories.forEach(category => categoryField.querySelector("select").append(node("option", {value: category.id, selected: !!item?.categories.includes(category.id)}, category.name))); form.append(categoryField);
                form.append(disclosure("Vị trí đội", node("div", {class: "grid"}, field("Vĩ độ", "latitude", "number", {step: "any", min: -90, max: 90, value: item?.location?.latitude ?? "", disabled: item?.status === "busy"}), field("Kinh độ", "longitude", "number", {step: "any", min: -180, max: 180, value: item?.location?.longitude ?? "", disabled: item?.status === "busy"})), node("p", {class: "muted"}, "Đội đang làm nhiệm vụ tự cập nhật vị trí.")));
            }
            form.append(formError, node("div", {class: "actions"}, save, button("Đóng", () => editor.replaceChildren(node("h2", {}, "Tạo / chỉnh sửa")))));
            form.onsubmit = event => { event.preventDefault(); perform(save, formError, async () => {
                const values = formData(form); if ("is_active" in values) values.is_active = values.is_active === "true";
                if (section === "users") { values.response_team = values.role === "rescue_team" && values.response_team ? Number(values.response_team) : null; if (!values.password) delete values.password; }
                if (section === "teams") {
                    values.categories = [...form.elements.categories.selectedOptions].map(option => Number(option.value));
                    if (values.latitude === "" && values.longitude === "") {delete values.latitude; delete values.longitude;}
                    else if (values.latitude !== undefined || values.longitude !== undefined) {
                        if (values.latitude === "" || values.longitude === "") throw new Error("Điền đầy đủ vị trí hoặc để trống cả hai ô.");
                        values.latitude = Number(values.latitude); values.longitude = Number(values.longitude);
                        if (item?.location && values.latitude === item.location.latitude && values.longitude === item.location.longitude) {delete values.latitude; delete values.longitude;}
                    }
                }
                if (item && !await confirm(`Lưu thay đổi ${titles[section].toLowerCase()}?`)) return;
                await api.request(`admin/${section}/${item ? item.id + "/" : ""}`, item ? "PATCH" : "POST", values); notify("Đã lưu dữ liệu.");
                state.categories = await api.all("incident-categories/?page_size=100"); [teamChoices, categories] = await Promise.all([api.all("admin/teams/?page_size=100"), api.all("admin/categories/?page_size=100")]); await refresh(); editor.replaceChildren(node("h2", {}, "Tạo / chỉnh sửa"), node("p", {class: "hint"}, "Đã lưu. Chọn bản ghi khác để tiếp tục."));
            }); }; editor.replaceChildren(node("h2", {}, item ? "Chỉnh sửa" : "Tạo mới"), form); editor.scrollIntoView({behavior: "smooth", block: "start"});
        }
        state.refresh = async () => { const data = await api.request(page || `admin/${section}/?` + query(search)); if (generation !== state.generation) return;
            const table = node("table", {}, node("thead", {}, node("tr", {}, ...[section === "users" ? "Tài khoản" : "Tên", "Thông tin", "Trạng thái", "Thao tác"].map(title => node("th", {}, title)))), node("tbody"));
            data.results.forEach(item => {
                const remove = button(section === "users" ? "Vô hiệu hóa" : "Xóa", event => perform(event.currentTarget, error, async () => {
                    if (!await confirm(section === "users" ? `Vô hiệu hóa tài khoản ${item.username}? Lịch sử được giữ nguyên.` : `Xóa ${item.name}? Nếu đã được sử dụng, hãy ngừng hoạt động thay vì xóa.`)) return;
                    await api.request(`admin/${section}/${item.id}/`, "DELETE"); notify("Đã cập nhật dữ liệu."); await refresh();
                }));
                table.querySelector("tbody").append(node("tr", {}, node("td", {}, item.username || item.name), node("td", {}, section === "users" ? `${labels[item.role] || "Chưa có vai trò"}${item.response_team ? " · " + (teamChoices.find(team => team.id === item.response_team)?.name || "Đội ứng cứu") : ""}` : item.description || "—"), node("td", {}, section === "teams" ? badge(item.status) : item.is_active ? "Hoạt động" : "Ngừng sử dụng"), node("td", {}, node("div", {class: "actions"}, button("Sửa", () => edit(item)), remove))));
            });
            list.replaceChildren(data.results.length ? node("div", {class: "table-wrap"}, table) : node("p", {class: "empty"}, "Không có kết quả.")); pagination(pages, data, "", value => {page = value;}, refresh); error.textContent = "";
        }; search.onsubmit = event => { event.preventDefault(); page = null; refresh(true); }; await refresh();
    }
    async function render() {
        state.lastUpdated = null; state.snapshotReady = false; state.refreshError = false; state.rendering = true;
        state.cleanups?.forEach(cleanup => cleanup()); state.cleanups = []; state.contactNodes = new Set(); state.contactEpoch = (state.contactEpoch || 0) + 1; state.drawerMap = null; state.destination = null; state.generation++; state.refresh = null; state.mediaGuard = null; state.visibleTeamIds = null; if (state.map) state.map.destroy(); state.map = null;
        const route = location.hash.slice(1) || (state.user ? E.homes[state.user.role] : "/login"), path = route.split("?")[0];
        if (!state.user) { if (path !== "/login") { go("/login"); return; } login(); return; }
        if (path === "/login" || !E.allowed(state.user.role, path)) { if (path !== "/login") notify("Bạn không có quyền truy cập màn hình này.", true); go(E.homes[state.user.role]); return; }
        const content = shell(route), parts = path.split("/"), generation = state.generation; setDisplayState("loading");
        const loading = node("div", {class: "loading skeleton", role: "status"}, node("span", {}, "Đang tải…"), node("i"), node("i"), node("i")); content.append(loading);
        if (window.scrollTo) window.scrollTo(0, 0);
        try {
            if (path === "/citizen/report") citizenCreate(content);
            else if (path === "/citizen/reports") await reportList(content);
            else if (parts[1] === "citizen" && parts[2] === "reports" && /^\d+$/.test(parts[3] || "")) await reportDetail(content, parts[3], false);
            else if (parts[1] === "dispatcher") await dispatcher(content, new URLSearchParams(route.split("?")[1]).get("view"), parts[3] ? {kind: parts[2], id: parts[3]} : null);
            else if (path === "/rescue") await rescue(content, null, new URLSearchParams(route.split("?")[1]).get("view") || "home");
            else if (parts[1] === "rescue" && parts[2] === "assignments" && /^\d+$/.test(parts[3] || "")) await rescue(content, parts[3]);
            else if (parts[1] === "admin") await admin(content, parts[2]);
            else content.append(heading("Không tìm thấy trang", "Chọn một mục trong thanh điều hướng."));
            if (generation === state.generation) state.rendering = false;
            if (generation === state.generation && !state.refreshError) {if (path === "/citizen/report") setDisplayState(navigator.onLine === false ? "offline" : "success"); else if (!state.snapshotReady) snapshotCompleted();}
        } catch (error) { if (generation === state.generation) {state.refreshError = true; if (error.name !== "ObsoleteSnapshot") setDisplayState(error.status === 0 ? "offline" : "error", E.publicError(error));} }
        finally {if (generation === state.generation) state.rendering = false; loading.remove();}
    }
    try {
        let response;
        try {response = await fetch("/realtime/config.json");} catch (_) {throw new E.APIError(0);}
        if (!response.ok) throw new E.APIError(response.status);
        try {config = await response.json();} catch (_) {throw new E.APIError(503);}
        api = E.apiClient({base: config.apiBase, fetcher: fetch, token: () => state.token, snapshot: () => `${state.generation}:${state.syncEpoch}`, canWrite: () => navigator.onLine !== false && (!state.user || state.user.role === "citizen" && (state.displayState === "success" || location.hash.split("?")[0] === "#/citizen/report") || (state.displayState === "success" && state.connectionState === "online")), unauthorized: () => clearSession("Phiên đăng nhập đã hết hạn. Vui lòng đăng nhập lại.")});
        window.addEventListener("offline", () => {state.socket?.pause(); updateConnection("Mất kết nối", "offline");});
        window.addEventListener("online", () => {
            if (state.socket) state.socket.retry();
            else if (state.user) {
                state.syncEpoch++; state.connectionState = "idle";
                if (state.refresh) {setDisplayState(state.lastUpdated ? "stale" : "loading", "Đang cập nhật…"); refresh(true);}
                else setDisplayState("success");
            }
        });
        window.addEventListener("hashchange", render); window.addEventListener("pagehide", () => {clearTimeout(deferredRefreshTimer); state.generation++; state.cleanups?.forEach(cleanup => cleanup()); if (state.gps) state.gps.stop(); if (state.socket) state.socket.stop(); if (state.map) state.map.destroy(); });
        document.querySelector(".skip").addEventListener("click", event => { event.preventDefault(); document.getElementById("content").focus(); });
        if (state.token) { state.user = await api.request("auth/me/"); state.categories = await api.all("incident-categories/?page_size=100"); }
        await render(); if (state.user) connectRealtime(); setInterval(refresh, 30000);
    } catch (error) { app.replaceChildren(node("p", {class: "error", role: "alert"}, E.publicError(error)), button("Thử lại", () => location.reload())); }
})();
