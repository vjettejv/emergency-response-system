(function (root) {
    "use strict";
    const homes = {citizen: "/citizen/report", dispatcher: "/dispatcher", rescue_team: "/rescue", admin: "/admin/users"};
    const homeURL = role => Object.hasOwn(homes, role) ? `/realtime/#${homes[role]}` : "/login";
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
        const fields = {username: "Tên đăng nhập", ["password"]: "Mật khẩu", email: "Email", reporter_name: "Họ tên", reporter_phone: "Số điện thoại", category: "Loại sự cố", description: "Mô tả", name: "Tên", title: "Tên sự cố", code: "Mã", latitude: "Vị trí", longitude: "Vị trí", gps_location: "Vị trí", occurred_at: "Thời điểm", categories: "Loại sự cố hỗ trợ", response_team: "Đội ứng cứu", file: "Tệp đính kèm", size_bytes: "Kích thước tệp"};
        if (typeof body === "string") return /[À-ỹ]/.test(body) && body.length <= 240 && !/S3|WebSocket|backend|checksum|[a-z]+_[a-z_]+|#\d+|\b(HTTP|SQL|SRID|Redis|Celery|PostgreSQL|traceback|payload|metadata|timestamp|pending|accepted|assigned|resolved)\b/i.test(body) ? body : "Vui lòng kiểm tra thông tin và thử lại.";
        if (Array.isArray(body)) return describe(body[0]);
        if (body?.non_field_errors) return describe(body.non_field_errors[0]);
        const errors = Object.keys(body || {}).filter(key => fields[key]).slice(0, 2);
        return errors.length ? [...new Set(errors.map(key => `${fields[key]}: Vui lòng kiểm tra lại.`))].join(" ") : describe(body?.detail || "");
    }
    function publicError(error) {return error instanceof APIError ? error.message : describe(error?.message || "");}
    function friendlyTime(value, now = Date.now()) {
        const parsed = Date.parse(value); if (!Number.isFinite(parsed)) return "Chưa cập nhật";
        const minutes = Math.max(0, Math.floor((now - parsed) / 60000));
        if (minutes < 1) return "Vừa xong"; if (minutes < 60) return `${minutes} phút trước`;
        const date = new Date(parsed), today = new Date(now);
        return date.toLocaleDateString("vi-VN") === today.toLocaleDateString("vi-VN") ? `Hôm nay, ${date.toLocaleTimeString("vi-VN", {hour: "2-digit", minute: "2-digit"})}` : date.toLocaleDateString("vi-VN", {day: "2-digit", month: "2-digit", ...(date.getFullYear() !== today.getFullYear() ? {year: "numeric"} : {})});
    }
    class APIError extends Error {
        constructor(status, body, retryAfter, path = "") {
            const invalidLogin = path === "auth/login/" && (status === 401 || (status === 400 && (body?.non_field_errors || body?.detail)));
            const authUnavailable = ["auth/login/", "auth/register/"].includes(path) && (status === 0 || status >= 500);
            const message = invalidLogin ? "Tên đăng nhập hoặc mật khẩu không đúng." : authUnavailable ? "Không thể kết nối tới hệ thống. Vui lòng thử lại." : status === 0 ? "Mất kết nối. Kiểm tra mạng và thử lại." : status === 401 ? "Phiên đăng nhập đã hết hạn. Vui lòng đăng nhập lại." : status === 403 ? "Bạn không có quyền thực hiện thao tác này." : status === 404 ? "Thông tin này không còn khả dụng." : status === 409 ? "Thông tin đã thay đổi. Cập nhật và thử lại." : status === 429 ? "Bạn thao tác quá nhanh. Vui lòng chờ một chút." : status >= 500 ? "Tạm thời không tải được dữ liệu. Vui lòng thử lại." : describe(body);
            super(message); this.status = status; this.body = body; this.retryAfter = retryAfter;
        }
    }
    function apiClient({base, fetcher, token, unauthorized, snapshot = () => 0, canWrite = () => true}) {
        async function request(path, method = "GET", body, options = {}) {
            const version = snapshot();
            if (method !== "GET" && !path.startsWith("auth/") && !canWrite(path)) throw new APIError(0);
            const origin = new URL(base, root.location ? root.location.origin : "http://test.local"), url = new URL(path, origin);
            if (url.origin !== origin.origin || !url.pathname.startsWith(origin.pathname)) throw new APIError(400, "Đường dẫn API không hợp lệ.");
            let response;
            try { response = await fetcher(url.href, {method, signal: options.signal, headers: {"Content-Type": "application/json", ...(token() ? {Authorization: `Token ${token()}`} : {})}, ...(body === undefined ? {} : {body: JSON.stringify(body)})}); }
            catch (error) { if (error.name === "AbortError") throw error; throw new APIError(0, "Mất kết nối máy chủ. Kiểm tra mạng và thử lại.", undefined, path); }
            const data = response.status === 204 ? null : await response.json().catch(() => ({detail: "Phản hồi máy chủ không hợp lệ."}));
            if (!response.ok) { if (response.status === 401 && path !== "auth/login/") unauthorized(); throw new APIError(response.status, data, Number(response.headers.get("Retry-After")) || 15, path); }
            if (method === "GET" && version !== snapshot()) throw Object.assign(new Error("Obsolete snapshot"), {name: "ObsoleteSnapshot"});
            return data;
        }
        async function all(path) { const records = []; let next = path; while (next) { const data = await request(next); records.push(...(data.results || data)); next = data.next; } return records; }
        return {request, all};
    }
    function realtime({url, token, Socket, timers, now, state, event, ready, revoked}) {
        let socket, stopped = false, attempt = 0, timeout, heartbeat, lastMessage, generation = 0;
        function connect() {
            if (stopped) return;
            state(attempt ? "Đang kết nối lại…" : "Đang kết nối…", "pending");
            const version = ++generation; let active;
            try {active = new Socket(url);} catch (_) {state("Mất kết nối", "offline"); timeout = timers.setTimeout(connect, Math.min(30000, 1000 * 2 ** attempt++)); return;}
            socket = active;
            let failed = false, authenticated = false, synced = false, queued = [];
            const current = () => !stopped && socket === active && generation === version;
            active.onopen = () => { if (!current() || failed) return; lastMessage = now(); active.send(JSON.stringify({type: "authenticate", token: token()})); };
            active.onmessage = message => {
                if (!current() || failed) return;
                lastMessage = now(); let data; try { data = JSON.parse(message.data); } catch (_) { return; }
                if (data.type === "ready") {
                    if (authenticated) return;
                    authenticated = true; state("Đang cập nhật…", "syncing");
                    Promise.resolve().then(ready).then(result => {
                        if (!current() || failed) return;
                        if (result === false) {failed = true; state("Dữ liệu tạm thời chưa cập nhật", "offline"); active.close(); return;}
                        synced = true; attempt = 0; state("Đã cập nhật", "online"); queued.forEach(event); queued = [];
                    }).catch(() => {if (current()) {failed = true; state("Dữ liệu tạm thời chưa cập nhật", "offline"); active.close();}});
                    timers.clearInterval(heartbeat);
                    heartbeat = timers.setInterval(() => {if (!current()) return; if (now() - lastMessage > 45000) {failed = true; state("Mất kết nối", "offline"); active.close();} else if (active.readyState === 1) active.send(JSON.stringify({type: "ping"}));}, 15000);
                } else if (authenticated && !["heartbeat", "pong"].includes(data.type)) {
                    if (synced) event(data); else if (queued.length < 100) queued.push(data); else {failed = true; state("Mất kết nối", "offline"); active.close();}
                }
            };
            active.onerror = () => {if (current()) {failed = true; state("Mất kết nối", "offline"); active.close();}};
            active.onclose = ({code}) => {
                if (!current()) return; failed = true; timers.clearInterval(heartbeat);
                if ([4400, 4401, 4403].includes(code)) { stopped = true; state("Phiên hoặc quyền đã thay đổi", "offline"); revoked(); return; }
                state("Mất kết nối", "offline"); timeout = timers.setTimeout(connect, Math.min(30000, 1000 * 2 ** attempt++));
            };
        }
        connect(); return {
            pause() {generation++; timers.clearTimeout(timeout); timers.clearInterval(heartbeat); socket?.close();},
            retry() {if (stopped) return; generation++; timers.clearTimeout(timeout); timers.clearInterval(heartbeat); socket?.close(); connect();},
            stop() {stopped = true; generation++; timers.clearTimeout(timeout); timers.clearInterval(heartbeat); socket?.close();},
        };
    }
    function distance(a, b) {
        const rad = n => n * Math.PI / 180, dl = rad(b.latitude - a.latitude), dn = rad(b.longitude - a.longitude);
        const d = Math.sin(dl / 2) ** 2 + Math.cos(rad(a.latitude)) * Math.cos(rad(b.latitude)) * Math.sin(dn / 2) ** 2;
        return 6371000 * 2 * Math.atan2(Math.sqrt(d), Math.sqrt(Math.max(0, 1 - d)));
    }
    function freshPosition(position, now = Date.now()) {
        const timestamp = Date.parse(position?.timestamp);
        return Number.isFinite(timestamp) && now >= timestamp && now - timestamp <= 60000;
    }
    function gps({geo, timers, now, send, state, intervalMs = 10000}) {
        let watch = null, timer, latest, sent, sending = false, retryAt = 0, generation = 0, expired = false;
        async function tick() {
            if (watch === null || sending || !latest || now() < retryAt) return;
            if (!freshPosition(latest, now())) {if (!expired) state("Vị trí chưa cập nhật", "offline"); expired = true; return;}
            if (sent && distance(sent, latest) < 10 && now() - sent.sentAt < 30000) return;
            sending = true; const current = latest, activeGeneration = generation;
            try { await send(current); if (generation === activeGeneration) { sent = {...current, sentAt: now()}; state("Đang chia sẻ vị trí", "online"); } }
            catch (error) {
                if (generation !== activeGeneration) return;
                if ([401, 403].includes(error.status)) { stop(); state("Chưa thể chia sẻ vị trí", "offline"); }
                else { retryAt = now() + (error.status === 429 ? error.retryAfter * 1000 : 10000); state("Vị trí chưa được cập nhật", "offline"); }
            } finally { sending = false; }
        }
        function stop() { generation++; if (watch !== null) geo.clearWatch(watch); watch = null; latest = null; sent = null; timers.clearInterval(timer); state("Đã dừng chia sẻ vị trí", "idle"); }
        function start() {
            if (!geo) { state("Thiết bị chưa hỗ trợ lấy vị trí", "offline"); return; } if (watch !== null) return;
            generation++; retryAt = 0; state("Đang lấy vị trí…", "pending");
            watch = geo.watchPosition(position => {expired = false; latest = {latitude: position.coords.latitude, longitude: position.coords.longitude, accuracy: position.coords.accuracy, timestamp: new Date(position.timestamp).toISOString()}; }, error => { if (error.code === 1) stop(); state(error.code === 1 ? "Chưa cho phép chia sẻ vị trí" : "Chưa lấy được vị trí. Hãy thử lại.", "offline"); }, {enableHighAccuracy: true, maximumAge: 5000, timeout: 15000});
            timer = timers.setInterval(tick, intervalMs);
        }
        return {start, stop, tick};
    }
    function normalizePhone(value) {
        const phone = String(value || "").replace(/[ .()-]/g, "");
        if (phone && !/^\+?[0-9]{8,15}$/.test(phone)) throw new Error("Số điện thoại cần 8–15 chữ số, có thể bắt đầu bằng +.");
        return phone;
    }
    function validPoint(point) {
        return !!point && Number.isFinite(point.latitude) && Number.isFinite(point.longitude) && Math.abs(point.latitude) <= 90 && Math.abs(point.longitude) <= 180;
    }
    function geocoding({api, timers, Controller = root.AbortController, delayMs = 450}) {
        const channels = {};
        const aborted = () => Object.assign(new Error("Cancelled lookup"), {name: "AbortError"});
        function cancel(kind) {const old = channels[kind]; if (old) {timers.clearTimeout(old.timer); old.controller?.abort(); old.reject(aborted()); delete channels[kind];}}
        function lookup(kind, body) {
            cancel(kind);
            return new Promise((resolve, reject) => {
                const job = {reject, controller: Controller ? new Controller() : null}; channels[kind] = job;
                job.timer = timers.setTimeout(async () => {
                    if (channels[kind] !== job) return;
                    try {
                        const data = await api.request(`geocoding/${kind}/`, "POST", body, {signal: job.controller?.signal});
                        if (channels[kind] !== job) return;
                        delete channels[kind]; resolve(data);
                    } catch (error) {if (channels[kind] === job) {delete channels[kind]; reject(error);}}
                }, delayMs);
            });
        }
        return {search: query => lookup("search", {query}), reverse: point => lookup("reverse", {latitude: point.latitude, longitude: point.longitude}),
            cancelSearch: () => cancel("search"), cancelReverse: () => cancel("reverse"), destroy() {cancel("search"); cancel("reverse");}};
    }
    function camera({devices, canvas, now, makeFile}) {
        let stream = null, generation = 0, recorder = null;
        function stop() {generation++; if (recorder?.state === "recording") recorder.stop(); if (stream) stream.getTracks().forEach(track => track.stop()); stream = null;}
        async function start(video) {
            stop(); const current = generation;
            if (!devices?.getUserMedia) throw new Error("Chưa mở được camera. Bạn vẫn có thể gửi báo cáo không kèm ảnh.");
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
        function record(video, position, {Recorder, timers, maxBytes, maxSeconds}) {
            if (!stream || !Recorder) throw new Error("Thiết bị chưa hỗ trợ quay video.");
            const type = ["video/webm", "video/mp4"].find(value => Recorder.isTypeSupported(value));
            if (!type) throw new Error("Thiết bị chưa hỗ trợ định dạng video này.");
            const current = generation, captured = now(), chunks = []; let bytes = 0;
            recorder = new Recorder(stream, {mimeType: type, videoBitsPerSecond: 1500000});
            const active = recorder;
            return new Promise((resolve, reject) => {
                let timer;
                active.ondataavailable = event => {if (event.data.size) {chunks.push(event.data); bytes += event.data.size; if (bytes > maxBytes && active.state === "recording") active.stop();}};
                active.onerror = () => {timers.clearTimeout(timer); stop(); reject(new Error("Không quay được video. Hãy thử chụp ảnh."));};
                active.onstop = () => {
                    timers.clearTimeout(timer); if (recorder === active) recorder = null;
                    if (generation !== current) {resolve(null); return;}
                    const blob = new Blob(chunks, {type}); stop(); video.srcObject = null;
                    if (!blob.size || blob.size > maxBytes) {reject(new Error("Video rỗng hoặc vượt giới hạn kích thước.")); return;}
                    const metadata = {capture_source: "camera", captured_at: new Date(captured).toISOString()};
                    if (validPoint(position) && captured - Date.parse(position.timestamp) >= 0 && captured - Date.parse(position.timestamp) <= 60000) Object.assign(metadata, {capture_latitude: position.latitude, capture_longitude: position.longitude, capture_accuracy: position.accuracy});
                    resolve({file: makeFile(blob, `scene-${captured}.${type === "video/mp4" ? "mp4" : "webm"}`), metadata});
                };
                active.start(1000); timer = timers.setTimeout(() => {if (active.state === "recording") active.stop();}, maxSeconds * 1000);
            });
        }
        function finishRecording() {if (recorder?.state === "recording") recorder.stop();}
        return {start, stop, capture, record, finishRecording};
    }
    async function upload(api, target, file, {maxBytes, crypto, fetcher, metadata = {}, intent}) {
        if (!file.size || file.size > maxBytes) throw new Error(`File phải từ 1 byte đến ${Math.round(maxBytes / 1048576)} MB.`);
        if (!crypto || !crypto.subtle) throw new Error("Chưa thể gửi tệp trên kết nối này. Hãy dùng kết nối an toàn.");
        const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", await file.arrayBuffer()));
        const checksum = root.btoa(String.fromCharCode(...digest));
        const result = await api.request("media/presign/", "POST", {...target, ...metadata, filename: file.name, content_type: file.type, size_bytes: file.size, checksum_sha256: checksum});
        intent?.(result.media.id);
        let response; try { response = await fetcher(result.upload.url, {method: result.upload.method, headers: result.upload.headers, body: file}); }
        catch (_) { throw new Error("Không gửi được tệp. Kiểm tra mạng và thử lại."); }
        if (!response.ok) throw new Error("Không gửi được tệp. Hãy thử lại.");
        return api.request(`media/${result.media.id}/confirm/`, "POST", {});
    }
    const exports = {homes, homeURL, allowed, describe, publicError, friendlyTime, freshPosition, APIError, apiClient, realtime, gps, distance, upload, missionAction, sheetSnap, camera, normalizePhone, validPoint, geocoding};
    if (typeof module !== "undefined") module.exports = exports;
    root.Emergency = exports;
})(typeof window === "undefined" ? globalThis : window);
