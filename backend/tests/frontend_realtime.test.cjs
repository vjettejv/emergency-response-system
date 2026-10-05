const {test} = require("node:test");
const assert = require("node:assert/strict");
const {readFileSync} = require("node:fs");
const {join} = require("node:path");
const vm = require("node:vm");
const {webcrypto} = require("node:crypto");
const E = require("../realtime/frontend/client.js");

function timers() {
    let id = 0; const intervals = new Map(), timeouts = new Map();
    return {intervals, timeouts, setInterval(fn, ms) {intervals.set(++id, {fn, ms}); return id;}, clearInterval(id) {intervals.delete(id);}, setTimeout(fn, ms) {timeouts.set(++id, {fn, ms}); return id;}, clearTimeout(id) {timeouts.delete(id);}};
}
function response(status, data, retry = "15") {return {ok: status >= 200 && status < 300, status, headers: {get: () => retry}, json: async () => data};}

test("role route guards cover all workspaces and allow Admin dispatch", () => {
    for (const role of Object.keys(E.homes)) for (const [owner, route] of Object.entries(E.homes)) {
        assert.equal(E.allowed(role, route), role === owner || (role === "admin" && owner === "dispatcher"));
    }
    assert.equal(E.allowed("citizen", "/dispatcher/incidents/5"), false);
    assert.equal(E.allowed("dispatcher", "/dispatcher/unknown/5"), false);
    assert.equal(E.allowed("rescue_team", "/rescue-other"), false);
});

test("phone validation accepts common spacing and rejects URI/control injection", () => {
    assert.equal(E.normalizePhone("(+84) 900 000 000"), "+84900000000");
    assert.equal(E.normalizePhone(""), "");
    for (const phone of ["tel:+84900000000", "abc", "123", "+12345678\n", "javascript:12345678", "+" + "9".repeat(16)]) assert.throws(() => E.normalizePhone(phone));
});

test("camera captures bounded JPEG with truthful device metadata and releases tracks", async () => {
    let stopped = 0, constraints, drawing, clock = 1700000000000;
    const stream = {getTracks: () => [{stop() {stopped++;}}]}, canvas = {getContext: () => ({drawImage(...args) {drawing = args;}}), toBlob(fn, type) {fn(new Blob(["synthetic"], {type}));}};
    const video = {videoWidth: 3200, videoHeight: 2400, play: async () => {}};
    const cam = E.camera({devices: {getUserMedia: async value => {constraints = value; return stream;}}, canvas: () => canvas, now: () => clock, makeFile: (blob, name) => ({blob, name})});
    await cam.start(video);
    assert.equal(constraints.audio, false); assert.equal(constraints.video.facingMode.ideal, "environment");
    const result = await cam.capture(video, {latitude: 21, longitude: 105, accuracy: 250, timestamp: new Date(clock - 1000).toISOString()});
    assert.equal(canvas.width, 1600); assert.equal(canvas.height, 1200); assert.equal(drawing[0], video);
    assert.equal(result.file.blob.type, "image/jpeg"); assert.equal(result.metadata.capture_source, "camera");
    assert.equal(result.metadata.capture_longitude, 105); assert.equal(result.metadata.capture_accuracy, 250);
    assert.equal(stopped, 1); assert.equal(video.srcObject, null);
    await cam.start(video);
    const stale = await cam.capture(video, {latitude: 21, longitude: 105, accuracy: 5, timestamp: new Date(clock - 61000).toISOString()});
    assert.equal(stale.metadata.capture_longitude, undefined); assert.equal(stopped, 2);
    await assert.rejects(cam.capture(video), /chưa sẵn sàng/);
});

test("camera denial and navigation during permission request never leak an active stream", async () => {
    let resolve, stopped = 0;
    const cam = E.camera({devices: {getUserMedia: () => new Promise(done => {resolve = done;})}, canvas() {}, now: Date.now, makeFile() {}});
    const opening = cam.start({play: async () => {}}); cam.stop();
    resolve({getTracks: () => [{stop() {stopped++;}}]}); assert.equal(await opening, false); assert.equal(stopped, 1);
    const denied = E.camera({devices: {getUserMedia: async () => {throw new Error("Permission denied");}}});
    await assert.rejects(denied.start({}), /Permission denied/); denied.stop();
    await assert.rejects(E.camera({}).start({}), /HTTPS/);
});
test("API centralizes token, JSON errors, revocation and network errors", async () => {
    let revoked = 0, status = 200, called;
    const api = E.apiClient({base: "/api/v1/", token: () => "synthetic", unauthorized: () => revoked++, fetcher: async (url, options) => {called = {url, options}; return response(status, {detail: "Expired"});}});
    await api.request("auth/me/"); assert.equal(called.options.headers.Authorization, "Token synthetic");
    status = 401; await assert.rejects(api.request("incidents/"), error => error.status === 401 && error.message === "Expired"); assert.equal(revoked, 1);
    const disconnected = E.apiClient({base: "/api/v1/", token: () => "", unauthorized() {}, fetcher: async () => {throw new Error("offline");}});
    await assert.rejects(disconnected.request("incidents/"), error => error.status === 0);
});
test("pagination follows trusted same-origin links and never leaks tokens to foreign next links", async () => {
    let calls = 0;
    const api = E.apiClient({base: "/api/v1/", token: () => "synthetic", unauthorized() {}, fetcher: async () => response(200, {results: [++calls], next: calls === 1 ? "http://test.local/api/v1/items/?page=2" : null})});
    assert.deepEqual(await api.all("items/"), [1, 2]);
    await assert.rejects(api.request("https://foreign.example/api/v1/"), error => error.status === 400); assert.equal(calls, 2);
});
test("WebSocket authenticates in first frame, resyncs, retries with bounded backoff and cleans timers", () => {
    const t = timers(), sockets = []; let ready = 0, revoked = 0, now = 100;
    class Socket {constructor(url) {this.url = url; this.readyState = 1; this.sent = []; sockets.push(this);} send(data) {this.sent.push(JSON.parse(data));} close() {this.onclose({code: 1006});}}
    const client = E.realtime({url: "ws://test.local/ws/rescue/", token: () => "secret", Socket, timers: t, now: () => now, state() {}, event() {}, ready: () => ready++, revoked: () => revoked++});
    sockets[0].onopen(); assert.equal(sockets[0].sent[0].type, "authenticate"); assert.ok(!sockets[0].url.includes("secret"));
    sockets[0].onmessage({data: '{"type":"ready"}'}); assert.equal(ready, 1);
    sockets[0].onclose({code: 1006}); assert.equal([...t.timeouts.values()][0].ms, 1000);
    const timer = [...t.timeouts.entries()][0]; t.timeouts.delete(timer[0]); timer[1].fn();
    sockets[1].onopen(); sockets[1].onmessage({data: '{"type":"ready"}'}); assert.equal(ready, 2);
    sockets[1].onclose({code: 4403}); assert.equal(revoked, 1); assert.equal(t.timeouts.size, 0);
    client.stop(); sockets[1].onmessage({data: '{"type":"ready"}'}); assert.equal(ready, 2); assert.equal(t.intervals.size, 0);
});
test("WebSocket heartbeat closes a silent socket, ignores malformed JSON, delivers domain events", () => {
    const t = timers(); let socket, clock = 0, events = 0;
    class Socket {constructor() {socket = this; this.readyState = 1;} send() {} close() {this.onclose({code: 1006});}}
    const client = E.realtime({url: "ws://test", token: () => "synthetic", Socket, timers: t, now: () => clock, state() {}, ready() {}, revoked() {}, event: () => events++});
    socket.onopen(); socket.onmessage({data: "broken"}); socket.onmessage({data: '{"type":"ready"}'});
    socket.onmessage({data: '{"type":"incident.status_changed","data":{}}'}); assert.equal(events, 1);
    clock = 46000; [...t.intervals.values()][0].fn(); assert.equal(t.timeouts.size, 1); client.stop();
});
test("GPS timer suppresses small movement, refreshes stationary samples, respects Retry-After and stops watch", async () => {
    const t = timers(); let clock = 100000, sample, calls = [], fail = false, cleared = false;
    const geo = {watchPosition(callback) {sample = callback; return 7;}, clearWatch(id) {assert.equal(id, 7); cleared = true;}};
    const gps = E.gps({geo, timers: t, now: () => clock, state() {}, send: async body => {calls.push(body); if (fail) throw new E.APIError(429, "Wait", 15);}});
    const position = (lat = 21) => sample({coords: {latitude: lat, longitude: 105, accuracy: 5}, timestamp: clock});
    gps.start(); position(); assert.equal(calls.length, 0); await gps.tick();
    clock += 10000; position(21.000001); await gps.tick(); assert.equal(calls.length, 1);
    clock += 21000; position(); fail = true; await gps.tick(); assert.equal(calls.length, 2);
    clock += 10000; position(); await gps.tick(); assert.equal(calls.length, 2);
    clock += 6000; position(); fail = false; await gps.tick(); assert.equal(calls.length, 3);
    assert.equal(calls[0].longitude, 105); gps.stop(); assert.ok(cleared); assert.equal(t.intervals.size, 0);
});
test("GPS drops stale samples and stops on lost authorization", async () => {
    const t = timers(); let callback, clock = 100000, calls = 0, cleared = false;
    const gps = E.gps({geo: {watchPosition(fn) {callback = fn; return 1;}, clearWatch() {cleared = true;}}, timers: t, now: () => clock, state() {}, send: async () => {calls++; throw new E.APIError(403, "Denied");}});
    gps.start(); callback({coords: {latitude: 21, longitude: 105, accuracy: 5}, timestamp: 0}); await gps.tick(); assert.equal(calls, 0);
    callback({coords: {latitude: 21, longitude: 105, accuracy: 5}, timestamp: clock}); await gps.tick(); assert.ok(cleared); assert.equal(calls, 1);
});
test("stopping GPS during an in-flight request prevents late success state and clears timers", async () => {
    const t = timers(); let callback, finish; const states = [];
    const pending = new Promise(resolve => {finish = resolve;});
    const gps = E.gps({geo: {watchPosition(fn) {callback = fn; return 1;}, clearWatch() {}}, timers: t, now: () => 100000, state: text => states.push(text), send: () => pending});
    gps.start(); callback({coords: {latitude: 21, longitude: 105, accuracy: 5}, timestamp: 100000});
    const request = gps.tick(); gps.stop(); finish(); await request;
    assert.equal(states.at(-1), "GPS đã dừng"); assert.equal(t.intervals.size, 0);
});
test("private upload hashes file, PUTs directly to S3 without app token, confirms only after success", async () => {
    const calls = [], file = {name: "synthetic.png", type: "image/png", size: 3, arrayBuffer: async () => new Uint8Array([1, 2, 3]).buffer};
    const api = {request: async (path, method, body) => {calls.push({path, method, body}); return path.includes("presign") ? {media: {id: "uuid"}, upload: {method: "PUT", url: "https://s3.example/private", headers: {"Content-Type": file.type}}} : {status: "ready"};}};
    let s3;
    const result = await E.upload(api, {report_id: 5}, file, {maxBytes: 100, crypto: webcrypto, fetcher: async (url, options) => {s3 = {url, options}; return response(200, {});}});
    assert.equal(result.status, "ready"); assert.equal(calls[0].body.checksum_sha256.length, 44); assert.equal(calls[1].path, "media/uuid/confirm/"); assert.equal(s3.options.headers.Authorization, undefined); assert.equal(s3.options.body, file);
    await assert.rejects(E.upload(api, {report_id: 5}, {...file, size: 101}, {maxBytes: 100, crypto: webcrypto}), /File phải/);
    calls.length = 0; await assert.rejects(E.upload(api, {report_id: 5}, file, {maxBytes: 100, crypto: webcrypto, fetcher: async () => response(403, {})}), /S3 từ chối/); assert.equal(calls.length, 1);
});
test("Leaflet reuses markers, ignores older GPS, removes filtered markers and destroys map", () => {
    global.document = {createElement: () => ({append() {}, textContent: ""})};
    const {createMap} = require("../realtime/frontend/map.js"); let made = 0, moves = 0, removed = 0, destroyed = 0;
    const map = {setView() {return this;}, fitBounds() {}, invalidateSize() {}, removeLayer() {removed++;}, remove() {destroyed++;}};
    const library = {map: () => map, tileLayer: () => ({addTo() {}}), divIcon: value => value, latLngBounds: values => values, marker: () => {made++; return {addTo() {return this;}, setLatLng() {moves++; return this;}, setIcon() {return this;}, bindPopup() {}, getLatLng() {return [21, 105];}};}};
    const controller = createMap({}, library);
    controller.update("team", {team_id: 1, latitude: 21, longitude: 105, timestamp: "2026-10-03T10:00:00Z"}, "Synthetic");
    controller.update("team", {team_id: 1, latitude: 20, longitude: 104, timestamp: "2026-10-03T09:00:00Z"}, "Synthetic");
    controller.update("team", {team_id: 1, latitude: 22, longitude: 106, timestamp: "2026-10-03T11:00:00Z"}, "Synthetic");
    assert.equal(made, 1); assert.equal(moves, 1); controller.replace("team", [], () => ""); assert.equal(removed, 1); controller.destroy(); assert.equal(destroyed, 1);
});

// Render the actual shell with a minimal DOM. These tests cover data loading and role routes;
// real Leaflet, layout and user gestures are additionally checked in the browser.
function screenHarness(role, route, failure = false, delayDashboard = false, assignmentStatus = null) {
    const roots = [], handlers = {}, calls = [], requests = [], sockets = [], mapUpdates = []; let maps = 0;
    let releaseDashboard;
    const dashboardGate = new Promise(resolve => {releaseDashboard = resolve;});
    class Element {
        constructor(tag) {this.tag = tag; this.nodeType = 1; this.children = []; this.attrs = {}; this.value = ""; this.name = ""; this.id = ""; this.textContent = ""; this.className = ""; this.dataset = {};}
        append(...nodes) {nodes.forEach(node => {node.parentElement = this; this.children.push(node);});}
        replaceChildren(...nodes) {this.children = []; this.append(...nodes);}
        addEventListener(type, fn) {this["on" + type] = fn;}
        setAttribute(key, value) {this.attrs[key] = value;}
        removeAttribute(key) {delete this.attrs[key];}
        click() {return this.onclick?.({currentTarget: this, preventDefault() {}});}
        querySelector(selector) {return this.children.find(node => node.tag === selector) || this.children.map(node => node.querySelector?.(selector)).find(Boolean);}
        remove() {if (this.parentElement) this.parentElement.children = this.parentElement.children.filter(child => child !== this);}
        get text() {return this.textContent + this.children.map(child => child.text || "").join(" ");}
        get isConnected() {return this === app || !!this.parentElement?.isConnected;}
    }
    const app = new Element("div"); app.id = "app"; roots.push(app);
    const document = {querySelector: () => new Element("a"), createElement: tag => new Element(tag), createTextNode: text => Object.assign(new Element("text"), {textContent: text}), getElementById(id) {const search = element => element.id === id ? element : element.children.map(search).find(Boolean); return roots.map(search).find(Boolean) || new Element("div");}};
    let hash = "#" + route;
    const location = {origin: "http://test.local", protocol: "http:", get hash() {return hash;}, set hash(value) {hash = value.startsWith("#") ? value : "#" + value;}};
    const user = {username: "synthetic", role, response_team: 7};
    const context = vm.createContext({document, location, URL, URLSearchParams, Event, Date, console, navigator: {}, crypto: webcrypto,
        sessionStorage: {getItem: () => "synthetic-token", setItem() {}, removeItem() {}},
        FormData: class {constructor(form) {this.entries = []; const visit = element => {if (element.name) this.entries.push([element.name, element.value]); element.children.forEach(visit);}; visit(form);} [Symbol.iterator]() {return this.entries[Symbol.iterator]();}},
        fetch: async (url, options = {}) => {
            calls.push(url);
            requests.push({url, options});
            if (url.includes("s3.synthetic.invalid")) return response(200, {});
            if (url.endsWith("incident-reports/drafts/")) return response(201, {id: 5, is_draft: true});
            if (url.endsWith("media/presign/")) return response(201, {media: {id: "synthetic-media"}, upload: {url: "https://s3.synthetic.invalid/scene", method: "PUT", headers: {}}});
            if (url.endsWith("/confirm/")) return response(200, {id: "synthetic-media", status: "ready"});
            if (url.endsWith("incident-reports/5/submit/")) return response(200, {id: 5, is_draft: false});
            if (url.endsWith("config.json")) return response(200, {apiBase: "/api/v1/", dispatcherSocket: "/ws/dispatcher/", rescueSocket: "/ws/rescue/", mediaMaxBytes: 100});
            if (url.includes("auth/me")) return response(200, user);
            if (url.includes("incident-categories")) return response(200, {results: [{id: 1, name: "Synthetic"}], next: null});
            if (failure) return response(503, {detail: "Synthetic service unavailable"});
            if (delayDashboard && url.endsWith("incidents/?")) await dashboardGate;
            if (url.includes("configuration")) return response(200, {values: {CLUSTER_RADIUS_METERS: 300}});
            if (url.endsWith("/contacts/")) return response(200, [{report_id: 5, reporter_name: "Synthetic reporter", reporter_phone: "+84900000000", allow_contact: true, can_call: true, phone_masked: false, latitude: 21, longitude: 105}]);
            if (url.endsWith("/contact/")) return response(200, {report_id: 5, reporter_name: "Synthetic reporter", reporter_phone: "•••000", allow_contact: false, can_call: false, phone_masked: true});
            if (url.endsWith("/timeline/")) return response(200, {milestones: {}, assignments: []});
            if (url.endsWith("incident-reports/5/")) return response(200, {id: 5, category: 1, review_status: "pending", description: "Synthetic <script>"});
            if (url.includes("potential-duplicates")) return response(200, {reports: [], incidents: []});
            if (url.endsWith("incidents/5/")) return response(200, {id: 5, category: 1, status: "resolved", title: "Synthetic incident", description: "Synthetic"});
            if (assignmentStatus && /assignments\/(?:5\/)?$/.test(url)) {
                const task = {id: 5, team: 7, status: assignmentStatus, incident: {id: 5, title: "Synthetic mission", category: 1, status: "dispatched", latitude: 21, longitude: 105, address: "Synthetic site"}};
                return response(200, url.endsWith("assignments/5/") ? task : {count: 1, results: [task], next: null});
            }
            return response(200, {count: 0, results: [], next: null});
        },
        WebSocket: class {constructor() {this.readyState = 1; sockets.push(this);} send() {} close() {}}, setInterval() {}, clearInterval() {}, setTimeout() {}, clearTimeout() {}, addEventListener(type, fn) {handlers[type] = fn;}, removeEventListener(type) {delete handlers[type];},
        EmergencyMap: {createMap: () => {maps++; return {replace() {}, update(kind, item) {mapUpdates.push({kind, item});}, focus() {}, destroy() {}};}},
    }); context.window = context; context.Emergency = E;
    return {app, context, calls, requests, sockets, mapUpdates, get maps() {return maps;}, setTaskStatus(status) {assignmentStatus = status;}, releaseDashboard, async navigate(path) {location.hash = path; await handlers.hashchange();}, async run() {vm.runInContext(readFileSync(join(__dirname, "../realtime/frontend/app.js"), "utf8"), context); for (let i = 0; i < 12; i++) await new Promise(resolve => setImmediate(resolve)); if (location.hash !== "#" + route && handlers.hashchange) {handlers.hashchange(); for (let i = 0; i < 12; i++) await new Promise(resolve => setImmediate(resolve));}}};
}
test("actual role screens render report form, dashboard, rescue list and Admin data", async () => {
    for (const [role, route, title] of [["citizen", "/citizen/report", "Báo cáo sự cố"], ["dispatcher", "/dispatcher", "Trung tâm điều phối"], ["rescue_team", "/rescue", "Nhiệm vụ được giao"], ["admin", "/admin/users", "Tài khoản & phân quyền"]]) {
        const h = screenHarness(role, route); await h.run(); assert.match(h.app.text, new RegExp(title)); assert.ok(!h.app.text.includes("Đang tải dữ liệu"));
        if (role !== "citizen") assert.ok(h.calls.some(url => url.includes(role === "rescue_team" ? "assignments/" : role === "admin" ? "admin/users/" : "incidents/")));
    }
});

test("Citizen automatically requests device location, shows accuracy, and offers camera only", async () => {
    const h = screenHarness("citizen", "/citizen/report"); let requests = 0;
    h.context.navigator.geolocation = {getCurrentPosition(success) {requests++; success({coords: {latitude: 21, longitude: 105, accuracy: 250}, timestamp: Date.now()});}};
    await h.run(); assert.equal(requests, 1); assert.match(h.app.text, /Đã xác định vị trí/); assert.match(h.app.text, /±250 m/); assert.match(h.app.text, /Độ chính xác vị trí hiện tại thấp/); assert.match(h.app.text, /CHỤP ẢNH HIỆN TRƯỜNG/);
    const files = element => (element.attrs.type === "file" || element.type === "file") || element.children.some(files);
    assert.equal(files(h.app), false); assert.match(h.app.text, /Tên người báo tin/);
    const denied = screenHarness("citizen", "/citizen/report"); denied.context.navigator.geolocation = {getCurrentPosition(_, failure) {failure({code: 1});}};
    await denied.run(); assert.match(denied.app.text, /Chạm bản đồ \/ nhập tọa độ/); assert.match(denied.app.text, /Gửi báo cáo sự cố/);
});

test("a late automatic GPS response does not overwrite a manually selected incident position", async () => {
    const h = screenHarness("citizen", "/citizen/report"); let deliver;
    h.context.navigator.geolocation = {getCurrentPosition(success) {deliver = success;}};
    await h.run();
    const find = (element, name) => element.name === name ? element : element.children.map(child => find(child, name)).find(Boolean);
    const lat = find(h.app, "latitude"), lon = find(h.app, "longitude"); lat.value = "10.79"; lon.value = "106.71";
    lat.parentElement.oninput();
    deliver({coords: {latitude: 21, longitude: 105, accuracy: 5}, timestamp: Date.now()});
    for (let i = 0; i < 4; i++) await new Promise(resolve => setImmediate(resolve));
    assert.equal(lat.value, "10.79"); assert.equal(lon.value, "106.71"); assert.match(h.app.text, /Vị trí hiện trường chọn thủ công/);
});

test("Citizen camera form uploads a private draft before publishing the report", async () => {
    const h = screenHarness("citizen", "/citizen/report"), file = new File(["abc"], "scene-unit.jpg", {type: "image/jpeg"}), captured = {file, metadata: {capture_source: "camera", captured_at: new Date().toISOString()}};
    h.context.Emergency = {...E, camera: () => ({start: async () => true, stop() {}, capture: async () => captured})};
    await h.run();
    const find = (element, predicate) => predicate(element) ? element : element.children.map(child => find(child, predicate)).find(Boolean);
    const control = text => find(h.app, element => element.tag === "button" && element.text === text);
    await control("CHỤP ẢNH HIỆN TRƯỜNG").click(); await control("Chụp ảnh").click(); await control("Sử dụng ảnh").click();
    for (const [name, value] of Object.entries({description: "Synthetic camera report", latitude: "21", longitude: "105", reporter_name: "Synthetic", reporter_phone: "+12025550123"})) find(h.app, element => element.name === name).value = value;
    const form = find(h.app, element => element.tag === "form"); form.onsubmit({preventDefault() {}});
    for (let i = 0; i < 20; i++) await new Promise(resolve => setImmediate(resolve));
    const writes = h.requests.filter(request => request.options.method && request.options.method !== "GET");
    assert.deepEqual(writes.map(request => new URL(request.url).pathname), ["/api/v1/incident-reports/drafts/", "/api/v1/media/presign/", "/scene", "/api/v1/media/synthetic-media/confirm/", "/api/v1/incident-reports/5/submit/"]);
    const upload = JSON.parse(writes[1].options.body); assert.equal(upload.capture_source, "camera"); assert.equal(upload.report_id, 5);
    assert.equal(writes[2].options.headers.Authorization, undefined); assert.equal(h.context.location.hash, "#/citizen/reports/5");
});

test("authorized mission shows shared contact and quick actions; no-consent report has no call", async () => {
    const mission = screenHarness("rescue_team", "/rescue/assignments/5", false, false, "responding"); await mission.run();
    assert.match(mission.app.text, /Synthetic reporter/); assert.match(mission.app.text, /Gọi người báo tin/);
    for (const label of ["Báo vấn đề", "YÊU CẦU THÊM LỰC LƯỢNG", "Xem report gốc", "Media", "Hoàn thành nhiệm vụ"]) assert.ok(mission.app.text.includes(label), mission.app.text);
    const report = screenHarness("dispatcher", "/dispatcher/reports/5"); await report.run();
    assert.match(report.app.text, /Không cho phép gọi trực tiếp/); assert.ok(!report.app.text.includes("Gọi người báo tin"));
    const completed = screenHarness("rescue_team", "/rescue/assignments/5", false, false, "completed"); await completed.run();
    assert.ok(!completed.calls.some(url => url.endsWith("/contacts/"))); assert.ok(!completed.app.text.includes("+84900000000"));
});
test("screen displays unified API errors and Citizen guard never loads privileged data", async () => {
    const failed = screenHarness("dispatcher", "/dispatcher", true); await failed.run(); assert.match(failed.app.text, /Synthetic service unavailable/);
    const guarded = screenHarness("citizen", "/admin/users"); await guarded.run(); assert.match(guarded.app.text, /Báo cáo sự cố/); assert.ok(!guarded.calls.some(url => url.includes("admin/users/")));
});
test("detail screens finish loading media and show backend text safely", async () => {
    const report = screenHarness("dispatcher", "/dispatcher/reports/5"); await report.run();
    assert.match(report.app.text, /Media hiện trường/); assert.match(report.app.text, /Synthetic <script>/);
    assert.ok(report.calls.some(url => url.includes("media/?report_id=5")));
    const incident = screenHarness("dispatcher", "/dispatcher/incidents/5"); await incident.run();
    assert.match(incident.app.text, /Media hiện trường/); assert.match(incident.app.text, /Đã giải quyết/);
    assert.ok(incident.calls.some(url => url.includes("media/?incident_id=5")));
});
test("navigation during an in-flight snapshot loads new detail media and ignores the old response", async () => {
    const h = screenHarness("dispatcher", "/dispatcher", false, true); await h.run();
    await h.navigate("/dispatcher/reports/5"); assert.match(h.app.text, /Media hiện trường/);
    h.releaseDashboard(); for (let i = 0; i < 12; i++) await new Promise(resolve => setImmediate(resolve));
    assert.match(h.app.text, /Báo cáo #5/); assert.ok(!h.app.text.includes("Sự cố theo trạng thái"));
});
test("mission screens expose next backend command including responding and terminal summary", async () => {
    for (const [status, label] of [["assigned", "Nhận nhiệm vụ"], ["accepted", "Bắt đầu di chuyển"], ["en_route", "Đã đến hiện trường"], ["on_scene", "Bắt đầu xử lý"], ["responding", "Hoàn thành nhiệm vụ"], ["completed", "Nhiệm vụ đã hoàn thành"]]) {
        const h = screenHarness("rescue_team", "/rescue/assignments/5", false, false, status); await h.run();
        assert.ok(h.app.text.includes(label), h.app.text); assert.equal(h.maps, 1);
        assert.ok(!h.app.text.includes("Trạng thái cần cập nhật"));
    }
    assert.equal(E.missionAction("responding").status, "completed"); assert.equal(E.missionAction("completed"), null);
});
test("bottom sheet supports dragging, cancellation and keyboard expansion with bounded snap points", async () => {
    const h = screenHarness("rescue_team", "/rescue"); await h.run();
    const find = (element, predicate) => predicate(element) ? element : element.children.map(child => find(child, predicate)).find(Boolean);
    const sheet = find(h.app, node => node.className === "mission-sheet"), handle = sheet.children[0];
    // The minimal DOM keeps data-* in attrs; a browser exposes it as dataset.
    sheet.dataset.snap = "half";
    handle.onpointerdown({clientY: 300, pointerId: 1}); handle.onpointermove({clientY: 200}); handle.onpointerup({clientY: 200, preventDefault() {}});
    assert.equal(sheet.dataset.snap, "expanded"); handle.onclick({}); assert.equal(sheet.dataset.snap, "expanded");
    handle.onclick({}); assert.equal(sheet.dataset.snap, "half");
    handle.onpointerdown({clientY: 300}); handle.onpointercancel(); handle.onpointerup({clientY: 400}); assert.equal(sheet.dataset.snap, "half");
    assert.equal(E.sheetSnap("expanded", -100), "expanded"); assert.equal(E.sheetSnap("peek", 100), "peek");
});
test("drawer reuses the dashboard map, and WebSocket updates the current mission without navigation", async () => {
    const drawer = screenHarness("dispatcher", "/dispatcher/incidents/5"); await drawer.run(); assert.equal(drawer.maps, 1); assert.match(drawer.app.text, /HIỆN TRƯỜNG \/ CHI TIẾT/);
    await drawer.navigate("/dispatcher"); assert.equal(drawer.maps, 2);
    const h = screenHarness("rescue_team", "/rescue", false, false, "assigned"); await h.run(); assert.match(h.app.text, /Nhận nhiệm vụ/);
    h.setTaskStatus("accepted"); h.sockets[0].onmessage({data: JSON.stringify({type: "assignment.status_changed", data: {assignment_id: 5, status: "accepted"}})});
    for (let i = 0; i < 12; i++) await new Promise(resolve => setImmediate(resolve));
    assert.match(h.app.text, /Bắt đầu di chuyển/); assert.equal(h.context.location.hash, "#/rescue"); assert.equal(h.maps, 1);
    h.setTaskStatus("completed"); h.sockets[0].onmessage({data: JSON.stringify({type: "assignment.status_changed", data: {assignment_id: 5, status: "completed"}})});
    for (let i = 0; i < 12; i++) await new Promise(resolve => setImmediate(resolve));
    assert.match(h.app.text, /Nhiệm vụ đã hoàn thành/);
    const before = drawer.calls.length;
    drawer.sockets[0].onmessage({data: JSON.stringify({type: "team.location_updated", data: {team_id: 7, latitude: 21, longitude: 105}})});
    assert.equal(drawer.mapUpdates.at(-1).item.team_id, 7); assert.equal(drawer.calls.length, before);
});

test("background refresh preserves editing, but terminal events immediately remove mission contact", async () => {
    const h = screenHarness("rescue_team", "/rescue", false, false, "en_route"); await h.run();
    h.context.document.activeElement = {tagName: "INPUT", closest: () => true};
    const calls = h.calls.length;
    h.setTaskStatus("on_scene"); h.sockets[0].onmessage({data: JSON.stringify({type: "assignment.status_changed", data: {assignment_id: 5, status: "on_scene"}})});
    for (let i = 0; i < 4; i++) await new Promise(resolve => setImmediate(resolve));
    assert.equal(h.calls.length, calls); assert.match(h.app.text, /Đã đến hiện trường/);
    h.setTaskStatus("completed"); h.sockets[0].onmessage({data: JSON.stringify({type: "assignment.status_changed", data: {assignment_id: 5, status: "completed"}})});
    for (let i = 0; i < 12; i++) await new Promise(resolve => setImmediate(resolve));
    assert.match(h.app.text, /Nhiệm vụ đã hoàn thành/); assert.ok(!h.app.text.includes("+84900000000"));
});
test("GPS marker interpolation cancels superseded frames, supports layers, and stops after destroy", () => {
    const saved = {document: global.document, request: global.requestAnimationFrame, cancel: global.cancelAnimationFrame}, frames = new Map();
    let id = 0, cancelled = 0, added = 0, removed = 0, point = {lat: 21, lng: 105};
    global.document = {createElement: () => ({append() {}, textContent: ""})};
    global.requestAnimationFrame = fn => {frames.set(++id, fn); return id;}; global.cancelAnimationFrame = id => {cancelled++; frames.delete(id);};
    try {
        const map = {setView() {return this;}, fitBounds() {}, invalidateSize() {}, removeLayer() {removed++;}, remove() {}};
        const marker = {addTo() {added++; return this;}, getLatLng: () => point, setIcon() {return this;}, bindPopup() {}, setLatLng([lat, lng]) {point = {lat, lng}; return this;}};
        const L = {map: () => map, tileLayer: () => ({addTo() {}}), divIcon: value => value, marker: () => marker, latLngBounds: values => values};
        const controller = require("../realtime/frontend/map.js").createMap({}, L);
        controller.update("team", {team_id: 1, latitude: 21, longitude: 105, timestamp: "2026-10-04T10:00:00Z"}, "Demo");
        controller.update("team", {team_id: 1, latitude: 22, longitude: 105, timestamp: "2026-10-04T10:01:00Z"}, "Demo"); assert.equal(point.lat, 21);
        controller.update("team", {team_id: 1, latitude: 23, longitude: 105, timestamp: "2026-10-04T10:02:00Z"}, "Demo"); assert.equal(cancelled, 1);
        const pending = [...frames.values()][0]; pending(performance.now() + 500); assert.equal(point.lat, 23);
        controller.visibility("team", false); assert.equal(removed, 1); controller.visibility("team", true); assert.equal(added, 2);
        controller.update("team", {team_id: 1, latitude: 24, longitude: 105, timestamp: "2026-10-04T10:03:00Z"}, "Demo");
        const late = [...frames.values()].at(-1); controller.destroy(); late(performance.now() + 500); assert.equal(point.lat, 23);
    } finally {global.document = saved.document; global.requestAnimationFrame = saved.request; global.cancelAnimationFrame = saved.cancel;}
});
