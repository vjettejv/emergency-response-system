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
const flushUI = async () => {for (let i = 0; i < 12; i++) await new Promise(resolve => setImmediate(resolve));};
const findUI = (element, predicate) => predicate(element) ? element : element.children.map(child => findUI(child, predicate)).find(Boolean);

test("public errors hide backend fields, codes and infrastructure failures", () => {
    for (const status of [0, 401, 403, 404, 409, 429, 503]) {
        const error = new E.APIError(status, {detail: "S3 failed HTTP 503", assignment_id: ["invalid"]});
        assert.ok(!/S3|HTTP|503|assignment_id/.test(error.message));
        assert.equal(error.body.detail, "S3 failed HTTP 503");
    }
    assert.match(E.describe({reporter_phone: ["invalid"], category: ["invalid"]}), /Số điện thoại/);
    assert.ok(!E.describe({reporter_phone: ["invalid"]}).includes("reporter_phone"));
    assert.ok(!E.describe("Không thể chuyển en_route sang on_scene; expected_status không đúng.").includes("en_route"));
    assert.equal(E.friendlyTime("invalid"), "Chưa cập nhật");
    assert.equal(E.friendlyTime("2026-10-05T01:00:00Z", Date.parse("2026-10-05T01:05:00Z")), "5 phút trước");
});

test("login errors hide non_field_errors and preserve network and validation states", async () => {
    let status = 400, body = {non_field_errors: ["Không thể đăng nhập với thông tin đã nhập."]}, revoked = 0;
    const api = E.apiClient({base: "/api/v1/", token: () => "", unauthorized() {revoked++;}, fetcher: async () => response(status, body)});
    for (const code of [400, 401]) {
        status = code;
        await assert.rejects(api.request("auth/login/", "POST", {}), error => error.message === "Tên đăng nhập hoặc mật khẩu không đúng.");
    }
    assert.equal(revoked, 0);
    status = 400; body = {username: ["This field is required."]};
    await assert.rejects(api.request("auth/login/", "POST", {}), error => error.message === "Tên đăng nhập: Vui lòng kiểm tra lại.");
    status = 503;
    await assert.rejects(api.request("auth/login/", "POST", {}), error => error.message === "Không thể kết nối tới hệ thống. Vui lòng thử lại.");
    status = 400; body = {non_field_errors: ["Không thể thực hiện thao tác này."]};
    await assert.rejects(api.request("incidents/", "POST", {}), error => error.message === "Không thể thực hiện thao tác này.");
});

test("actual login form renders a friendly credentials error without backend field names", async () => {
    const h = screenHarness("citizen", "/login"), fetcher = h.context.fetch;
    h.context.sessionStorage.getItem = () => "";
    h.context.fetch = (url, options) => url.endsWith("auth/login/") ? Promise.resolve(response(400, {non_field_errors: ["Không thể đăng nhập với thông tin đã nhập."]})) : fetcher(url, options);
    await h.run();
    findUI(h.app, element => element.name === "username").value = "synthetic-invalid-login";
    findUI(h.app, element => element.name === "password").value = "SyntheticInvalidLogin42!";
    findUI(h.app, element => element.tag === "form").onsubmit({preventDefault() {}}); await flushUI();
    assert.match(h.app.text, /Tên đăng nhập hoặc mật khẩu không đúng\./);
    assert.ok(!h.app.text.includes("non_field_errors"));
    assert.equal(findUI(h.app, element => element.tag === "button" && element.attrs.type === "submit").disabled, false);
});

test("login password visibility is accessible and preserves the typed password", async () => {
    const h = screenHarness("citizen", "/login"); h.context.sessionStorage.getItem = () => "";
    await h.run();
    const password = findUI(h.app, element => element.name === "password");
    const toggle = findUI(h.app, element => element.className === "auth-visibility");
    password.value = "SyntheticVisibility42!";
    assert.equal(password.type, "password"); assert.equal(toggle.attrs["aria-label"], "Hiện mật khẩu");
    toggle.click(); assert.equal(password.type, "text"); assert.equal(toggle.attrs["aria-pressed"], "true");
    assert.equal(toggle.attrs["aria-label"], "Ẩn mật khẩu");
    toggle.click(); assert.equal(password.type, "password"); assert.equal(password.value, "SyntheticVisibility42!");
    assert.ok(!h.calls.some(url => url.includes("auth/login/")));
    assert.ok(!/KHÔNG GIAN LÀM VIỆC|Quên mật khẩu/.test(h.app.text));
});

test("login prevents duplicate requests and restores controls after failure", async () => {
    const h = screenHarness("citizen", "/login"), fetcher = h.context.fetch; let release, requests = 0;
    h.context.sessionStorage.getItem = () => "";
    h.context.fetch = (url, options) => url.endsWith("auth/login/") ? (requests++, new Promise(resolve => {release = resolve;})) : fetcher(url, options);
    await h.run();
    const form = findUI(h.app, element => element.tag === "form");
    const submit = findUI(h.app, element => element.className === "auth-submit");
    const spinner = findUI(h.app, element => element.className === "auth-spinner");
    const registration = findUI(h.app, element => element.className === "auth-link");
    const event = {preventDefault() {}};
    const pending = form.onsubmit(event); await form.onsubmit(event);
    assert.equal(requests, 1); assert.equal(submit.disabled, true); assert.equal(spinner.hidden, false);
    assert.equal(form.attrs["aria-busy"], "true"); assert.equal(registration.disabled, true);
    assert.equal(findUI(h.app, element => element.name === "password").readOnly, true);
    release(response(400, {non_field_errors: ["Internal credentials failure"]})); await pending;
    assert.equal(submit.disabled, false); assert.equal(spinner.hidden, true);
    assert.equal(registration.disabled, false); assert.equal(form.attrs["aria-busy"], "false");
    assert.equal(findUI(h.app, element => element.id === "login-error").textContent, "Tên đăng nhập hoặc mật khẩu không đúng.");
});

test("login network and server failures use the same nontechnical message", async () => {
    for (const failure of [new TypeError("Failed to fetch"), response(503, {detail: "Redis host internal unavailable"})]) {
        const api = E.apiClient({base: "/api/v1/", token: () => "", fetcher: async () => {if (failure instanceof Error) throw failure; return failure;}});
        await assert.rejects(api.request("auth/login/", "POST", {}), error => error.message === "Không thể kết nối tới hệ thống. Vui lòng thử lại.");
    }
});

test("registration preserves the Citizen API payload and returns to login", async () => {
    const h = screenHarness("citizen", "/login"), fetcher = h.context.fetch; let payload;
    h.context.sessionStorage.getItem = () => "";
    h.context.fetch = (url, options) => {
        if (url.endsWith("auth/register/")) {payload = JSON.parse(options.body); return Promise.resolve(response(201, {}));}
        return fetcher(url, options);
    };
    await h.run();
    await findUI(h.app, element => element.className === "auth-link").click();
    const password = findUI(h.app, element => element.name === "password");
    assert.equal(password.attrs.autocomplete, "new-password");
    findUI(h.app, element => element.name === "username").value = "synthetic-citizen"; password.value = "SyntheticRegister42!";
    await findUI(h.app, element => element.tag === "form").onsubmit({preventDefault() {}});
    assert.deepEqual(payload, {username: "synthetic-citizen", password: "SyntheticRegister42!"});
    assert.equal(password.attrs.autocomplete, "current-password");
    assert.equal(findUI(h.app, element => element.id === "login-title").textContent, "Chào mừng trở lại");
    assert.equal(h.context.location.hash, "#/login");
});

test("API rejects late snapshots and blocks writes against stale data", async () => {
    let epoch = 0, complete, writes = 0;
    const api = E.apiClient({base: "/api/v1/", token: () => "", unauthorized() {}, snapshot: () => epoch, canWrite: () => false,
        fetcher: () => {writes++; return new Promise(resolve => {complete = resolve;});}});
    const read = api.request("incidents/"); epoch++; complete(response(200, {results: [{id: 1}]}));
    await assert.rejects(read, error => error.name === "ObsoleteSnapshot");
    await assert.rejects(api.request("incidents/1/status/", "PATCH", {}), error => error.status === 0);
    assert.equal(writes, 1);
});

test("realtime stays stale until REST resync, queues events and ignores obsolete sockets", async () => {
    const clock = timers(), sockets = [], states = [], events = []; let finish;
    class Socket {constructor() {sockets.push(this); this.readyState = 1;} send() {} close() {this.onclose?.({code: 1006});}}
    const client = E.realtime({url: "ws://test", token: () => "fixture", Socket, timers: clock, now: () => 0,
        state: (_, mode) => states.push(mode), ready: () => new Promise(resolve => {finish = resolve;}), event: data => events.push(data), revoked() {}});
    sockets[0].onopen(); sockets[0].onmessage({data: '{"type":"ready"}'}); await flushUI();
    sockets[0].onmessage({data: '{"type":"team.location_updated"}'});
    assert.equal(states.at(-1), "syncing"); assert.equal(events.length, 0);
    finish(true); await flushUI(); assert.equal(states.at(-1), "online"); assert.equal(events.length, 1);
    sockets[0].onclose({code: 1006}); assert.equal(states.at(-1), "offline");
    client.retry(); sockets[1].onmessage({data: '{"type":"ready"}'}); await flushUI();
    sockets[1].onclose({code: 1006}); finish(true); await flushUI();
    sockets[0].onmessage({data: '{"type":"team.location_updated"}'});
    assert.equal(states.at(-1), "offline"); assert.equal(events.length, 1); client.stop();
});

test("failed resync never advertises online and keeps retry bounded", async () => {
    for (const result of [false, new Error("fixture failure")]) {
        const clock = timers(), states = []; let socket;
        class Socket {constructor() {socket = this;} send() {} close() {this.onclose({code: 1006});}}
        const client = E.realtime({url: "ws://test", token: () => "fixture", Socket, timers: clock, now: () => 0, state: (_, mode) => states.push(mode),
            ready: async () => {if (result instanceof Error) throw result; return result;}, event() {}, revoked() {}});
        socket.onmessage({data: '{"type":"ready"}'}); await flushUI();
        assert.ok(!states.includes("online")); assert.equal(states.at(-1), "offline");
        assert.equal(clock.timeouts.size, 1); client.stop();
    }
});

test("all live role screens mark snapshots stale immediately and restore after resync", async () => {
    for (const [role, route] of [["dispatcher", "/dispatcher"], ["rescue_team", "/rescue"], ["admin", "/admin/users"]]) {
        const h = screenHarness(role, route); await h.run();
        const shell = h.context.document.getElementById("ui-shell"), content = h.context.document.getElementById("content");
        assert.equal(shell.dataset.viewState, "success");
        h.sockets[0].onclose({code: 1006});
        assert.equal(shell.dataset.viewState, "stale"); assert.equal(content.inert, true);
        const notice = findUI(h.app, node => node.className === "view-status");
        assert.equal(notice.hidden, false); assert.match(notice.text, /Mất kết nối/); assert.match(notice.text, /Dữ liệu gần nhất/);
        h.handlers.online(); h.sockets[1].onmessage({data: '{"type":"ready"}'});
        assert.notEqual(shell.dataset.viewState, "success"); await flushUI();
        assert.equal(shell.dataset.viewState, "success"); assert.equal(content.inert, false); assert.equal(notice.hidden, true);
    }
});

test("initial loading, empty and failed snapshots have distinct UI states", async () => {
    const loading = screenHarness("dispatcher", "/dispatcher", false, true); await loading.run();
    assert.equal(loading.context.document.getElementById("ui-shell").dataset.viewState, "loading");
    assert.equal(loading.context.document.getElementById("content").attrs["aria-busy"], "true");
    loading.releaseDashboard(); await flushUI();
    loading.sockets[0].onmessage({data: '{"type":"ready"}'}); await flushUI();
    assert.equal(loading.context.document.getElementById("ui-shell").dataset.viewState, "success");
    assert.ok(findUI(loading.app, node => node.dataset.state === "empty"));
    const failed = screenHarness("dispatcher", "/dispatcher", true); await failed.run();
    assert.notEqual(failed.context.document.getElementById("ui-shell").dataset.viewState, "success");
    assert.ok(!failed.app.text.includes("Synthetic service unavailable"));
});

test("Citizen retains editable draft when offline, while mission GPS failures hide browser errors", async () => {
    const h = screenHarness("citizen", "/citizen/report"); await h.run();
    const description = findUI(h.app, node => node.name === "description"); description.value = "Keep draft";
    h.context.navigator.onLine = false; h.handlers.offline();
    assert.equal(h.context.document.getElementById("ui-shell").dataset.viewState, "offline");
    assert.equal(h.context.document.getElementById("content").inert, false); assert.equal(description.value, "Keep draft");
    h.context.navigator.onLine = true; h.handlers.online(); assert.equal(h.context.document.getElementById("ui-shell").dataset.viewState, "success");
    let failure; const states = [];
    const gps = E.gps({geo: {watchPosition(_, error) {failure = error; return 1;}, clearWatch() {}}, timers: timers(), now: Date.now, send() {}, state: text => states.push(text)});
    gps.start(); failure({code: 2, message: "POSITION_UNAVAILABLE: raw native error"});
    assert.ok(!states.at(-1).includes("POSITION_UNAVAILABLE")); gps.stop();
});

test("geocoding debounces, aborts superseded lookups and ignores late responses", async () => {
    const clock = timers(), requests = [];
    const api = {request(path, method, body, options) {return new Promise(resolve => requests.push({path, body, options, resolve}));}};
    const geocoder = E.geocoding({api, timers: clock, Controller: AbortController});
    const cancelled = geocoder.search("Old place").catch(error => error.name);
    const second = geocoder.search("Second place").catch(error => error.name);
    assert.equal(await cancelled, "AbortError"); assert.equal(requests.length, 0); assert.equal(clock.timeouts.size, 1);
    const run = [...clock.timeouts.values()][0].fn();
    const newest = geocoder.search("New place");
    assert.equal(await second, "AbortError"); assert.equal(requests[0].options.signal.aborted, true);
    requests[0].resolve([{address: "Stale"}]); await run;
    const last = [...clock.timeouts.values()].at(-1).fn(); requests.at(-1).resolve([]); await last;
    assert.deepEqual(await newest, []);
    const reverse = geocoder.reverse({latitude: 21, longitude: 105}).catch(error => error.name);
    geocoder.destroy(); assert.equal(await reverse, "AbortError");
});

test("video capture is bounded, camera-only and cancelled recordings release tracks", async () => {
    let stopped = 0, active;
    class Recorder {
        static isTypeSupported(type) {return type === "video/webm";}
        constructor() {active = this; this.state = "inactive";}
        start(ms) {assert.equal(ms, 1000); this.state = "recording";}
        stop() {this.state = "inactive"; queueMicrotask(() => this.onstop());}
    }
    const clock = timers(), video = {play: async () => {}}, now = 1700000000000;
    const cam = E.camera({devices: {getUserMedia: async () => ({getTracks: () => [{stop() {stopped++;}}]})}, canvas: () => {}, now: () => now, makeFile: (blob, name) => new File([blob], name, {type: blob.type})});
    await cam.start(video);
    const recording = cam.record(video, {latitude: 21, longitude: 105, timestamp: new Date(now).toISOString()}, {Recorder, timers: clock, maxBytes: 100, maxSeconds: 30});
    active.ondataavailable({data: new Blob(["scene"])});
    assert.equal([...clock.timeouts.values()][0].ms, 30000);
    [...clock.timeouts.values()][0].fn();
    const result = await recording; assert.equal(result.file.type, "video/webm"); assert.equal(result.metadata.capture_source, "camera"); assert.equal(result.metadata.capture_longitude, 105); assert.equal(stopped, 1); assert.equal(video.srcObject, null);
    await cam.start(video);
    const cancelled = cam.record(video, null, {Recorder, timers: clock, maxBytes: 100, maxSeconds: 30}); cam.stop();
    assert.equal(await cancelled, null); assert.equal(stopped, 2);
    await cam.start(video);
    const tooLarge = cam.record(video, null, {Recorder, timers: clock, maxBytes: 2, maxSeconds: 30});
    active.ondataavailable({data: new Blob(["oversize"])});
    await assert.rejects(tooLarge, /vượt giới hạn/); assert.equal(stopped, 3);
});

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
    await assert.rejects(E.camera({}).start({}), /camera/);
});
test("API centralizes token, JSON errors, revocation and network errors", async () => {
    let revoked = 0, status = 200, called;
    const api = E.apiClient({base: "/api/v1/", token: () => "synthetic", unauthorized: () => revoked++, fetcher: async (url, options) => {called = {url, options}; return response(status, {detail: "Expired"});}});
    await api.request("auth/me/"); assert.equal(called.options.headers.Authorization, "Token synthetic");
    status = 401; await assert.rejects(api.request("incidents/"), error => error.status === 401 && error.body.detail === "Expired" && /hết hạn/.test(error.message)); assert.equal(revoked, 1);
    const disconnected = E.apiClient({base: "/api/v1/", token: () => "", unauthorized() {}, fetcher: async () => {throw new Error("offline");}});
    await assert.rejects(disconnected.request("incidents/"), error => error.status === 0);
});
test("pagination follows trusted same-origin links and never leaks tokens to foreign next links", async () => {
    let calls = 0;
    const api = E.apiClient({base: "/api/v1/", token: () => "synthetic", unauthorized() {}, fetcher: async () => response(200, {results: [++calls], next: calls === 1 ? "http://test.local/api/v1/items/?page=2" : null})});
    assert.deepEqual(await api.all("items/"), [1, 2]);
    await assert.rejects(api.request("https://foreign.example/api/v1/"), error => error.status === 400); assert.equal(calls, 2);
});
test("WebSocket authenticates in first frame, resyncs, retries with bounded backoff and cleans timers", async () => {
    const t = timers(), sockets = []; let ready = 0, revoked = 0, now = 100;
    class Socket {constructor(url) {this.url = url; this.readyState = 1; this.sent = []; sockets.push(this);} send(data) {this.sent.push(JSON.parse(data));} close() {this.onclose({code: 1006});}}
    const client = E.realtime({url: "ws://test.local/ws/rescue/", token: () => "secret", Socket, timers: t, now: () => now, state() {}, event() {}, ready: () => ready++, revoked: () => revoked++});
    sockets[0].onopen(); assert.equal(sockets[0].sent[0].type, "authenticate"); assert.ok(!sockets[0].url.includes("secret"));
    sockets[0].onmessage({data: '{"type":"ready"}'}); await new Promise(resolve => setImmediate(resolve)); assert.equal(ready, 1);
    sockets[0].onclose({code: 1006}); assert.equal([...t.timeouts.values()][0].ms, 1000);
    const timer = [...t.timeouts.entries()][0]; t.timeouts.delete(timer[0]); timer[1].fn();
    sockets[1].onopen(); sockets[1].onmessage({data: '{"type":"ready"}'}); await new Promise(resolve => setImmediate(resolve)); assert.equal(ready, 2);
    sockets[1].onclose({code: 4403}); assert.equal(revoked, 1); assert.equal(t.timeouts.size, 0);
    client.stop(); sockets[1].onmessage({data: '{"type":"ready"}'}); await new Promise(resolve => setImmediate(resolve)); assert.equal(ready, 2); assert.equal(t.intervals.size, 0);
});
test("WebSocket heartbeat closes a silent socket, ignores malformed JSON, delivers domain events", async () => {
    const t = timers(); let socket, clock = 0, events = 0;
    class Socket {constructor() {socket = this; this.readyState = 1;} send() {} close() {this.onclose({code: 1006});}}
    const client = E.realtime({url: "ws://test", token: () => "synthetic", Socket, timers: t, now: () => clock, state() {}, ready() {}, revoked() {}, event: () => events++});
    socket.onopen(); socket.onmessage({data: "broken"}); socket.onmessage({data: '{"type":"ready"}'}); await new Promise(resolve => setImmediate(resolve));
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

test("GPS stops claiming current position when the device no longer produces fresh samples", async () => {
    let callback, clock = 100000, sends = 0; const states = [];
    const gps = E.gps({geo: {watchPosition(fn) {callback = fn; return 1;}, clearWatch() {}}, timers: timers(), now: () => clock,
        state: (_, mode) => states.push(mode), send: async () => sends++});
    gps.start(); callback({coords: {latitude: 21, longitude: 105, accuracy: 5}, timestamp: clock}); await gps.tick();
    assert.equal(states.at(-1), "online"); clock += 61000; await gps.tick(); await gps.tick();
    assert.equal(states.at(-1), "offline"); assert.equal(sends, 1);
    assert.equal(E.freshPosition({timestamp: new Date(clock + 1000).toISOString()}, clock), false);
    gps.stop();
});

test("Admin configuration and unknown statuses never expose field names, raw enums or IDs", async () => {
    const admin = screenHarness("admin", "/admin/config"); await admin.run();
    assert.match(admin.app.text, /Quy tắc vận hành/); assert.ok(!/CLUSTER_RADIUS_METERS|undefined|NaN|environment variables/.test(admin.app.text));
    const report = screenHarness("dispatcher", "/dispatcher/reports/5"), fetcher = report.context.fetch;
    report.context.fetch = async (url, options) => url.endsWith("incident-reports/5/") ? response(200, {id: 5, category: 999, review_status: "internal_unknown_enum", description: "Business report"}) : fetcher(url, options);
    await report.run(); assert.match(report.app.text, /Chưa cập nhật/);
    assert.ok(!/internal_unknown_enum|Báo cáo #5|999|location_accuracy/.test(report.app.text));
});

test("startup network failure shows an offline message without discarding the session", async () => {
    const h = screenHarness("citizen", "/citizen/report"), fetcher = h.context.fetch; let cleared = 0;
    h.context.sessionStorage.removeItem = () => cleared++;
    h.context.fetch = async (url, options) => {if (url.endsWith("config.json")) throw new Error("network internals"); return fetcher(url, options);};
    await h.run(); assert.match(h.app.text, /Mất kết nối/); assert.match(h.app.text, /Thử lại/);
    assert.ok(!h.app.text.includes("network internals")); assert.equal(cleared, 0);
});

test("Citizen reconnect retains a labelled snapshot until REST really finishes", async () => {
    const h = screenHarness("citizen", "/citizen/reports"), fetcher = h.context.fetch; let release, delay = false;
    h.context.fetch = async (url, options) => {if (delay && url.includes("incident-reports/")) await new Promise(resolve => {release = resolve;}); return fetcher(url, options);};
    await h.run(); const shell = h.context.document.getElementById("ui-shell");
    h.handlers.offline(); delay = true; h.handlers.online(); await flushUI();
    assert.equal(shell.dataset.viewState, "stale"); assert.equal(h.context.document.getElementById("content").inert, true);
    release(); await flushUI(); assert.equal(shell.dataset.viewState, "success");
});

test("Admin rules resync after reconnect and a failed resync keeps the error and retry visible", async () => {
    const h = screenHarness("admin", "/admin/config"), fetcher = h.context.fetch; let unavailable = false, reads = 0;
    h.context.fetch = async (url, options) => {if (url.includes("configuration/")) {reads++; if (unavailable) return response(503, {detail: "Raw backend error"});} return fetcher(url, options);};
    await h.run(); const initialReads = reads; assert.equal(h.context.document.getElementById("ui-shell").dataset.viewState, "success");
    h.sockets[0].onclose({code: 1006}); unavailable = true; h.handlers.online(); h.sockets[1].onmessage({data: '{"type":"ready"}'}); await flushUI();
    assert.ok(reads > initialReads); assert.equal(h.context.document.getElementById("ui-shell").dataset.viewState, "error");
    const notice = findUI(h.app, node => node.className === "view-status"); assert.match(notice.text, /Tạm thời không tải được dữ liệu/); assert.match(notice.text, /Dữ liệu gần nhất/);
    assert.equal(findUI(notice, node => node.tag === "button").hidden, false); assert.ok(!notice.text.includes("Raw backend error"));
});

test("incident history shows business actions without generated backend notes or report identifiers", async () => {
    const h = screenHarness("dispatcher", "/dispatcher/incidents/5"), fetcher = h.context.fetch;
    h.context.fetch = async (url, options) => {
        if (url.endsWith("status-history/")) return response(200, {results: [{to_status: "verified", note: "Created from accepted report 7777."}], next: null});
        if (url.endsWith("report-link-history/")) return response(200, {results: [{operation: "merge", report: 7777, note: "Response team assigned."}], next: null});
        return fetcher(url, options);
    };
    await h.run(); assert.match(h.app.text, /Gom báo cáo/);
    assert.ok(!/7777|Created from accepted report|Response team assigned|MTTR|MTTA/.test(h.app.text));
});
test("stopping GPS during an in-flight request prevents late success state and clears timers", async () => {
    const t = timers(); let callback, finish; const states = [];
    const pending = new Promise(resolve => {finish = resolve;});
    const gps = E.gps({geo: {watchPosition(fn) {callback = fn; return 1;}, clearWatch() {}}, timers: t, now: () => 100000, state: text => states.push(text), send: () => pending});
    gps.start(); callback({coords: {latitude: 21, longitude: 105, accuracy: 5}, timestamp: 100000});
    const request = gps.tick(); gps.stop(); finish(); await request;
    assert.equal(states.at(-1), "Đã dừng chia sẻ vị trí"); assert.equal(t.intervals.size, 0);
});
test("private upload hashes file, PUTs directly to S3 without app token, confirms only after success", async () => {
    const calls = [], file = {name: "synthetic.png", type: "image/png", size: 3, arrayBuffer: async () => new Uint8Array([1, 2, 3]).buffer};
    const api = {request: async (path, method, body) => {calls.push({path, method, body}); return path.includes("presign") ? {media: {id: "uuid"}, upload: {method: "PUT", url: "https://s3.example/private", headers: {"Content-Type": file.type}}} : {status: "ready"};}};
    let s3;
    const result = await E.upload(api, {report_id: 5}, file, {maxBytes: 100, crypto: webcrypto, fetcher: async (url, options) => {s3 = {url, options}; return response(200, {});}});
    assert.equal(result.status, "ready"); assert.equal(calls[0].body.checksum_sha256.length, 44); assert.equal(calls[1].path, "media/uuid/confirm/"); assert.equal(s3.options.headers.Authorization, undefined); assert.equal(s3.options.body, file);
    await assert.rejects(E.upload(api, {report_id: 5}, {...file, size: 101}, {maxBytes: 100, crypto: webcrypto}), /File phải/);
    calls.length = 0; await assert.rejects(E.upload(api, {report_id: 5}, file, {maxBytes: 100, crypto: webcrypto, fetcher: async () => response(403, {})}), /Không gửi được tệp/); assert.equal(calls.length, 1);
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
    const roots = [], handlers = {}, calls = [], requests = [], sockets = [], mapUpdates = []; let maps = 0, mapPicker = null;
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
    const user = {username: "synthetic", first_name: "Synthetic", last_name: "Citizen", phone: "+12025550123", role, response_team: 7};
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
            if (url.endsWith("config.json")) return response(200, {apiBase: "/api/v1/", dispatcherSocket: "/ws/dispatcher/", rescueSocket: "/ws/rescue/", mediaMaxBytes: 100, incidentLocationDistanceWarningMeters: 1000});
            if (url.endsWith("geocoding/reverse/")) return response(200, {address: "12 Synthetic Street, Synthetic Ward"});
            if (url.endsWith("geocoding/search/")) return response(200, [{latitude: 10.79, longitude: 106.71, address: "Synthetic destination"}]);
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
        WebSocket: class {constructor() {this.readyState = 1; sockets.push(this);} send() {} close() {}}, setInterval() {}, clearInterval() {}, setTimeout(fn, delay) {if (delay === 450) queueMicrotask(fn);}, clearTimeout() {}, addEventListener(type, fn) {handlers[type] = fn;}, removeEventListener(type) {delete handlers[type];},
        EmergencyMap: {createMap: () => {maps++; return {replace() {}, update(kind, item) {mapUpdates.push({kind, item});}, focus() {}, destroy() {}, pick(fn) {mapPicker = fn;}, edit() {}};}},
    }); context.window = context; context.Emergency = E;
    return {app, context, calls, requests, sockets, handlers, mapUpdates, choosePoint(latitude, longitude) {mapPicker?.(latitude, longitude);}, get maps() {return maps;}, setTaskStatus(status) {assignmentStatus = status;}, releaseDashboard, async navigate(path) {location.hash = path; await handlers.hashchange();}, async run() {vm.runInContext(readFileSync(join(__dirname, "../realtime/frontend/app.js"), "utf8"), context); for (let i = 0; i < 12; i++) await new Promise(resolve => setImmediate(resolve)); if (location.hash !== "#" + route && handlers.hashchange) {handlers.hashchange(); for (let i = 0; i < 12; i++) await new Promise(resolve => setImmediate(resolve));} if (sockets.length) {sockets[0].onopen(); sockets[0].onmessage({data: JSON.stringify({type: "ready"})}); for (let i = 0; i < 12; i++) await new Promise(resolve => setImmediate(resolve));}}};
}
test("actual role screens render report form, dashboard, rescue list and Admin data", async () => {
    for (const [role, route, title] of [["citizen", "/citizen/report", "Báo cáo sự cố"], ["dispatcher", "/dispatcher", "Trung tâm điều phối"], ["rescue_team", "/rescue", "Nhiệm vụ được giao"], ["admin", "/admin/users", "Tài khoản & phân quyền"]]) {
        const h = screenHarness(role, route); await h.run(); assert.match(h.app.text, new RegExp(title)); assert.ok(!h.app.text.includes("Đang tải dữ liệu"));
        if (role !== "citizen") assert.ok(h.calls.some(url => url.includes(role === "rescue_team" ? "assignments/" : role === "admin" ? "admin/users/" : "incidents/")));
    }
});

test("Citizen automatically gets device address, hides coordinates/accuracy, and offers camera only", async () => {
    const h = screenHarness("citizen", "/citizen/report"); let requests = 0;
    h.context.navigator.geolocation = {getCurrentPosition(success) {requests++; success({coords: {latitude: 21, longitude: 105, accuracy: 250}, timestamp: Date.now()});}};
    await h.run(); assert.equal(requests, 1); assert.match(h.app.text, /Vị trí chưa chính xác/); assert.match(h.app.text, /12 Synthetic Street/); assert.ok(!h.app.text.includes("±250")); assert.match(h.app.text, /CHỤP ẢNH/);
    for (const technical of ["Latitude", "Longitude", "Accuracy", "Metadata"]) assert.ok(!h.app.text.includes(technical));
    const files = element => (element.attrs.type === "file" || element.type === "file") || element.children.some(files);
    assert.equal(files(h.app), false); assert.match(h.app.text, /Họ và tên/);
    const denied = screenHarness("citizen", "/citizen/report"); denied.context.navigator.geolocation = {getCurrentPosition(_, failure) {failure({code: 1});}};
    await denied.run(); assert.match(denied.app.text, /Không thể lấy vị trí hiện tại/); assert.match(denied.app.text, /GỬI BÁO CÁO/);
});

test("a late automatic GPS response does not overwrite a manually selected incident position", async () => {
    const h = screenHarness("citizen", "/citizen/report"); let deliver;
    h.context.navigator.geolocation = {getCurrentPosition(success) {deliver = success;}};
    await h.run();
    const find = (element, text) => element.tag === "button" && element.text === text ? element : element.children.map(child => find(child, text)).find(Boolean);
    await find(h.app, "Chỉnh vị trí").click(); h.choosePoint(10.79, 106.71); await find(h.app, "XÁC NHẬN VỊ TRÍ").click();
    deliver({coords: {latitude: 21, longitude: 105, accuracy: 5}, timestamp: Date.now()});
    for (let i = 0; i < 4; i++) await new Promise(resolve => setImmediate(resolve));
    assert.equal(h.mapUpdates.at(-1).item.latitude, 10.79); assert.equal(h.mapUpdates.at(-1).item.longitude, 106.71); assert.match(h.app.text, /cách khá xa/);
    await find(h.app, "Chỉnh vị trí").click(); h.choosePoint(20, 104); await find(h.app, "Hủy").click(); assert.equal(h.mapUpdates.at(-1).item.latitude, 10.79);
});

test("Citizen camera form uploads a private draft before publishing the report", async () => {
    const h = screenHarness("citizen", "/citizen/report"), file = new File(["abc"], "scene-unit.jpg", {type: "image/jpeg"}), captured = {file, metadata: {capture_source: "camera", captured_at: new Date().toISOString()}};
    h.context.Emergency = {...E, camera: () => ({start: async () => true, stop() {}, capture: async () => captured})};
    h.context.navigator.geolocation = {getCurrentPosition(success) {success({coords: {latitude: 21, longitude: 105, accuracy: 5}, timestamp: Date.now()});}};
    await h.run();
    const find = (element, predicate) => predicate(element) ? element : element.children.map(child => find(child, predicate)).find(Boolean);
    const control = text => find(h.app, element => element.tag === "button" && element.text === text);
    await control("CHỤP ẢNH").click(); await control("Chụp ảnh").click(); await control("Sử dụng").click();
    for (const [name, value] of Object.entries({description: "Synthetic camera report", reporter_name: "Synthetic", reporter_phone: "+12025550123"})) find(h.app, element => element.name === name).value = value;
    const form = find(h.app, element => element.tag === "form"); form.onsubmit({preventDefault() {}});
    for (let i = 0; i < 20; i++) await new Promise(resolve => setImmediate(resolve));
    const writes = h.requests.filter(request => request.options.method && request.options.method !== "GET" && !request.url.includes("geocoding/"));
    assert.deepEqual(writes.map(request => new URL(request.url).pathname), ["/api/v1/incident-reports/drafts/", "/api/v1/media/presign/", "/scene", "/api/v1/media/synthetic-media/confirm/", "/api/v1/incident-reports/5/submit/"]);
    const upload = JSON.parse(writes[1].options.body); assert.equal(upload.capture_source, "camera"); assert.equal(upload.report_id, 5);
    const body = JSON.parse(writes[0].options.body); assert.deepEqual(body.gps_location, {latitude: 21, longitude: 105}); assert.equal(body.allow_contact, true); assert.equal(body.latitude, 21);
    assert.equal(writes[2].options.headers.Authorization, undefined); assert.equal(h.context.location.hash, "#/citizen/reports/5");
});

test("Citizen map hides coordinate popups and only allows marker dragging in edit mode", () => {
    const saved = global.document; let popup, dragging = false, dragEnd, clicks, selected;
    global.document = {createElement: () => ({children: [], textContent: "", append(child) {this.children.push(child);}})};
    try {
        const map = {setView() {return this;}, on(_, callback) {clicks = callback;}, remove() {}};
        const marker = {addTo() {return this;}, setIcon() {return this;}, setLatLng() {}, bindPopup(value) {popup = value;}, on(_, callback) {dragEnd = callback;}, getLatLng: () => ({lat: 20, lng: 104}), dragging: {enable() {dragging = true;}, disable() {dragging = false;}}};
        const L = {map: () => map, tileLayer: () => ({addTo() {}}), divIcon: value => value, marker: () => marker};
        const controller = require("../realtime/frontend/map.js").createMap({}, L, {showCoordinates: false});
        controller.update("report", {id: "draft", latitude: 21, longitude: 105, address: "Synthetic site"}, "Vị trí hiện trường");
        assert.equal(popup.children[1].textContent, "Synthetic site"); assert.equal(dragging, false);
        dragEnd(); assert.equal(selected, undefined);
        controller.edit("report", "draft", (lat, lon) => {selected = [lat, lon];}); assert.equal(dragging, true); dragEnd(); assert.deepEqual(selected, [20, 104]);
        controller.pick((lat, lon) => {selected = [lat, lon];}); clicks({latlng: {lat: 22, lng: 106}}); assert.deepEqual(selected, [22, 106]);
        controller.edit("report", "draft", null); controller.pick(null); dragging = false; dragEnd(); clicks({latlng: {lat: 23, lng: 107}});
        assert.deepEqual(selected, [22, 106]); controller.destroy();
    } finally {global.document = saved;}
});

test("denied and timed-out GPS allow manual submission, and geocoder failure keeps the site", async () => {
    const find = (element, predicate) => predicate(element) ? element : element.children.map(child => find(child, predicate)).find(Boolean);
    for (const code of [1, 3]) {
        const h = screenHarness("citizen", "/citizen/report"), fetcher = h.context.fetch, writes = [];
        h.context.navigator.geolocation = {getCurrentPosition(_, fail) {fail({code});}};
        h.context.fetch = async (url, options) => {
            if (url.endsWith("geocoding/reverse/")) return response(503, {detail: "Unavailable"});
            if (url.endsWith("incident-reports/")) {writes.push(JSON.parse(options.body)); return response(201, {id: 5});}
            return fetcher(url, options);
        };
        await h.run(); h.choosePoint(10.79, 106.71);
        await find(h.app, e => e.tag === "button" && e.text === "XÁC NHẬN VỊ TRÍ").click();
        for (let i = 0; i < 4; i++) await new Promise(resolve => setImmediate(resolve));
        assert.match(h.app.text, /Đã chọn vị trí trên bản đồ/);
        find(h.app, e => e.name === "description").value = "Synthetic test site";
        find(h.app, e => e.tag === "form").onsubmit({preventDefault() {}});
        for (let i = 0; i < 12; i++) await new Promise(resolve => setImmediate(resolve));
        assert.equal(writes.length, 1); assert.equal(writes[0].gps_location, null); assert.equal(writes[0].latitude, 10.79); assert.equal(writes[0].longitude, 106.71);
        assert.equal(h.context.location.hash, "#/citizen/reports/5");
    }
});

test("address search is explicit, empty results do not block map picking, and submit errors retain the form", async () => {
    const h = screenHarness("citizen", "/citizen/report"), fetcher = h.context.fetch, writes = []; let searches = 0, release;
    h.context.fetch = async (url, options) => {
        if (url.endsWith("geocoding/search/")) {searches++; return response(200, []);}
        if (url.endsWith("incident-reports/")) {writes.push(JSON.parse(options.body)); await new Promise(resolve => {release = resolve;}); return response(503, {detail: "Synthetic submit failure"});}
        return fetcher(url, options);
    };
    await h.run();
    const find = (element, predicate) => predicate(element) ? element : element.children.map(child => find(child, predicate)).find(Boolean);
    const control = text => find(h.app, e => e.tag === "button" && e.text === text);
    const input = find(h.app, e => e.tag === "input" && e.attrs.type === "search"); input.value = "Synthetic town"; input.oninput();
    assert.equal(searches, 0); await control("Tìm").click();
    for (let i = 0; i < 4; i++) await new Promise(resolve => setImmediate(resolve));
    assert.equal(searches, 1); assert.match(h.app.text, /Không tìm thấy địa chỉ/);
    h.choosePoint(21, 105); await control("XÁC NHẬN VỊ TRÍ").click();
    const description = find(h.app, e => e.name === "description"); description.value = "Keep this description";
    const form = find(h.app, e => e.tag === "form"); form.onsubmit({preventDefault() {}}); form.onsubmit({preventDefault() {}});
    for (let i = 0; i < 4; i++) await new Promise(resolve => setImmediate(resolve));
    const submit = find(h.app, e => e.tag === "button" && e.className === "report-submit");
    assert.equal(writes.length, 1); assert.equal(submit.disabled, true);
    release(); for (let i = 0; i < 4; i++) await new Promise(resolve => setImmediate(resolve));
    assert.match(h.app.text, /Tạm thời không tải được dữ liệu/); assert.equal(description.value, "Keep this description"); assert.equal(h.context.location.hash, "#/citizen/report"); assert.equal(submit.disabled, false);
    assert.equal(h.mapUpdates.at(-1).item.latitude, 21);
});

test("authorized mission shows shared contact and quick actions; no-consent report has no call", async () => {
    const mission = screenHarness("rescue_team", "/rescue/assignments/5", false, false, "responding"); await mission.run();
    assert.match(mission.app.text, /Synthetic reporter/); assert.match(mission.app.text, /Gọi người báo tin/);
    for (const label of ["Báo vấn đề", "YÊU CẦU THÊM LỰC LƯỢNG", "Báo cáo gốc", "Ảnh/video", "Hoàn thành nhiệm vụ"]) assert.ok(mission.app.text.includes(label), mission.app.text);
    const report = screenHarness("dispatcher", "/dispatcher/reports/5"); await report.run();
    assert.match(report.app.text, /Không cho phép gọi trực tiếp/); assert.ok(!report.app.text.includes("Gọi người báo tin"));
    const completed = screenHarness("rescue_team", "/rescue/assignments/5", false, false, "completed"); await completed.run();
    assert.ok(!completed.calls.some(url => url.endsWith("/contacts/"))); assert.ok(!completed.app.text.includes("+84900000000"));
});
test("screen displays unified API errors and Citizen guard never loads privileged data", async () => {
    const failed = screenHarness("dispatcher", "/dispatcher", true); await failed.run(); assert.match(failed.app.text, /Tạm thời không tải được dữ liệu/);
    const guarded = screenHarness("citizen", "/admin/users"); await guarded.run(); assert.match(guarded.app.text, /Báo cáo sự cố/); assert.ok(!guarded.calls.some(url => url.includes("admin/users/")));
});
test("detail screens finish loading media and show backend text safely", async () => {
    const report = screenHarness("dispatcher", "/dispatcher/reports/5"); await report.run();
    assert.match(report.app.text, /Ảnh & video hiện trường/); assert.match(report.app.text, /Synthetic <script>/);
    assert.ok(report.calls.some(url => url.includes("media/?report_id=5")));
    const incident = screenHarness("dispatcher", "/dispatcher/incidents/5"); await incident.run();
    assert.match(incident.app.text, /Ảnh & video hiện trường/); assert.match(incident.app.text, /Đã giải quyết/);
    assert.ok(incident.calls.some(url => url.includes("media/?incident_id=5")));
});
test("navigation during an in-flight snapshot loads new detail media and ignores the old response", async () => {
    const h = screenHarness("dispatcher", "/dispatcher", false, true); await h.run();
    await h.navigate("/dispatcher/reports/5"); assert.match(h.app.text, /Ảnh & video hiện trường/);
    h.releaseDashboard(); for (let i = 0; i < 12; i++) await new Promise(resolve => setImmediate(resolve));
    assert.match(h.app.text, /Chi tiết báo cáo/); assert.ok(!h.app.text.includes("Sự cố theo trạng thái"));
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
