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
    const SERIES_COLOR = Object.fromEntries(SERIES.map(([key, , color]) => [key, color]));
    // The totals strip above the chart: one tile per measure, out and in as seen from the perspective.
    const TOTALS = [
        { label: "Damage", out: ["damage_out"], in: ["damage_in"] },
        { label: "Reps", out: ["rep_out"], in: ["rep_in"] },
        { label: "Neut/nos (GJ)", out: ["neut_out", "nos_out"], in: ["neut_in", "nos_in"] },
        { label: "Cap transfers (GJ)", out: ["cap_out"], in: ["cap_in"], onlyIfAny: true },
        { label: "Jams", out: ["jam_out"], in: ["jam_in"] },
        { label: "Tackle", out: ["tackle_out"], in: ["tackle_in"] },
        { label: "Friendly fire", out: ["friendly_fire"] },
    ];

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
        // [first, last second] of a zoomed-in time window, or null. Kept in the URL only.
        window: null,
    };
    const urlWindow = [Number(params.get("from")), Number(params.get("to"))];
    if (params.has("from") && params.has("to") && urlWindow.every(Number.isInteger) && urlWindow[0] <= urlWindow[1]) {
        state.window = urlWindow;
    }

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
            ...(state.window ? { from: String(state.window[0]), to: String(state.window[1]) } : {}),
            ...extra,
        });
        return q.toString();
    }

    function persist() {
        save({ ...state, window: undefined });
        const url = `${window.location.pathname}?${query()}`;
        window.history.replaceState(null, "", url);
    }

    let chart = null;
    let lastResult = null;
    let lastDragEnd = 0;

    // Deaths on the timeline: one marker per killmail along the bottom of the plot, shaped and coloured by the
    // victim's side. Hovering one shows who lost what with a guide line through the chart; a click opens zKillboard.
    const DEATH_COLOR = { ours: "#2e7d32", theirs: "#c62828", unknown: "#757575" };
    const DEATH_LABEL_SPACING = 80; // px per death on average before ship names are drawn
    const deaths = { list: [], hits: [], hovered: null, changed: false, labelled: 0 };
    let deathTip = null;

    function deathSide(kill) {
        const side = config.sides.find((s) => s.id === kill.side);
        if (!side) {
            return { key: "unknown", name: "Unknown side" };
        }
        return { key: side.friendly ? "ours" : "theirs", name: side.name };
    }

    function visibleDeaths(result) {
        if (!result.labels.length) {
            return [];
        }
        const first = result.labels[0];
        const last = result.labels[result.labels.length - 1] + result.bucket - 1;
        return (result.kills || [])
            .filter((kill) => kill.time >= first && kill.time <= last)
            .map((kill) => ({ kill, index: (kill.time - first) / result.bucket }));
    }

    function drawDeathMarker(ctx, x, y, kind, hovered, ring) {
        const r = hovered ? 7 : 5;
        ctx.beginPath();
        if (kind === "ours") {
            ctx.arc(x, y, r, 0, Math.PI * 2);
        } else {
            ctx.moveTo(x, y - r - 1);
            ctx.lineTo(x + r + 1, y);
            ctx.lineTo(x, y + r + 1);
            ctx.lineTo(x - r - 1, y);
            ctx.closePath();
        }
        ctx.fillStyle = DEATH_COLOR[kind];
        ctx.fill();
        ctx.lineWidth = 2;
        ctx.strokeStyle = ring;
        ctx.stroke();
    }

    function showDeathTip(chartInstance, hit) {
        const container = chartInstance.canvas.parentElement;
        if (!deathTip) {
            deathTip = document.createElement("div");
            deathTip.className = "fightlog-death-tip";
            Object.assign(deathTip.style, {
                position: "absolute",
                zIndex: "3",
                pointerEvents: "none",
                padding: "4px 8px",
                borderRadius: "4px",
                fontSize: "0.8rem",
                whiteSpace: "nowrap",
                background: "var(--bs-body-bg, #fff)",
                color: "var(--bs-body-color, #212529)",
                border: "1px solid var(--bs-border-color, #dee2e6)",
                boxShadow: "0 2px 6px rgba(0, 0, 0, 0.15)",
            });
            container.appendChild(deathTip);
        }
        if (!hit) {
            deathTip.hidden = true;
            return;
        }
        const kill = deaths.list[hit.i].kill;
        const side = deathSide(kill);
        const ship = kill.ship ? `${/^[AEIOU]/i.test(kill.ship) ? "an" : "a"} ${esc(kill.ship)}` : "a ship";
        deathTip.innerHTML =
            `<div class="text-body-secondary">${time(kill.time)} · ${esc(side.name)}${kill.role === "related" ? " · related kill" : ""}</div>` +
            `<div><span class="fw-semibold">${esc(kill.victim || "Unknown pilot")}</span> lost ${ship}</div>` +
            `<div class="text-body-secondary">${kill.value ? `${short(kill.value)} ISK · ` : ""}click to open on zKillboard</div>`;
        deathTip.hidden = false;
        const left = Math.min(Math.max(hit.x - deathTip.offsetWidth / 2, 0), container.clientWidth - deathTip.offsetWidth);
        Object.assign(deathTip.style, { left: `${left}px`, top: `${hit.y - deathTip.offsetHeight - 12}px` });
    }

    const deathMarkers = {
        id: "deathMarkers",
        beforeEvent(chartInstance, args) {
            const e = args.event;
            let hovered = deaths.hovered;
            if (e.type === "mousemove" || e.type === "click") {
                let hit = null;
                if (!drag) {
                    let best = 9; // px: a little bigger than the marker
                    deaths.hits.forEach((h) => {
                        const distance = Math.hypot(h.x - e.x, h.y - e.y);
                        if (distance <= best) {
                            best = distance;
                            hit = h;
                        }
                    });
                }
                hovered = hit ? hit.i : null;
                if (!drag) {
                    chartInstance.canvas.style.cursor = hit ? "pointer" : "crosshair";
                }
                if (hovered !== deaths.hovered) {
                    showDeathTip(chartInstance, hit);
                }
            } else if (e.type === "mouseout") {
                hovered = null;
                showDeathTip(chartInstance, null);
            }
            deaths.changed = hovered !== deaths.hovered;
            deaths.hovered = hovered;
        },
        afterEvent(chartInstance, args) {
            if (deaths.changed) {
                args.changed = true;
            }
        },
        afterDatasetsDraw(chartInstance) {
            const list = deaths.list || [];
            const { ctx, chartArea: area, scales } = chartInstance;
            const style = getComputedStyle(chartInstance.canvas);
            const ring = style.getPropertyValue("--bs-body-bg").trim() || "#fff";
            const ink = style.getPropertyValue("--bs-secondary-color").trim() || "#6c757d";
            // Deaths seconds apart would sit on top of each other: stack them upwards instead.
            const placed = [];
            list.forEach((death, i) => {
                const x = scales.x.getPixelForValue(death.index);
                let level = 0;
                while (level < 3 && placed.some((p) => p.level === level && Math.abs(p.x - x) < 12)) {
                    level += 1;
                }
                placed.push({ x, y: area.bottom - 7 - level * 13, level, i });
            });
            deaths.hits = [];
            deaths.labelled = 0;
            ctx.save();
            const hoveredSpot = placed.find((p) => p.i === deaths.hovered);
            if (hoveredSpot) {
                const x = hoveredSpot.x;
                ctx.strokeStyle = ink;
                ctx.lineWidth = 1;
                ctx.setLineDash([4, 3]);
                ctx.beginPath();
                ctx.moveTo(x, area.top);
                ctx.lineTo(x, area.bottom);
                ctx.stroke();
                ctx.setLineDash([]);
            }
            // Ship names only when there is room for them: never in the busy whole-fight view.
            const labelled = list.length > 0 && (area.right - area.left) / list.length >= DEATH_LABEL_SPACING;
            const labelBoxes = [];
            ctx.font = `10px ${window.Chart.defaults.font.family}`;
            ctx.textAlign = "center";
            // The hovered marker is drawn last, on top of its neighbours.
            const order = placed.filter((p) => p !== hoveredSpot).concat(hoveredSpot ? [hoveredSpot] : []);
            order.forEach(({ x, y, i }) => {
                const death = list[i];
                drawDeathMarker(ctx, x, y, deathSide(death.kill).key, i === deaths.hovered, ring);
                deaths.hits.push({ x, y, i });
                if (!labelled || !death.kill.ship) {
                    return;
                }
                // "Capsule - Genolution 'Auroral' 197-variant" is just a capsule here; the tooltip has the rest.
                const name = death.kill.ship.split(" - ")[0];
                // Above the top of this marker's stack, at the first of three heights where the name doesn't
                // run into one already drawn; else no name.
                const top = Math.min(...placed.filter((p) => Math.abs(p.x - x) < 12).map((p) => p.y));
                const half = ctx.measureText(name).width / 2 + 3;
                const ty = [top - 11, top - 23, top - 35].find(
                    (h) => !labelBoxes.some((b) => Math.abs(b.ty - h) < 11 && b.x0 < x + half && x - half < b.x1)
                );
                if (ty === undefined) {
                    return;
                }
                labelBoxes.push({ x0: x - half, x1: x + half, ty });
                ctx.lineWidth = 3;
                ctx.strokeStyle = ring;
                ctx.strokeText(name, x, ty);
                ctx.fillStyle = ink;
                ctx.fillText(name, x, ty);
                deaths.labelled += 1;
            });
            ctx.restore();
        },
    };

    function renderDeathLegend(list) {
        const legend = el("death-legend");
        const kinds = new Set(list.map((death) => deathSide(death.kill).key));
        legend.hidden = !list.length;
        const shape = (kind) =>
            kind === "ours"
                ? `<svg width="12" height="12" aria-hidden="true"><circle cx="6" cy="6" r="5" fill="${DEATH_COLOR.ours}"/></svg>`
                : `<svg width="12" height="12" aria-hidden="true"><path d="M6 0 L12 6 L6 12 L0 6 Z" fill="${DEATH_COLOR[kind]}"/></svg>`;
        const items = [
            ["ours", "our losses"],
            ["theirs", "their losses"],
            ["unknown", "unknown side"],
        ].filter(([kind]) => kinds.has(kind));
        legend.innerHTML =
            items.map(([kind, label]) => `<span class="me-2 text-nowrap">${shape(kind)} ${label}</span>`).join("") +
            `${list.length} death${list.length === 1 ? "" : "s"}: hover one for details, click to open it on zKillboard.`;
    }

    // The "Neut/nos" lines carry both: GJ neutralized plus GJ drained.
    function chartSeries(result) {
        const series = { ...result.series };
        ["out", "in"].forEach((dir) => {
            const nos = series[`nos_${dir}`] || [];
            series[`neut_${dir}`] = (series[`neut_${dir}`] || []).map((v, i) => v + (nos[i] || 0));
        });
        return series;
    }

    function renderChart(result) {
        const ctx = el("timeline");
        if (!window.Chart || !ctx) {
            return;
        }
        const labels = result.labels.map(time);
        const series = chartSeries(result);
        const datasets = SERIES.filter(([key]) => (series[key] || []).some((v) => v)).map(([key, label, color]) => ({
            label,
            data: series[key],
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
        deaths.list = visibleDeaths(result);
        deaths.hovered = null;
        if (deathTip) {
            deathTip.hidden = true;
        }
        renderDeathLegend(deaths.list);
        chart = new window.Chart(ctx, {
            type: "line",
            data: { labels, datasets },
            plugins: [deathMarkers],
            options: {
                animation: false,
                maintainAspectRatio: false,
                interaction: { mode: "index", intersect: false },
                // No series tooltip while a time window is dragged or a death is hovered: it would cover them.
                plugins: { tooltip: { filter: () => !drag && deaths.hovered === null } },
                scales: {
                    amount: { position: "left", beginAtZero: true, title: { display: true, text: "HP / GJ" } },
                    count: { position: "right", beginAtZero: true, grid: { drawOnChartArea: false }, title: { display: true, text: "count" } },
                },
                onClick: (evt, elements) => {
                    // Chart.js reports the click that ends a drag a frame later: that one zoomed, so skip it.
                    if (performance.now() - lastDragEnd < 400) {
                        return;
                    }
                    if (deaths.hovered !== null) {
                        window.open(deaths.list[deaths.hovered].kill.url, "_blank", "noopener");
                        return;
                    }
                    if (elements.length) {
                        const t = result.labels[elements[0].index];
                        showEvents(t, t + result.bucket - 1);
                    }
                },
            },
        });
    }

    const compact = new Intl.NumberFormat("en", { notation: "compact", maximumFractionDigits: 1 });
    const short = (n) => (Math.abs(n) < 10000 ? fmt(n) : compact.format(n));
    const sumOf = (series, keys) => keys.reduce((sum, key) => sum + (series[key] || []).reduce((a, b) => a + b, 0), 0);

    function renderTotals(result) {
        const box = el("totals");
        if (!result || result.empty) {
            box.innerHTML = "";
            return;
        }
        const option = el("perspective").selectedOptions[0];
        const who = option ? option.textContent.trim() : "";
        const when = state.window ? `${time(state.window[0])}–${time(state.window[1])}` : "whole fight";
        const value = (label, keys, dir) => {
            const n = sumOf(result.series, keys);
            const swatch = `<span class="d-inline-block rounded me-1 align-middle" style="width:12px;height:3px;background:${SERIES_COLOR[keys[0]] || "#9e9e9e"}"></span>`;
            const tail = dir ? ` <span class="small text-body-secondary">${dir}</span>` : "";
            return `<div class="text-nowrap" title="${esc(label)}${dir ? ` ${dir}` : ""}: ${fmt(n)}">${swatch}<span class="fw-semibold">${short(n)}</span>${tail}</div>`;
        };
        let html = `<div class="w-100 small text-body-secondary">Totals · ${esc(who)} · ${esc(when)}</div>`;
        TOTALS.forEach((tile) => {
            if (tile.onlyIfAny && !sumOf(result.series, [...tile.out, ...(tile.in || [])])) {
                return;
            }
            html += `<div class="border rounded px-2 py-1" data-total="${esc(tile.label)}"><div class="small text-body-secondary">${esc(tile.label)}</div><div class="d-flex gap-3">`;
            html += value(tile.label, tile.out, tile.in ? "out" : "") + (tile.in ? value(tile.label, tile.in, "in") : "") + "</div></div>";
        });
        box.innerHTML = html;
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

    function duration(seconds) {
        return seconds >= 60 ? `${Math.floor(seconds / 60)}m ${seconds % 60}s` : `${seconds}s`;
    }

    function renderZoomBar() {
        el("zoom-bar").hidden = !state.window;
        if (state.window) {
            const [first, last] = state.window;
            el("zoom-text").textContent =
                `Zoomed to ${time(first)}–${time(last)} (${duration(last - first + 1)}). ` +
                "The tables, targets and pilot details cover this window only.";
        }
    }

    async function refresh() {
        persist();
        renderZoomBar();
        el("analysis-status").textContent = "Loading…";
        try {
            const result = await getJson(`${config.analysisUrl}?${query()}`);
            lastResult = result;
            if (result.empty) {
                el("analysis-status").textContent = "No data yet.";
                renderTotals(result);
                return;
            }
            renderChart(result);
            renderTotals(result);
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

    // Damage the pilot table leaves out of "Dmg out", shown beside the weapons when there is any.
    const LEFT_OUT = [
        ["ff", "Friendly fire", "Hits on our own side: the table's FF column"],
        ["non_ship", "Drones & missiles", "Hits on drones, missiles in flight and structures. Tick “Count damage to drones, missiles and structures” to count them"],
        ["other", "NPCs & others", "Hits on NPCs and anyone else outside the fight"],
    ];

    function weaponsTable(weapons) {
        const extras = LEFT_OUT.filter(([field]) => weapons.some((w) => w[field]));
        const total = (field) => weapons.reduce((sum, w) => sum + (w[field] || 0), 0);
        const cells = (value) =>
            `<td class="text-end">${fmt(value("damage"))}</td><td class="text-end">${value("hits")}</td><td class="text-end">${value("misses")}</td>` +
            extras.map(([field]) => `<td class="text-end text-body-secondary">${fmt(value(field))}</td>`).join("");
        let html = `<h6>Weapons</h6><table class="table table-sm" id="weapons-table"><thead><tr><th>Weapon</th>
            <th class="text-end" title="Hits on other sides: the table's Dmg out">Damage</th><th class="text-end">Hits</th><th class="text-end">Misses</th>`;
        html += extras.map(([, label, title]) => `<th class="text-end" title="${esc(title)}">${label}</th>`).join("") + "</tr></thead><tbody>";
        weapons.forEach((w) => {
            html += `<tr><td>${esc(w.name)}</td>${cells((field) => w[field] || 0)}</tr>`;
        });
        html += `</tbody><tfoot><tr class="fw-semibold"><td>Total</td>${cells(total)}</tr></tfoot></table>`;
        if (extras.length) {
            html += '<p class="small text-body-secondary">Damage counts hits on other sides, as in the pilot table. The grey columns are left out of it.</p>';
        }
        return html;
    }

    async function showPilot(key) {
        const data = await getJson(`${config.pilotUrl}?${query({ key })}`);
        const p = data.participant || {};
        let html = `<div class="mb-2">${esc(p.corp || "")} ${p.alliance ? "· " + esc(p.alliance) : ""} · ${esc(p.side_name || "")}</div>`;
        if (p.ship_rows && p.ship_rows.length) {
            html += `<div class="mb-2">Ships: ${p.ship_rows.map((s) => `${esc(s.ship)} <span class="text-body-secondary">(${esc(s.source)})</span>`).join(", ")}</div>`;
        }
        html += weaponsTable(data.weapons);
        html += '<div class="row"><div class="col-md-6">' + list("Targets", data.targets, " HP") + '</div><div class="col-md-6">';
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
    // Drag across the chart to narrow the whole page to that time window; a plain click still lists a bucket's events.
    // The selection is styled here, not in fightlog.css, so a stale cached stylesheet can't make it invisible.
    const DRAG_MIN_PX = 6;
    const canvas = el("timeline");
    Object.assign(canvas.style, { cursor: "crosshair", touchAction: "pan-y" });
    const zoomBox = document.createElement("div");
    zoomBox.className = "fightlog-zoom-box";
    zoomBox.hidden = true;
    Object.assign(zoomBox.style, {
        position: "absolute",
        zIndex: "2",
        pointerEvents: "none",
        background: "rgba(var(--bs-primary-rgb, 13, 110, 253), 0.22)",
        borderLeft: "2px solid var(--bs-primary, #0d6efd)",
        borderRight: "2px solid var(--bs-primary, #0d6efd)",
    });
    const zoomLabel = document.createElement("div");
    Object.assign(zoomLabel.style, {
        position: "absolute",
        top: "4px",
        left: "50%",
        transform: "translateX(-50%)",
        whiteSpace: "nowrap",
        padding: "1px 6px",
        borderRadius: "4px",
        fontSize: "0.75rem",
        color: "#fff",
        background: "var(--bs-primary, #0d6efd)",
    });
    zoomBox.appendChild(zoomLabel);
    canvas.parentElement.appendChild(zoomBox);
    let drag = null;

    function windowFor(x0, x1) {
        const labels = lastResult.labels;
        const scale = chart.scales.x;
        const index = (px) => Math.min(labels.length - 1, Math.max(0, Math.round(scale.getValueForPixel(px))));
        return [labels[index(Math.min(x0, x1))], labels[index(Math.max(x0, x1))] + lastResult.bucket - 1];
    }

    canvas.addEventListener("pointerdown", (e) => {
        if (!chart || !lastResult || !lastResult.labels.length || e.button !== 0) {
            return;
        }
        const area = chart.chartArea;
        if (e.offsetX < area.left || e.offsetX > area.right || e.offsetY < area.top || e.offsetY > area.bottom) {
            return;
        }
        drag = { x0: e.offsetX, x1: e.offsetX };
        canvas.setPointerCapture(e.pointerId);
    });
    canvas.addEventListener("pointermove", (e) => {
        if (!drag) {
            return;
        }
        const area = chart.chartArea;
        drag.x1 = Math.min(Math.max(e.offsetX, area.left), area.right);
        const width = Math.abs(drag.x1 - drag.x0);
        zoomBox.hidden = width < DRAG_MIN_PX;
        canvas.style.cursor = zoomBox.hidden ? "crosshair" : "ew-resize";
        Object.assign(zoomBox.style, {
            left: `${Math.min(drag.x0, drag.x1)}px`,
            width: `${width}px`,
            top: `${area.top}px`,
            height: `${area.bottom - area.top}px`,
        });
        const [first, last] = windowFor(drag.x0, drag.x1);
        zoomLabel.textContent = `${time(first)}–${time(last)} · ${duration(last - first + 1)}`;
    });
    function endDrag(apply) {
        if (!drag) {
            return;
        }
        const { x0, x1 } = drag;
        drag = null;
        zoomBox.hidden = true;
        canvas.style.cursor = "crosshair";
        if (apply && Math.abs(x1 - x0) >= DRAG_MIN_PX) {
            lastDragEnd = performance.now();
            state.window = windowFor(x0, x1);
            refresh();
        }
    }
    canvas.addEventListener("pointerup", () => endDrag(true));
    canvas.addEventListener("pointercancel", () => endDrag(false));
    el("zoom-reset").addEventListener("click", () => {
        state.window = null;
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
    window.fightlog = { state, refresh, lastResult: () => lastResult, chart: () => chart, deaths: () => deaths };
})();
