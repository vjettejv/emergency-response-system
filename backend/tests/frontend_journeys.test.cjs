const {test} = require("node:test");
const assert = require("node:assert/strict");
const {tracker} = require("../realtime/frontend/journeys.js");
const E = require("../realtime/frontend/client.js");
const {createMap} = require("../realtime/frontend/map.js");

function harness() {
    let now = 100000, online = true, fresh = true, calls = 0, failure = null, finish;
    const states = [], intervals = new Map();
    const result = {duration_seconds: 180, distance_meters: 1500};
    const engine = tracker({load: async () => {calls++; if (failure) throw failure; if (finish) return new Promise(resolve => {finish.resolve = resolve;}); return result;},
        fresh: () => fresh, online: () => online, now: () => now, intervalMs: 60000,
        state: (mode, data) => states.push({mode, data}), timers: {setInterval(fn) {intervals.set(1, fn); return 1;}, clearInterval(id) {intervals.delete(id);}}});
    return {engine, states, intervals, get calls() {return calls;}, advance(ms) {now += ms;}, offline() {online = false;}, stale() {fresh = false;}, fail(error) {failure = error;}, pending() {finish = {}; return finish;}};
}
const flush = () => new Promise(resolve => setImmediate(resolve));

test("route controller never calls while offline and has no fabricated result", async () => {
    const h = harness(); h.offline(); await h.engine.calculate();
    assert.equal(h.calls, 0); assert.deepEqual(h.states.at(-1), {mode: "offline", data: undefined}); h.engine.destroy();
});
test("route controller refreshes at most once per interval and clears stale GPS ETA", async () => {
    const h = harness(); h.engine.follow(true); await flush(); assert.equal(h.calls, 1);
    h.advance(59999); h.engine.tick(); await flush(); assert.equal(h.calls, 1);
    h.advance(1); h.engine.tick(); await flush(); assert.equal(h.calls, 2);
    h.stale(); h.engine.tick(); assert.equal(h.states.at(-1).mode, "stale"); assert.equal(h.states.at(-1).data, undefined); h.engine.destroy();
});
test("route failure clears earlier ETA and does not retain a fake estimate", async () => {
    const h = harness(); await h.engine.calculate(); assert.equal(h.states.at(-1).mode, "success");
    h.fail(new E.APIError(503)); await h.engine.calculate(); assert.equal(h.states.at(-1).mode, "error"); assert.equal(h.states.at(-1).data, null); h.engine.destroy();
});
test("disconnect discards an outstanding route response", async () => {
    const h = harness(), pending = h.pending(), request = h.engine.calculate();
    h.offline(); h.engine.invalidate("offline"); pending.resolve({duration_seconds: 10}); await request;
    assert.equal(h.states.at(-1).mode, "offline"); h.engine.destroy();
});
test("changing context or destroying a page discards late responses and releases timers", async () => {
    const h = harness(), pending = h.pending(), request = h.engine.calculate();
    h.engine.invalidate(); h.engine.destroy(); pending.resolve({duration_seconds: 10}); await request;
    assert.equal(h.states.at(-1).mode, "empty"); assert.equal(h.intervals.size, 0);
});
test("busy routing does not create parallel provider calls", async () => {
    const h = harness(), pending = h.pending(), request = h.engine.calculate();
    await h.engine.calculate(); assert.equal(h.calls, 1); pending.resolve({}); await request; h.engine.destroy();
});
test("follow mode needs fresh samples and pauses after stopping", async () => {
    const h = harness(); h.stale(); h.engine.follow(true); await flush(); assert.equal(h.calls, 0);
    h.engine.follow(false); h.advance(120000); h.engine.tick(); assert.equal(h.calls, 0); h.engine.destroy();
});
test("Citizen cannot open history or routing workspaces", () => {
    for (const path of ["/journeys", "/navigation"]) {
        assert.equal(E.allowed("citizen", path), false);
        for (const role of ["dispatcher", "rescue_team", "admin"]) assert.equal(E.allowed(role, path), true);
    }
});
test("Leaflet draws observed segments separately from dashed suggested route, replacing old layers", () => {
    const layers = [], removed = [], fitted = [];
    const map = {setView() {return this;}, on() {}, removeLayer(layer) {removed.push(layer);}, fitBounds(bounds) {fitted.push(bounds);}, remove() {}};
    const layer = (coordinates, options) => {const value = {coordinates, options, addTo() {layers.push(this); return this;}}; return value;};
    const library = {map: () => map, tileLayer: () => ({addTo() {return this;}, on() {}}), polyline: layer, circleMarker: layer, latLngBounds: points => points};
    const ui = createMap({}, library);
    ui.path("history", [[[105, 21], [105.001, 21.001]], [[106, 22]]], true);
    assert.equal(layers.length, 2); assert.deepEqual(layers[0].coordinates, [[21, 105], [21.001, 105.001]]); assert.equal(layers[0].options.dashArray, undefined);
    ui.path("route", [[[105, 21], [106, 22]]]); assert.equal(layers[2].options.dashArray, "9 7");
    ui.path("route", []); assert.ok(removed.includes(layers[2])); assert.equal(fitted.length, 1);
    ui.destroy();
});
