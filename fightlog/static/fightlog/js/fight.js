/* Fight page: timeline chart, per-side tables, targets, log toggles and drill-down. */
(() => {
    "use strict";

    const config = JSON.parse(document.getElementById("fight-config").textContent);
    const storageKey = `fightlog-fight-${config.id}`;
    const params = new URLSearchParams(window.location.search);

    const SERIES = [
        ["damage_out", "Damage out", "#2e7d32"],
        ["damage_in", "Damage in", "#c62828"],
        ["rep_out", "Reps out", "#1565c0"],
        ["rep_in", "Reps in", "#6a1b9a"],
        ["neut_out", "Neut/nos out (GJ)", "#ef6c00"],
        ["neut_in", "Neut/nos in (GJ)", "#795548"],
        ["jam_out", "Jams out", "#00838f"],
        ["jam_in", "Jams in", "#ad1457"],
        ["tackle_out", "Tackle out", "#9e9d24"],
        ["tackle_in", "Tackle in", "#5d4037"],
        ["friendly_fire", "Friendly fire", "#9e9e9e"],
    ];
    const COUNT_SERIES = new Set(["jam_out", "jam_in", "tackle_out", "tackle_in"]);

    function loadSaved() {
        try {
            return JSON.parse(window.localStorage.getItem(storageKey) || "{}");
        } catch (e) {
            return {};
        }
    }

    function save(state) {
        try {
            window.localStorage.setItem(storageKey, JSON.stringify(state));
        } catch (e) {
            /* storage unavailable: the URL still carries the state */
        }
    }

    const saved = loadSaved();
    const defaultLogs = config.logs.filter((l) => !l.excluded).map((l) => l.id);
    const state = {
        logs: params.has("logs") ? params.get("logs").split(",").filter(Boolean).map(Number) : saved.logs || defaultLogs,
        perspective: params.get("perspective") || saved.perspective || config.defaultPerspective,
        bucket: Number(params.get("bucket") || saved.bucket || 10),
        nonShips: (params.get("non_ships") || (saved.nonShips ? "1" : "0")) === "1",
    };

    const el = (id) => document.getElementById(id);
    const fmt = (n) => (n === null || n === undefined ? "" : Number(n).toLocaleString());
    const time = (t) => new Date(t * 1000).toISOString().substring(11, 19);
    const esc = (s) =>
        String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
    const sideName = (id) => (config.sides.find((s) => s.id === id) || { name: "Unknown" }).name;

    function syncControls() {
        el("perspective").value = state.perspective;
        el("bucket").value = String(state.bucket);
        el("non-ships").checked = state.nonShips;
        document.querySelectorAll(".log-toggle").forEach((box) => {
            box.checked = state.logs.includes(Number(box.value));
        });
    }

    function query(extra = {}) {
        const q = new URLSearchParams({
            logs: state.logs.join(","),
            perspective: state.perspective,
            bucket: String(state.bucket),
            non_ships: state.nonShips ? "1" : "0",
            ...extra,
        });
        return q.toString();
    }

    function persist() {
        save(state);
        const url = `${window.location.pathname}?${query()}`;
        window.history.replaceState(null, "", url);
    }

    let chart = null;
    let lastResult = null;

    function renderChart(result) {
        const ctx = el("timeline");
        if (!window.Chart || !ctx) {
            return;
        }
        const labels = result.labels.map(time);
        const datasets = SERIES.filter(([key]) => (result.series[key] || []).some((v) => v)).map(([key, label, color]) => ({
            label,
            data: result.series[key],
            borderColor: color,
            backgroundColor: color,
            yAxisID: COUNT_SERIES.has(key) ? "count" : "amount",
            tension: 0.2,
            pointRadius: 0,
            pointHitRadius: 6,
            borderWidth: 1.5,
        }));
        if (chart) {
            chart.destroy();
        }
        chart = new window.Chart(ctx, {
            type: "line",
            data: { labels, datasets },
            options: {
                animation: false,
                maintainAspectRatio: false,
                interaction: { mode: "index", intersect: false },
                scales: {
                    amount: { position: "left", beginAtZero: true, title: { display: true, text: "HP / GJ" } },
                    count: { position: "right", beginAtZero: true, grid: { drawOnChartArea: false }, title: { display: true, text: "count" } },
                },
                onClick: (evt, elements) => {
                    if (elements.length) {
                        const t = result.labels[elements[0].index];
                        showEvents(t, t + result.bucket - 1);
                    }
                },
            },
        });
    }

    function stateBadge(r, field) {
        const s = (r.states && (r.states[field] || r.states.default)) || "";
        if (s === "na") {
            return '<span class="text-body-secondary" title="Doesn\'t apply">n/a</span>';
        }
        return "";
    }

    function cellClass(r) {
        const s = r.states ? r.states.default : "";
        return s === "partial" ? "fl-partial" : s === "unattributed" ? "fl-unattributed" : "";
    }

    function renderTables(result) {
        const container = el("side-tables");
        const groups = new Map();
        result.pilots.forEach((r) => {
            const key = r.side === null || r.side === undefined ? "unknown" : String(r.side);
            if (!groups.has(key)) {
                groups.set(key, []);
            }
            groups.get(key).push(r);
        });
        const order = config.sides.map((s) => String(s.id)).concat(["unknown"]);
        let html = "";
        order.forEach((key) => {
            const rows = groups.get(key);
            if (!rows || !rows.length) {
                return;
            }
            const name = key === "unknown" ? "Unknown" : sideName(Number(key));
            html += `<div class="card mb-3"><div class="card-header d-flex justify-content-between"><span>${esc(name)} — ${rows.length} pilots</span>
                <span class="small text-body-secondary">grey cells: only what touched uploaders · italic: unattributed</span></div>
                <div class="card-body table-responsive"><table class="table table-sm table-hover fl-pilots" data-side="${key}"><thead><tr>
                <th>Pilot</th><th>Ships</th><th>Corp</th><th class="text-end">Dmg out</th><th class="text-end">Dmg in</th><th class="text-end">FF</th>
                <th class="text-end">Hit %</th><th class="text-end">Reps out</th><th class="text-end">Reps in</th><th class="text-end">Cap out</th>
                <th class="text-end">Neut out</th><th class="text-end">Neut in</th><th class="text-end">Jams out/in</th><th class="text-end">Tackle out/in</th><th>Log</th>
                </tr></thead><tbody>`;
            rows.forEach((r) => {
                const repsOut = r.rep_out.shield + r.rep_out.armor + r.rep_out.hull;
                const repsIn = r.rep_in.shield + r.rep_in.armor + r.rep_in.hull;
                const hint = r.hint === "aoe_mutual_only" ? ' <span class="badge text-bg-info" title="Only smartbomb contact with us, both ways">likely friendly</span>' : "";
                html += `<tr class="${cellClass(r)}">
                    <td><a href="#" class="fl-pilot" data-key="${esc(r.key)}">${esc(r.name)}</a>${hint}</td>
                    <td class="small">${esc(r.ships)}</td><td class="small">${esc(r.alliance || r.corp)}</td>
                    <td class="text-end" data-order="${r.damage_out}">${fmt(r.damage_out)}</td>
                    <td class="text-end" data-order="${r.damage_in}">${fmt(r.damage_in)}</td>
                    <td class="text-end" data-order="${r.ff_out}">${fmt(r.ff_out)}</td>
                    <td class="text-end" data-order="${r.hit_pct ?? -1}">${r.hit_pct === null ? stateBadge(r, "hit_pct") : r.hit_pct + "%"}</td>
                    <td class="text-end" data-order="${repsOut}">${fmt(repsOut)}</td>
                    <td class="text-end" data-order="${repsIn}">${fmt(repsIn)}</td>
                    <td class="text-end" data-order="${r.cap_out}">${fmt(r.cap_out)}</td>
                    <td class="text-end" data-order="${r.neut_out + r.nos_out}">${fmt(r.neut_out + r.nos_out)}</td>
                    <td class="text-end" data-order="${r.neut_in + r.nos_in}">${fmt(r.neut_in + r.nos_in)}</td>
                    <td class="text-end">${r.jam_out}/${r.jam_in}</td>
                    <td class="text-end">${r.tackle_out}/${r.tackle_in}</td>
                    <td>${r.has_log ? '<i class="fas fa-check text-success" title="Uploaded a log"></i>' : ""}</td></tr>`;
            });
            html += "</tbody></table></div></div>";
        });
        container.innerHTML = html || '<div class="alert alert-info">No pilots in the selected logs.</div>';
        if (window.DataTable) {
            container.querySelectorAll("table.fl-pilots").forEach((table) => {
                new window.DataTable(table, { paging: false, info: false, searching: false, order: [[3, "desc"]] });
            });
        }
    }

    function renderTargets(result) {
        const rows = result.targets.slice(0, 50);
        if (!rows.length) {
            el("targets").innerHTML = '<span class="text-body-secondary small">No damage in the selected logs.</span>';
            return;
        }
        let html = `<table class="table table-sm" id="targets-table"><thead><tr><th>Pilot</th><th>Side</th><th>First hit</th><th>Until</th>
            <th class="text-end">Seconds</th><th class="text-end">Damage taken</th><th class="text-end">Shooters</th><th class="text-end">Reps received</th></tr></thead><tbody>`;
        rows.forEach((r) => {
            html += `<tr><td><a href="#" class="fl-pilot" data-key="${esc(r.key)}">${esc(r.name)}</a></td><td>${esc(sideName(r.side))}</td>
                <td>${time(r.first_hit)}</td><td>${time(r.end)}${r.died ? ' <span class="badge text-bg-danger">died</span>' : ""}</td>
                <td class="text-end">${r.seconds}</td><td class="text-end">${fmt(r.damage)}</td><td class="text-end">${r.shooters}</td><td class="text-end">${fmt(r.reps)}</td></tr>`;
        });
        el("targets").innerHTML = html + "</tbody></table>";
    }

    async function getJson(url) {
        const response = await fetch(url, { credentials: "same-origin", headers: { Accept: "application/json" } });
        if (!response.ok) {
            throw new Error(`HTTP ${response.status}`);
        }
        return response.json();
    }

    async function refresh() {
        persist();
        el("analysis-status").textContent = "Loading…";
        try {
            const result = await getJson(`${config.analysisUrl}?${query()}`);
            lastResult = result;
            if (result.empty) {
                el("analysis-status").textContent = "No data yet.";
                return;
            }
            renderChart(result);
            renderTables(result);
            renderTargets(result);
            el("analysis-status").textContent = `${result.active_logs.length} logs · ${result.pilots.length} pilots`;
        } catch (err) {
            el("analysis-status").textContent = `Couldn't load the analysis (${err.message}).`;
        }
    }

    function modal(title, body) {
        el("drill-title").textContent = title;
        el("drill-body").innerHTML = body;
        if (window.bootstrap && window.bootstrap.Modal) {
            window.bootstrap.Modal.getOrCreateInstance(el("drill-modal")).show();
        } else {
            el("drill-modal").classList.add("show");
            el("drill-modal").style.display = "block";
        }
    }

    function eventsTable(events) {
        let html = `<table class="table table-sm" id="events-table"><thead><tr><th>Time</th><th>Kind</th><th>From</th><th>To</th><th>Module</th>
            <th class="text-end">Amount</th><th>Quality</th><th>Log (line)</th></tr></thead><tbody>`;
        events.forEach((e) => {
            html += `<tr><td>${time(e.t)}</td><td>${esc(e.kind)}${e.aoe ? ' <span class="badge text-bg-light">AoE</span>' : ""}${e.observed ? ' <span class="badge text-bg-light">seen</span>' : ""}</td>
                <td title="${esc(e.src_raw)}">${esc(e.src)}</td><td title="${esc(e.tgt_raw)}">${esc(e.tgt)}</td><td>${esc(e.module)}</td>
                <td class="text-end">${fmt(e.amount)}</td><td>${esc(e.quality)}</td><td class="small">${esc(e.log)} (${e.line})</td></tr>`;
        });
        return html + "</tbody></table>";
    }

    async function showEvents(start, end) {
        const data = await getJson(`${config.eventsUrl}?${query({ start: String(start), end: String(end), limit: "500" })}`);
        modal(`Events ${time(start)}–${time(end)} (${data.total})`, eventsTable(data.events));
    }

    function list(title, rows, unit) {
        if (!rows.length) {
            return "";
        }
        return `<h6 class="mt-2">${title}</h6><ul class="list-unstyled mb-0">${rows
            .map((r) => `<li>${esc(r.name)} — ${fmt(r.value)}${unit}</li>`)
            .join("")}</ul>`;
    }

    async function showPilot(key) {
        const data = await getJson(`${config.pilotUrl}?${query({ key })}`);
        const p = data.participant || {};
        let html = `<div class="mb-2">${esc(p.corp || "")} ${p.alliance ? "· " + esc(p.alliance) : ""} · ${esc(p.side_name || "")}</div>`;
        if (p.ship_rows && p.ship_rows.length) {
            html += `<div class="mb-2">Ships: ${p.ship_rows.map((s) => `${esc(s.ship)} <span class="text-body-secondary">(${esc(s.source)})</span>`).join(", ")}</div>`;
        }
        html += '<div class="row"><div class="col-md-6"><h6>Weapons</h6><table class="table table-sm"><thead><tr><th>Weapon</th><th class="text-end">Damage</th><th class="text-end">Hits</th><th class="text-end">Misses</th></tr></thead><tbody>';
        data.weapons.forEach((w) => {
            html += `<tr><td>${esc(w.name)}</td><td class="text-end">${fmt(w.damage)}</td><td class="text-end">${w.hits}</td><td class="text-end">${w.misses}</td></tr>`;
        });
        html += "</tbody></table>" + list("Targets", data.targets, " HP") + "</div><div class=\"col-md-6\">";
        html += list("Damaged by", data.damaged_by, " HP") + list("Repped by", data.repped_by, " HP") + list("Ewar and tackle by", data.ewar_by, "×");
        html += "</div></div>";
        const events = await getJson(`${config.eventsUrl}?${query({ key, limit: "300" })}`);
        html += `<h6 class="mt-3">Events (${events.total})</h6>` + eventsTable(events.events);
        modal(p.name || key, html);
    }

    document.addEventListener("click", (evt) => {
        const link = evt.target.closest(".fl-pilot");
        if (link) {
            evt.preventDefault();
            showPilot(link.dataset.key);
        }
    });
    el("perspective").addEventListener("change", (e) => {
        state.perspective = e.target.value;
        refresh();
    });
    el("bucket").addEventListener("change", (e) => {
        state.bucket = Number(e.target.value);
        refresh();
    });
    el("non-ships").addEventListener("change", (e) => {
        state.nonShips = e.target.checked;
        refresh();
    });
    document.querySelectorAll(".log-toggle").forEach((box) => {
        box.addEventListener("change", () => {
            state.logs = Array.from(document.querySelectorAll(".log-toggle:checked")).map((b) => Number(b.value));
            refresh();
        });
    });
    document.querySelectorAll('form[action$="/edit/exclude/"]').forEach((form) => {
        form.addEventListener("submit", (evt) => {
            const reason = window.prompt("Why exclude this log for everyone?", "");
            if (reason === null) {
                evt.preventDefault();
                return;
            }
            form.querySelector('input[name="reason"]').value = reason;
        });
    });
    const discordForm = el("discord-form");
    if (discordForm) {
        discordForm.addEventListener("submit", (evt) => {
            if (Number(discordForm.dataset.posted) > 0 && !window.confirm("This fight was already posted. Post it again?")) {
                evt.preventDefault();
            }
        });
    }

    async function waitForBuild() {
        if (config.built) {
            return;
        }
        const timer = window.setInterval(async () => {
            try {
                const status = await getJson(config.statusUrl);
                if (status.built) {
                    window.clearInterval(timer);
                    window.location.reload();
                }
            } catch (e) {
                /* keep polling */
            }
        }, 4000);
    }

    syncControls();
    refresh();
    waitForBuild();
    window.fightlog = { state, refresh, lastResult: () => lastResult };
})();
