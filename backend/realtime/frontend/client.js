(function (root) {
    "use strict";
    const homes = {citizen: "/citizen/report", dispatcher: "/dispatcher", rescue_team: "/rescue", admin: "/admin/users"};
    // Labels for existing API states. Transition validation remains in backend services.
    const missionActions = {
        assigned: {status: "accepted", label: "Nhận nhiệm vụ"},
        accepted: {status: "en_route", label: "Bắt đầu di chuyển"},
        en_route: {status: "on_scene", label: "Đã đến hiện trường"},
        on_scene: {status: "responding", label: "Bắt đầu xử lý"},
        responding: {status: "completed", label: "Hoàn thành nhiệm vụ"},
    };
    function missionAction(status) { return missionActions[status] || null; }
    function sheetSnap(current, delta) {
        const stops = ["peek", "half", "expanded"], index = Math.max(0, stops.indexOf(current));
        return stops[Math.max(0, Math.min(2, index + (delta < -45 ? 1 : delta > 45 ? -1 : 0)))];
    }
    function allowed(role, route) {
        const dispatcher = /^\/dispatcher(?:\/(?:reports|incidents)\/\d+)?$/.test(route);
        return route === "/login" || (role === "admin" && (/^\/admin\/(?:users|categories|teams|config)$/.test(route) || dispatcher)) ||
            (role === "dispatcher" && dispatcher) || (role === "citizen" && /^\/citizen\/(?:report|reports(?:\/\d+)?)$/.test(route)) || (role === "rescue_team" && /^\/rescue(?:\/assignments\/\d+)?$/.test(route));
    }
    function describe(body) {
        if (typeof body === "string") return body;
        if (Array.isArray(body)) return body.map(describe).join("; ");
        return Object.entries(body || {}).map(([key, value]) => `${key === "detail" ? "" : key + ": "}${describe(value)}`).join(" · ");
    }
    class APIError extends Error {
        constructor(status, body, retryAfter) { super(describe(body) || "Không thể kết nối máy chủ."); this.status = status; this.body = body; this.retryAfter = retryAfter; }
    }
    function apiClient({base, fetcher, token, unauthorized}) {
        async function request(path, method = "GET", body) {
            const origin = new URL(base, root.location ? root.location.origin : "http://test.local"), url = new URL(path, origin);
            if (url.origin !== origin.origin || !url.pathname.startsWith(origin.pathname)) throw new APIError(400, "Đường dẫn API không hợp lệ.");
            let response;
            try { response = await fetcher(url.href, {method, headers: {"Content-Type": "application/json", ...(token() ? {Authorization: `Token ${token()}`} : {})}, ...(body === undefined ? {} : {body: JSON.stringify(body)})}); }
            catch (_) { throw new APIError(0, "Mất kết nối máy chủ. Kiểm tra mạng và thử lại."); }
            const data = response.status === 204 ? null : await response.json().catch(() => ({detail: "Phản hồi máy chủ không hợp lệ."}));
            if (!response.ok) { if (response.status === 401) unauthorized(); throw new APIError(response.status, data, Number(response.headers.get("Retry-After")) || 15); }
            return data;
        }
        async function all(path) { const records = []; let next = path; while (next) { const data = await request(next); records.push(...(data.results || data)); next = data.next; } return records; }
        return {request, all};
    }
    function realtime({url, token, Socket, timers, now, state, event, ready, revoked}) {
        let socket, stopped = false, attempt = 0, timeout, heartbeat, lastMessage;
        function connect() {
            if (stopped) return;
            state("Đang kết nối…", "pending"); socket = new Socket(url);
            socket.onopen = () => { if (stopped) return; lastMessage = now(); socket.send(JSON.stringify({type: "authenticate", token: token()})); };
            socket.onmessage = message => {
                if (stopped) return;
                lastMessage = now(); let data; try { data = JSON.parse(message.data); } catch (_) { return; }
                if (data.type === "ready") {
                    attempt = 0; state("Trực tuyến", "online"); ready(); timers.clearInterval(heartbeat);
                    heartbeat = timers.setInterval(() => { if (now() - lastMessage > 45000) socket.close(); else if (socket.readyState === 1) socket.send(JSON.stringify({type: "ping"})); }, 15000);
                } else if (!["heartbeat", "pong"].includes(data.type)) event(data);
            };
            socket.onerror = () => { if (!stopped) state("Kết nối gián đoạn", "offline"); };
            socket.onclose = ({code}) => {
                timers.clearInterval(heartbeat); if (stopped) return;
                if ([4400, 4401, 4403].includes(code)) { stopped = true; state("Phiên hoặc quyền đã thay đổi", "offline"); revoked(); return; }
                state("Mất kết nối · đang thử lại", "offline"); timeout = timers.setTimeout(connect, Math.min(30000, 1000 * 2 ** attempt++));
            };
        }
        connect(); return {stop() { stopped = true; timers.clearTimeout(timeout); timers.clearInterval(heartbeat); if (socket) socket.close(); }};
    }
    function distance(a, b) {
        const rad = n => n * Math.PI / 180, dl = rad(b.latitude - a.latitude), dn = rad(b.longitude - a.longitude);
        const d = Math.sin(dl / 2) ** 2 + Math.cos(rad(a.latitude)) * Math.cos(rad(b.latitude)) * Math.sin(dn / 2) ** 2;
        return 6371000 * 2 * Math.atan2(Math.sqrt(d), Math.sqrt(Math.max(0, 1 - d)));
    }
    function gps({geo, timers, now, send, state, intervalMs = 10000}) {
        let watch = null, timer, latest, sent, sending = false, retryAt = 0, generation = 0;
        async function tick() {
            if (watch === null || sending || !latest || now() < retryAt || now() - Date.parse(latest.timestamp) > 60000) return;
            if (sent && distance(sent, latest) < 10 && now() - sent.sentAt < 30000) return;
            sending = true; const current = latest, activeGeneration = generation;
            try { await send(current); if (generation === activeGeneration) { sent = {...current, sentAt: now()}; state("Đang chia sẻ · vừa gửi vị trí", "online"); } }
            catch (error) {
                if (generation !== activeGeneration) return;
                if ([401, 403].includes(error.status)) { stop(); state("Không có quyền gửi GPS", "offline"); }
                else { retryAt = now() + (error.status === 429 ? error.retryAfter * 1000 : 10000); state(error.message, "offline"); }
            } finally { sending = false; }
        }
        function stop() { generation++; if (watch !== null) geo.clearWatch(watch); watch = null; latest = null; sent = null; timers.clearInterval(timer); state("GPS đã dừng", "idle"); }
        function start() {
            if (!geo) { state("Thiết bị không hỗ trợ GPS", "offline"); return; } if (watch !== null) return;
            generation++; retryAt = 0; state("Đang lấy vị trí…", "pending");
            watch = geo.watchPosition(position => { latest = {latitude: position.coords.latitude, longitude: position.coords.longitude, accuracy: position.coords.accuracy, timestamp: new Date(position.timestamp).toISOString()}; }, error => { if (error.code === 1) stop(); state(`GPS: ${error.message}`, "offline"); }, {enableHighAccuracy: true, maximumAge: 5000, timeout: 15000});
            timer = timers.setInterval(tick, intervalMs);
        }
        return {start, stop, tick};
    }
    function normalizePhone(value) {
        const phone = String(value || "").replace(/[ .()-]/g, "");
        if (phone && !/^\+?[0-9]{8,15}$/.test(phone)) throw new Error("Số điện thoại cần 8–15 chữ số, có thể bắt đầu bằng +.");
        return phone;
    }
    function camera({devices, canvas, now, makeFile}) {
        let stream = null, generation = 0;
        function stop() {generation++; if (stream) stream.getTracks().forEach(track => track.stop()); stream = null;}
        async function start(video) {
            stop(); const current = generation;
            if (!devices?.getUserMedia) throw new Error("Camera cần HTTPS hoặc localhost và trình duyệt hỗ trợ. Báo cáo vẫn có thể gửi không kèm ảnh.");
            const incoming = await devices.getUserMedia({video: {facingMode: {ideal: "environment"}}, audio: false});
            if (generation !== current) {incoming.getTracks().forEach(track => track.stop()); return false;}
            stream = incoming; video.srcObject = incoming;
            try {await video.play();} catch (error) {stop(); throw error;}
            return true;
        }
        async function capture(video, position) {
            if (!stream || !video.videoWidth || !video.videoHeight) throw new Error("Camera chưa sẵn sàng. Vui lòng thử lại.");
            const current = generation, captured = now(), element = canvas();
            // Bound bitmap memory and upload size; this is a browser capture, not location attestation.
            const ratio = Math.min(1, 1600 / Math.max(video.videoWidth, video.videoHeight));
            element.width = Math.round(video.videoWidth * ratio); element.height = Math.round(video.videoHeight * ratio);
            element.getContext("2d").drawImage(video, 0, 0, element.width, element.height);
            const blob = await new Promise(resolve => element.toBlob(resolve, "image/jpeg", 0.85));
            if (generation !== current) return null;
            if (!blob) throw new Error("Không chụp được ảnh. Hãy thử lại.");
            const metadata = {capture_source: "camera", captured_at: new Date(captured).toISOString()};
            if (position && captured - Date.parse(position.timestamp) >= 0 && captured - Date.parse(position.timestamp) <= 60000) {
                Object.assign(metadata, {capture_latitude: position.latitude, capture_longitude: position.longitude, capture_accuracy: position.accuracy});
            }
            stop(); video.srcObject = null;
            return {file: makeFile(blob, `scene-${captured}.jpg`), metadata};
        }
        return {start, stop, capture};
    }
    async function upload(api, target, file, {maxBytes, crypto, fetcher, metadata = {}, intent}) {
        if (!file.size || file.size > maxBytes) throw new Error(`File phải từ 1 byte đến ${Math.round(maxBytes / 1048576)} MB.`);
        if (!crypto || !crypto.subtle) throw new Error("Upload cần HTTPS hoặc localhost để tính checksum an toàn.");
        const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", await file.arrayBuffer()));
        const checksum = root.btoa(String.fromCharCode(...digest));
        const result = await api.request("media/presign/", "POST", {...target, ...metadata, filename: file.name, content_type: file.type, size_bytes: file.size, checksum_sha256: checksum});
        intent?.(result.media.id);
        let response; try { response = await fetcher(result.upload.url, {method: result.upload.method, headers: result.upload.headers, body: file}); }
        catch (_) { throw new Error("Không thể tải lên S3. Kiểm tra mạng và cấu hình CORS bucket."); }
        if (!response.ok) throw new Error(`S3 từ chối upload (${response.status}). Tạo URL mới để thử lại.`);
        return api.request(`media/${result.media.id}/confirm/`, "POST", {});
    }
    const exports = {homes, allowed, describe, APIError, apiClient, realtime, gps, distance, upload, missionAction, sheetSnap, camera, normalizePhone};
    if (typeof module !== "undefined") module.exports = exports;
    root.Emergency = exports;
})(typeof window === "undefined" ? globalThis : window);
