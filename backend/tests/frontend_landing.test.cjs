const test = require("node:test");
const assert = require("node:assert/strict");
const vm = require("node:vm");
const {readFileSync} = require("node:fs");
const {join} = require("node:path");
const E = require("../realtime/frontend/client.js");
const source = readFileSync(join(__dirname, "../realtime/frontend/landing.js"), "utf8");
const flush = async () => {for (let i = 0; i < 10; i++) await new Promise(resolve => setImmediate(resolve));};

function harness(token = "", role = "citizen", status = 200) {
    const handlers = {}, requests = [], attrs = {}, label = {textContent: ""}; let stored = token, focused = false, finish;
    const link = {dataset: {entry: "Đăng nhập"}, querySelector: () => label};
    const menu = {hidden: true, setAttribute(key, value) {attrs[key] = value;}, getAttribute: key => attrs[key], addEventListener(type, fn) {handlers["menu:" + type] = fn;}, focus() {focused = true;}};
    const navAttrs = {}, navigation = {setAttribute(key, value) {navAttrs[key] = value;}, removeAttribute(key) {delete navAttrs[key];}, addEventListener(type, fn) {handlers["nav:" + type] = fn;}};
    const response = (code, body) => ({status: code, ok: code < 400, headers: {get() {return null;}}, json: async () => body});
    const context = vm.createContext({Emergency: E, Date, document: {
        querySelectorAll: () => [link], getElementById: id => id === "public-menu" ? menu : id === "public-navigation" ? navigation : {textContent: ""},
        addEventListener(type, fn) {handlers[type] = fn;},
    }, sessionStorage: {getItem: () => stored, removeItem() {stored = "";}},
    fetch: async (url, options) => {
        requests.push({url, options});
        if (url.endsWith("config.json")) return response(200, {apiBase: "/api/v1/"});
        if (status === "network") throw new TypeError("Synthetic connection failure");
        if (status === "delay") await new Promise(resolve => {finish = resolve;});
        return response(typeof status === "number" ? status : 200, {role});
    }, addEventListener(type, fn) {handlers[type] = fn;},
    WebSocket: class {constructor() {throw new Error("Landing must not open a socket");}},
    navigator: {get geolocation() {throw new Error("Landing must not access GPS");}, get mediaDevices() {throw new Error("Landing must not access camera");}},
    }); context.window = context;
    return {requests, handlers, attrs, navAttrs, label, link, get focused() {return focused;}, get token() {return stored;}, setToken(value) {stored = value;}, release() {finish();}, async run() {vm.runInContext(source, context); await flush();}};
}

test("guest landing needs no API, socket, GPS or camera and menu works by keyboard", async () => {
    const h = harness(); await h.run();
    assert.equal(h.requests.length, 0); assert.equal(h.link.href, "/login"); assert.equal(h.label.textContent, "Đăng nhập");
    h.handlers["menu:click"](); assert.equal(h.attrs["aria-expanded"], "true"); assert.ok(Object.hasOwn(h.navAttrs, "data-open"));
    h.handlers.keydown({key: "Escape"}); assert.equal(h.attrs["aria-expanded"], "false"); assert.equal(h.focused, true);
    h.handlers["menu:click"](); h.handlers["nav:click"]({target: {closest: () => ({})}}); assert.equal(h.attrs["aria-expanded"], "false");
});

test("verified landing CTA enters the existing role route, only auth is fetched", async () => {
    for (const role of Object.keys(E.homes)) {
        const h = harness("synthetic-token", role); await h.run();
        assert.equal(h.link.href, `/realtime/#${E.homes[role]}`); assert.equal(h.label.textContent, "Vào hệ thống");
        assert.deepEqual(h.requests.map(request => new URL(request.url, "http://test.local").pathname), ["/realtime/config.json", "/api/v1/auth/me/"]);
        assert.equal(h.requests[1].options.headers.Authorization, "Token synthetic-token");
        h.handlers.pageshow({persisted: false}); await flush(); assert.equal(h.requests.length, 2);
    }
    assert.equal(E.homeURL("untrusted-role"), "/login");
});

test("expired token clears itself, connection failure keeps token and public CTA usable", async () => {
    for (const status of [401, 503, "network"]) {
        const h = harness("synthetic-token", "citizen", status); await h.run();
        assert.equal(h.link.href, "/login"); assert.equal(h.token, status === 401 ? "" : "synthetic-token");
    }
});

test("browser back after logout and late auth response cannot restore a private CTA", async () => {
    const h = harness("synthetic-token", "dispatcher"); await h.run(); h.setToken("");
    h.handlers.pageshow({persisted: true}); await flush(); assert.equal(h.link.href, "/login");
    const delayed = harness("synthetic-token", "dispatcher", "delay"); await delayed.run();
    delayed.setToken(""); delayed.handlers.pagehide(); delayed.release(); await flush();
    assert.equal(delayed.link.href, "/login");
});
