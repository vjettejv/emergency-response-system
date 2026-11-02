(function (root) {
    "use strict";
    // One outstanding request, bounded refresh, no ETA retained after a failure or stale GPS.
    function tracker({load, fresh, online, state, timers = root, now = Date.now, intervalMs = 60000}) {
        let version = 0, busy = false, following = false, destroyed = false, last = 0, result = null;
        async function calculate() {
            if (busy || destroyed) return;
            if (!online()) {invalidate("offline"); return;}
            const current = ++version; busy = true; result = null; last = now(); state("loading");
            try {
                const value = await load();
                if (current !== version || destroyed) return;
                if (!online() || following && !fresh()) {invalidate("stale"); return;}
                result = value; state("success", value);
            } catch (error) {if (current === version && !destroyed) {result = null; state(error.status === 0 ? "offline" : "error", null, error);}}
            finally {busy = false;}
        }
        function invalidate(mode = "empty") {version++; result = null; if (!destroyed) state(mode);}
        function tick() {
            if (destroyed || !following) return;
            if (!online()) {if (result) invalidate("offline"); return;}
            if (!fresh()) {if (result) invalidate("stale"); return;}
            if (!busy && now() - last >= Math.max(30000, intervalMs)) calculate();
        }
        const timer = timers.setInterval(tick, 1000);
        return {calculate, invalidate, tick, follow(value) {following = value; if (value) {if (fresh()) calculate(); else invalidate("stale");}},
            destroy() {destroyed = true; version++; result = null; timers.clearInterval(timer);}};
    }
    function element(tag, text, attrs = {}) {
        const node = document.createElement(tag); if (text) node.textContent = text;
        for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
        return node;
    }
    function action(text, click) {const b = element("button", text, {type: "button", class: "secondary"}); b.onclick = click; return b;}
    function field(title, type, value = "") {const label = element("label", null, {class: "field"}), input = element("input", null, {type}); input.value = value; label.append(element("span", title), input); return {label, input};}
    const time = value => new Date(value).toLocaleString("vi-VN", {day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit"});
    const distance = meters => meters < 1000 ? `${Math.round(meters)} m` : `${(meters / 1000).toFixed(1)} km`;
    function mountRoute({container, map, api, config, online, position, assignment = () => null}) {
        const section = element("section", null, {class: "panel route-planner"}), status = element("p", "Chọn điểm xuất phát và điểm đến.", {role: "status"}), summary = element("div", null, {class: "route-summary"});
        const start = element("p", "Chưa chọn điểm xuất phát"), end = element("p", "Chưa chọn điểm đến"), controls = element("div", null, {class: "actions"});
        let origin = null, destination = null, automatic = false, context = null, disposed = false;
        section.append(element("h2", "Tuyến đường gợi ý"), start, end, controls, status, summary,
            element("p", "OSRM · © OpenStreetMap contributors. Thời gian dự kiến chưa xét giao thông thời gian thực.", {class: "muted"}));
        container.append(section);
        const clear = () => {summary.replaceChildren(); map.path?.("route", []);};
        const following = element("input", null, {type: "checkbox"}), followLabel = element("label", null, {class: "route-follow"});
        followLabel.append(following, document.createTextNode(" Cập nhật theo vị trí đội"));
        const engine = tracker({intervalMs: config.routingRefreshMs || 60000, online,
            fresh: () => root.Emergency.freshPosition(position()),
            load: async () => {
                const task = assignment();
                if (automatic && task) return api.request(`assignments/${task.id}/route/`, "POST", {});
                const from = following.checked ? position() : origin;
                if (!from || !destination) throw new Error("Chọn đủ hai vị trí để tính tuyến đường.");
                if (following.checked && !root.Emergency.freshPosition(from)) throw new Error("Chưa có vị trí đội mới cập nhật.");
                return api.request("routes/", "POST", {origin: {latitude: from.latitude, longitude: from.longitude}, destination});
            },
            state: (mode, result, error) => {
                clear(); section.dataset.state = mode;
                status.textContent = mode === "loading" ? "Đang tìm tuyến đường…" : mode === "offline" ? "Mất kết nối. Tuyến đường tạm thời chưa cập nhật." : mode === "stale" ? "Vị trí đội chưa cập nhật. Chưa thể ước tính giờ đến." : mode === "error" ? error?.status === 503 ? "Chưa thể tính tuyến đường. Kiểm tra cấu hình hoặc thử lại sau." : root.Emergency.publicError(error) : mode === "empty" ? "Chọn điểm xuất phát và điểm đến." : "Tuyến đường gợi ý · không phải lịch sử GPS";
                if (mode !== "success") return;
                const minutes = result.duration_seconds < 60 ? "Dưới 1 phút" : `${Math.ceil(result.duration_seconds / 60)} phút`;
                summary.append(element("strong", `${following.checked ? "Còn khoảng " : "Quãng đường "}${distance(result.distance_meters)}`), element("p", `Thời gian dự kiến: ${minutes}`), element("p", `Dự kiến đến: ${time(result.estimated_arrival_at)}`));
                map.path?.("route", [result.geometry.coordinates], !following.checked);
            }});
        function changed() {automatic = false; following.checked = false; engine.follow(false); engine.invalidate();}
        function markers() {map.replace("routepoint", [origin && {...origin, id: "start"}, destination && {...destination, id: "end"}].filter(Boolean), p => p.id === "start" ? "Điểm xuất phát" : "Điểm đến");}
        function pick(kind) {
            changed(); status.textContent = kind === "start" ? "Chọn điểm xuất phát trên bản đồ." : "Chọn điểm đến trên bản đồ.";
            map.pick((latitude, longitude) => {const point = {latitude, longitude}; if (kind === "start") {origin = point; start.textContent = "Đã chọn điểm xuất phát";} else {destination = point; end.textContent = "Đã chọn điểm đến";} map.pick(null); markers(); status.textContent = "Bấm Tìm tuyến đường khi đã chọn đủ hai điểm.";});
        }
        const calculate = action("Tìm tuyến đường", () => engine.calculate());
        controls.append(action("Chọn điểm đi trên bản đồ", () => pick("start")), action("Chọn điểm đến trên bản đồ", () => pick("end")), action("Dùng vị trí hiện tại", () => {
            changed(); if (!root.navigator?.geolocation) {status.textContent = "Thiết bị không hỗ trợ lấy vị trí."; return;}
            status.textContent = "Đang lấy vị trí…";
            root.navigator.geolocation.getCurrentPosition(sample => {
                if (disposed) return; origin = {latitude: sample.coords.latitude, longitude: sample.coords.longitude}; start.textContent = "Điểm xuất phát: vị trí hiện tại"; markers(); map.focus(origin.latitude, origin.longitude); status.textContent = "Đã lấy vị trí. Chọn điểm đến rồi tìm tuyến đường.";
            }, () => {if (!disposed) status.textContent = "Không lấy được vị trí. Bạn có thể chọn trên bản đồ.";}, {enableHighAccuracy: true, timeout: 15000, maximumAge: 0});
        }), calculate);
        following.onchange = () => {automatic = Boolean(assignment()); engine.follow(following.checked);};
        section.append(followLabel);
        if (!config.routingEnabled) {status.textContent = "Chức năng định tuyến chưa được cấu hình."; for (const button of controls.children) button.disabled = true; following.disabled = true;}
        function sync() {
            const task = assignment(), next = task ? `${task.id}:${task.status}` : null;
            followLabel.hidden = !task;
            if (context !== next) {
                context = next; automatic = Boolean(task); following.checked = false; engine.follow(false); engine.invalidate();
                if (task) {destination = {latitude: task.incident.latitude, longitude: task.incident.longitude}; end.textContent = "Điểm đến: " + task.incident.title; start.textContent = "Điểm xuất phát: vị trí mới nhất của đội"; status.textContent = "Tính tuyến từ vị trí đội đến hiện trường.";}
                if (!config.routingEnabled) status.textContent = "Chức năng định tuyến chưa được cấu hình.";
            }
        }
        sync();
        return {sync, invalidate() {if (config.routingEnabled) engine.invalidate("offline");}, destroy() {disposed = true; engine.destroy(); map.pick(null); clear();}};
    }
    function mountHistory({container, map, api, teamChoices, online}) {
        const section = element("section", null, {class: "panel journey-history"}), filters = element("form", null, {class: "filters"}), list = element("div"), pages = element("div", null, {class: "actions"}), status = element("p", "Đang tải hành trình…", {role: "status"}), info = element("div");
        const local = date => new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16), now = new Date();
        const since = field("Từ", "datetime-local", local(new Date(now.getTime() - 86400000))), until = field("Đến", "datetime-local", local(now));
        const teams = element("select", null, {"aria-label": "Đội ứng cứu"}); teams.append(element("option", "Tất cả đội được phép xem", {value: ""}));
        for (const team of teamChoices) teams.append(element("option", team.name, {value: String(team.team_id)}));
        const teamField = element("label", null, {class: "field"}); teamField.append(element("span", "Đội ứng cứu"), teams);
        const submit = element("button", "Xem hành trình", {type: "submit"}); filters.append(since.label, until.label); if (teamChoices.length) filters.append(teamField); filters.append(submit);
        section.append(element("h2", "Lịch sử GPS thực tế"), filters, status, list, pages, info, element("p", "Đường liền màu xanh lá: các điểm GPS đã lưu. Khoảng mất tín hiệu hoặc bước nhảy bất thường được tách đoạn.", {class: "muted"})); container.append(section);
        let version = 0, disposed = false, range = null, selectedId = null;
        function query() {
            const start = new Date(since.input.value), end = new Date(until.input.value);
            if (!Number.isFinite(+start) || !Number.isFinite(+end) || start >= end) throw new Error("Chọn khoảng thời gian hợp lệ.");
            return new URLSearchParams({since: start.toISOString(), until: end.toISOString(), ...(teams.value ? {team: teams.value} : {})});
        }
        async function load(path) {
            const current = ++version; list.replaceChildren(); pages.replaceChildren(); info.replaceChildren(); map.path?.("history", []); status.textContent = "Đang tải hành trình…";
            try {
                range = query(); const data = await api.request(path || "journeys/?" + range);
                if (disposed || current !== version) return;
                status.textContent = data.results.length ? "Chọn một hành trình để xem đường đi." : "Chưa có hành trình trong khoảng thời gian này.";
                for (const journey of data.results) {
                    const card = element("article", null, {class: "record"});
                    card.append(element("h3", journey.team_name), element("p", journey.incident_title || "Theo dõi vị trí đội"), element("p", `${time(journey.started_at)} – ${time(journey.last_recorded_at)}`), element("small", journey.tracking_state === "recording" ? "Đang ghi nhận vị trí" : journey.tracking_state === "signal_lost" ? "Tín hiệu đã gián đoạn" : "Đã kết thúc"), action("Xem đường đi thực tế", () => show(journey.id)));
                    list.append(card);
                }
                if (data.previous) pages.append(action("← Trước", () => load(data.previous))); if (data.next) pages.append(action("Tiếp →", () => load(data.next)));
                if (selectedId && data.results.some(item => item.id === selectedId)) await show(selectedId);
                else selectedId = null;
            } catch (error) {if (!disposed && current === version) status.textContent = root.Emergency.publicError(error);}
        }
        async function show(id) {
            selectedId = id;
            const current = ++version; info.replaceChildren(); map.path?.("history", []); status.textContent = "Đang tải đường đi…";
            try {
                const data = await api.request(`journeys/${id}/?${range}`);
                if (disposed || current !== version) return;
                if (!online()) {invalidate(); return;}
                status.textContent = data.points.length ? "Lịch sử GPS đã lưu" : "Không có điểm GPS trong khoảng đã chọn.";
                map.path?.("history", data.segments, true);
                if (data.points.length) info.append(element("strong", `${data.points.length} điểm đã ghi nhận · ${distance(data.distance_meters)}`), element("p", `${data.segments.length} đoạn · khoảng cách chỉ tính giữa các mẫu liên tục, không phải đồng hồ đo hành trình.`));
            } catch (error) {if (!disposed && current === version) status.textContent = root.Emergency.publicError(error);}
        }
        function invalidate() {version++; info.replaceChildren(); list.replaceChildren(); pages.replaceChildren(); map.path?.("history", []); status.textContent = "Mất kết nối. Hãy tải lại hành trình khi kết nối phục hồi.";}
        filters.onsubmit = event => {event.preventDefault(); selectedId = null; load();};
        return {load, invalidate, destroy() {disposed = true; version++; map.path?.("history", []);}};
    }
    const exports = {tracker, mountRoute, mountHistory};
    root.EmergencyJourneys = exports;
    if (typeof module !== "undefined") module.exports = exports;
})(typeof window === "undefined" ? globalThis : window);
