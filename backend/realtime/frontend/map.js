(function (root) {
    "use strict";
    const colors = {resolved: "#64748b", cancelled: "#64748b", available: "#168664", busy: "#d87a18", offline: "#64748b", pending: "#d87a18", accepted: "#168664", rejected: "#64748b", verified: "#247cc0", dispatched: "#d87a18", in_progress: "#c6434b"};
    function createMap(element, library = root.L, {tileUrl = "https://tile.openstreetmap.org/{z}/{x}/{y}.png", onStatus = () => {}, showCoordinates = false} = {}) {
        if (!library) { element.textContent = "Không tải được bản đồ. Hãy kiểm tra kết nối."; onStatus("Không tải được bản đồ."); return {update() {}, replace() {}, destroy() {}, focus() {}, visibility() {}, pick() {}, edit() {}}; }
        const map = library.map(element).setView([16.06, 108.2], 6);
        const tiles = library.tileLayer(tileUrl, {attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors', maxZoom: 19}).addTo(map);
        if (tiles && tiles.on) {
            tiles.on("tileerror", () => onStatus("Không tải được bản đồ nền."));
            tiles.on("tileload", () => onStatus(""));
        }
        const markers = new Map(), hidden = new Set(); let fitted = false, destroyed = false, pickCallback = null, bottomInset = 0, rightInset = 0, editKey = null, editCallback = null;
        map.on?.("click", event => pickCallback?.(event.latlng.lat, event.latlng.lng));
        function cancel(entry) {if (entry?.frame != null) root.cancelAnimationFrame?.(entry.frame);}
        function move(entry, item) {
            cancel(entry);
            if (!root.requestAnimationFrame || root.matchMedia?.("(prefers-reduced-motion: reduce)").matches) {entry.marker.setLatLng([item.latitude, item.longitude]); return;}
            const from = entry.marker.getLatLng(), lat = from.lat ?? from[0], lon = from.lng ?? from[1];
            const started = root.performance.now();
            function step(now) {
                if (destroyed) return;
                const t = Math.min(1, Math.max(0, (now - started) / 220));
                entry.marker.setLatLng([lat + (item.latitude - lat) * t, lon + (item.longitude - lon) * t]);
                entry.frame = t < 1 ? root.requestAnimationFrame(step) : null;
            }
            entry.frame = root.requestAnimationFrame(step);
        }
        function update(kind, item, title, click) {
            const key = `${kind}:${item.id ?? item.team_id}`;
            if (item.latitude == null || item.longitude == null) { const old = markers.get(key); if (old) {cancel(old); map.removeLayer(old.marker); markers.delete(key);} return; }
            const timestamp = item.timestamp || item.updated_at, old = markers.get(key);
            if (old && old.timestamp && timestamp && Date.parse(old.timestamp) > Date.parse(timestamp)) return;
            const color = colors[item.status || item.review_status] || (kind === "incident" ? "#c44d43" : "#2f63aa");
            const stale = kind === "team" && !root.Emergency?.freshPosition({timestamp});
            const symbol = kind === "team" ? "✚" : kind === "report" ? "•" : "!";
            const icon = library.divIcon({className: "", html: `<span class="map-marker marker-${kind}${stale ? " marker-stale" : ""}" style="--marker-color:${color}">${symbol}</span>`, iconSize: [36, 36], iconAnchor: [18, 18]});
            const marker = old ? old.marker.setIcon(icon) : library.marker([item.latitude, item.longitude], {icon, draggable: key === editKey, title, alt: title}).addTo(map);
            if (!old) marker.on?.("dragend", () => {if (editKey === key && editCallback) {const point = marker.getLatLng(); editCallback(point.lat, point.lng);}});
            if (key === editKey) marker.dragging?.enable(); else marker.dragging?.disable();
            const entry = old || {marker, frame: null};
            if (old) {if (kind === "team") move(entry, item); else marker.setLatLng([item.latitude, item.longitude]);}
            if (hidden.has(kind)) map.removeLayer(marker);
            const popup = document.createElement("div"), heading = document.createElement("strong"); heading.textContent = title; popup.append(heading);
            const info = document.createElement("p"); info.textContent = stale ? `Vị trí gần nhất · ${root.Emergency?.friendlyTime(timestamp) || "Chưa cập nhật"}` : showCoordinates ? `${item.latitude.toFixed(5)}, ${item.longitude.toFixed(5)}` : item.address || (kind === "team" ? "Vị trí đội ứng cứu" : "Vị trí hiện trường"); popup.append(info);
            if (click) { const button = document.createElement("button"); button.textContent = "Xem chi tiết"; button.onclick = click; popup.append(button); }
            marker.bindPopup(popup); entry.timestamp = timestamp; markers.set(key, entry);
        }
        function replace(kind, items, title, click) {
            const keys = new Set(items.map(item => `${kind}:${item.id ?? item.team_id}`));
            for (const [key, entry] of markers) if (key.startsWith(kind + ":") && !keys.has(key)) {cancel(entry); map.removeLayer(entry.marker); markers.delete(key); }
            items.forEach(item => update(kind, item, title(item), click ? () => click(item) : null));
            if (!fitted && markers.size) { map.fitBounds(library.latLngBounds([...markers.values()].map(entry => entry.marker.getLatLng())), {maxZoom: 14, paddingTopLeft: [30, 30], paddingBottomRight: [rightInset + 30, bottomInset + 30]}); fitted = true; }
            map.invalidateSize();
        }
        function edit(kind, id, callback) {editKey = callback ? `${kind}:${id}` : null; editCallback = callback; for (const [key, entry] of markers) {if (key === editKey) entry.marker.dragging?.enable(); else entry.marker.dragging?.disable();}}
        return {update, replace, edit, freeze() {for (const entry of markers.values()) cancel(entry);}, focus(lat, lon, zoom = true) {fitted = true; map.setView([lat, lon], zoom ? 15 : map.getZoom?.() || 15, {animate: false}); if (bottomInset || rightInset) map.panBy?.([rightInset / 2, bottomInset / 2], {animate: false});}, inset(value, right = 0) {const size = map.getSize?.() || {x: 1200, y: 900}, next = Math.max(0, Math.min(value, size.y - 100)), nextRight = Math.max(0, Math.min(right, size.x - 100)), delta = next - bottomInset, deltaRight = nextRight - rightInset; bottomInset = next; rightInset = nextRight; if (fitted) map.panBy?.([deltaRight / 2, delta / 2], {animate: false});}, pick(callback) {pickCallback = callback;}, visibility(kind, visible) {if (visible) hidden.delete(kind); else hidden.add(kind); for (const [key, entry] of markers) if (key.startsWith(kind + ":")) {if (visible) entry.marker.addTo(map); else map.removeLayer(entry.marker);}}, destroy() {destroyed = true; for (const entry of markers.values()) cancel(entry); markers.clear(); map.remove();}};
    }
    root.EmergencyMap = {createMap};
    if (typeof module !== "undefined") module.exports = {createMap};
})(typeof window === "undefined" ? globalThis : window);
