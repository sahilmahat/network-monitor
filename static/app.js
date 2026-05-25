/* ====================================================================
   CIPL Network Monitor — Dashboard logic
   Plain JS. Fetches the API every 10s and re-renders the DOM.
   ==================================================================== */

const REFRESH_INTERVAL_MS = 10_000;

/* -------- Utilities -------- */

// Format an ISO timestamp into a relative string: "12s ago", "3m ago"
function timeAgo(isoString) {
    if (!isoString) return "never";
    const then = new Date(isoString).getTime();
    const now = Date.now();
    const seconds = Math.floor((now - then) / 1000);
    if (seconds < 5) return "just now";
    if (seconds < 60) return `${seconds}s ago`;
    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) return `${minutes}m ago`;
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return `${hours}h ago`;
    const days = Math.floor(hours / 24);
    return `${days}d ago`;
}

// Format a duration in seconds: 78 -> "1m 18s"
function formatDuration(seconds) {
    if (seconds == null) return "—";
    if (seconds < 60) return `${seconds}s`;
    const m = Math.floor(seconds / 60);
    const s = seconds % 60;
    if (m < 60) return `${m}m ${s}s`;
    const h = Math.floor(m / 60);
    return `${h}h ${m % 60}m`;
}

// Format a timestamp into a wall-clock time string: "14:23"
function formatTime(isoString) {
    if (!isoString) return "—";
    const d = new Date(isoString);
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

/* -------- Fetchers -------- */

async function fetchJSON(url) {
    const res = await fetch(url);
    if (!res.ok) throw new Error(`${url} returned ${res.status}`);
    return await res.json();
}

/* -------- Renderers -------- */

function renderSummary(summary) {
    document.getElementById("stat-total").textContent = summary.total_targets;
    document.getElementById("stat-up").textContent = summary.up;
    document.getElementById("stat-down").textContent = summary.down;
    document.getElementById("stat-incidents").textContent = summary.incidents_24h;

    const updatedEl = document.getElementById("last-updated");
    updatedEl.textContent = `Last updated ${timeAgo(summary.last_updated)} — auto-refresh ${REFRESH_INTERVAL_MS / 1000}s`;
}

function renderTargetCard(target, uptimePercent) {
    const isUp = target.is_up === 1 || target.is_up === true;
    const statusClass = isUp ? "up" : "down";
    const statusText = isUp ? "UP" : "DOWN";

    const latencyOrError = isUp
        ? `${target.latency_ms} ms`
        : "no response";

    const uptimeStr = uptimePercent != null
        ? `Uptime 24h: ${uptimePercent}%`
        : "Uptime 24h: —";

    return `
        <div class="target-card ${statusClass}">
            <div class="target-card-header">
                <div class="target-name">${escapeHTML(target.name)}</div>
                <span class="target-status ${statusClass}">${statusText}</span>
            </div>
            <div class="target-host">${escapeHTML(target.host)} — ${latencyOrError}</div>
            <div class="target-meta">${uptimeStr}</div>
        </div>
    `;
}

function renderTargets(statusData, uptimeMap) {
    const gateways = statusData.targets.filter(t => t.type === "gateway");
    const vms = statusData.targets.filter(t => t.type === "vm");

    const gatewaysGrid = document.getElementById("gateways-grid");
    const vmsGrid = document.getElementById("vms-grid");

    gatewaysGrid.innerHTML = gateways.length
        ? gateways.map(t => renderTargetCard(t, uptimeMap[t.host])).join("")
        : '<p class="empty-msg">No gateways configured.</p>';

    vmsGrid.innerHTML = vms.length
        ? vms.map(t => renderTargetCard(t, uptimeMap[t.host])).join("")
        : '<p class="empty-msg">No VMs configured.</p>';
}

function renderIncidents(incidents) {
    const list = document.getElementById("incidents-list");
    if (incidents.length === 0) {
        list.innerHTML = '<p class="empty-msg">No incidents in the last 24 hours.</p>';
        return;
    }

    list.innerHTML = incidents.map(inc => {
        const dotClass = inc.event_type === "down" ? "down" : "recovered";
        const verb = inc.event_type === "down"
            ? "went down"
            : `recovered${inc.duration_seconds ? ` after ${formatDuration(inc.duration_seconds)}` : ""}`;
        return `
            <div class="incident-row">
                <div>
                    <span class="incident-dot ${dotClass}"></span>
                    <strong>${escapeHTML(inc.name)}</strong> ${verb}
                </div>
                <div class="incident-time">${formatTime(inc.occurred_at)}</div>
            </div>
        `;
    }).join("");
}

// Defence against XSS — never trust string content going into innerHTML
function escapeHTML(s) {
    if (s == null) return "";
    return String(s)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
}

/* -------- Main refresh cycle -------- */

async function refresh() {
    const healthBadge = document.getElementById("health-badge");

    try {
        // Fetch summary + status in parallel
        const [summary, status, incidents] = await Promise.all([
            fetchJSON("/api/summary"),
            fetchJSON("/api/status"),
            fetchJSON("/api/incidents?hours=24"),
        ]);

        // Fetch uptime for each target in parallel (small N — fine)
        const uptimePromises = status.targets.map(t =>
            fetchJSON(`/api/uptime/${encodeURIComponent(t.host)}?hours=24`)
                .then(r => [t.host, r.uptime_percentage])
                .catch(() => [t.host, null])
        );
        const uptimeEntries = await Promise.all(uptimePromises);
        const uptimeMap = Object.fromEntries(uptimeEntries);

        // Render everything
        renderSummary(summary);
        renderTargets(status, uptimeMap);
        renderIncidents(incidents.incidents);

        healthBadge.textContent = "Online";
        healthBadge.classList.remove("disconnected");

    } catch (err) {
        console.error("refresh failed:", err);
        healthBadge.textContent = "Disconnected";
        healthBadge.classList.add("disconnected");
        document.getElementById("last-updated").textContent =
            `Lost connection — ${err.message}`;
    }
}

/* -------- Boot -------- */

refresh();
setInterval(refresh, REFRESH_INTERVAL_MS);
