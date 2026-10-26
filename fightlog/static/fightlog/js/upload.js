/* Upload page: pre-filter files in the browser, upload two at a time, then follow processing. */
(() => {
    "use strict";

    const cfg = document.getElementById("upload-config").dataset;
    const maxBytes = Number(cfg.maxMb) * 1024 * 1024;
    const FILENAME_RE = /(\d{8})_(\d{6})(?:_(\d+))?\.txt$/;
    const SESSION_RE = /Session Started:\s*(\d{4})\.(\d{2})\.(\d{2})/;
    const ENGLISH_HINT = /\(combat\)[^\n]*(?: to | from |misses|remote |energy |Warp |jammed|target locks)/;
    const FINAL = new Set(["parsed", "no_pvp", "failed", "rejected"]);

    const from = document.getElementById("from");
    const to = document.getElementById("to");
    const tbody = document.querySelector("#upload-table tbody");
    const summary = document.getElementById("upload-summary");
    const counts = { seen: 0, skipped: 0, sent: 0, done: 0 };
    const queue = [];
    let active = 0;

    function setDays(days) {
        const today = new Date();
        to.value = today.toISOString().substring(0, 10);
        if (days === 0) {
            from.value = "";
            to.value = "";
            return;
        }
        const start = new Date(today.getTime() - (days - 1) * 86400000);
        from.value = start.toISOString().substring(0, 10);
    }
    setDays(Number(cfg.defaultDays));
    document.querySelectorAll("[data-days]").forEach((b) => b.addEventListener("click", () => setDays(Number(b.dataset.days))));

    function updateSummary() {
        summary.textContent = `${counts.seen} files looked at · ${counts.skipped} skipped · ${counts.sent} uploaded · ${counts.done} processed`;
    }

    function sessionDate(file, text) {
        const m = FILENAME_RE.exec(file.name);
        if (m) {
            return `${m[1].substring(0, 4)}-${m[1].substring(4, 6)}-${m[1].substring(6, 8)}`;
        }
        const s = SESSION_RE.exec(text);
        return s ? `${s[1]}-${s[2]}-${s[3]}` : null;
    }

    function inRange(date) {
        if (!date) {
            return false;
        }
        return (!from.value || date >= from.value) && (!to.value || date <= to.value);
    }

    function addRow(file) {
        document.getElementById("upload-table").hidden = false;
        const tr = document.createElement("tr");
        tr.innerHTML = "<td></td><td></td><td></td><td></td>";
        tr.cells[0].textContent = file.webkitRelativePath || file.name;
        tbody.prepend(tr);
        return tr;
    }

    function setStatus(tr, text, kind) {
        tr.cells[2].textContent = text;
        tr.cells[2].className = kind ? `text-${kind}` : "";
    }

    async function consider(file) {
        counts.seen += 1;
        updateSummary();
        if (!file.name.endsWith(".txt")) {
            counts.skipped += 1;
            return;
        }
        const quickDate = sessionDate(file, "");
        if (quickDate && !inRange(quickDate)) {
            counts.skipped += 1;
            return;
        }
        if (file.size > maxBytes) {
            const tr = addRow(file);
            setStatus(tr, `Skipped: larger than ${cfg.maxMb} MB`, "warning");
            counts.skipped += 1;
            return;
        }
        const text = await file.text();
        if (!/Listener:/.test(text.substring(0, 2000)) || !inRange(sessionDate(file, text)) || !text.includes("(combat)")) {
            counts.skipped += 1;
            updateSummary();
            return;
        }
        const tr = addRow(file);
        const listener = /Listener:\s*(.+)/.exec(text);
        tr.cells[1].textContent = listener ? listener[1].trim() : "";
        if (!ENGLISH_HINT.test(text)) {
            setStatus(tr, "Skipped: not an English client log", "warning");
            counts.skipped += 1;
            return;
        }
        setStatus(tr, "Waiting…");
        queue.push({ file, tr });
        pump();
    }

    async function upload(item) {
        const body = new FormData();
        body.append("file", item.file, item.file.name);
        setStatus(item.tr, "Uploading…");
        const response = await fetch(cfg.uploadUrl, {
            method: "POST",
            body,
            credentials: "same-origin",
            headers: { "X-CSRFToken": cfg.csrf },
        });
        let data = {};
        try {
            data = await response.json();
        } catch (e) {
            data = { status: "rejected", message: `HTTP ${response.status}` };
        }
        counts.sent += 1;
        updateSummary();
        if (data.status === "rejected") {
            setStatus(item.tr, `Rejected: ${data.message}`, "danger");
            return;
        }
        setStatus(item.tr, data.message, data.status === "accepted" ? "" : "secondary");
        if (data.log_id) {
            follow(item.tr, data.log_id);
        }
    }

    async function follow(tr, logId) {
        const url = cfg.statusUrl.replace(/0\/$/, `${logId}/`);
        for (let attempt = 0; attempt < 120; attempt += 1) {
            try {
                const response = await fetch(url, { credentials: "same-origin", headers: { Accept: "application/json" } });
                const data = await response.json();
                if (FINAL.has(data.status)) {
                    const kind = data.status === "parsed" ? "success" : data.status === "no_pvp" ? "secondary" : "danger";
                    setStatus(tr, data.status_label + (data.message ? `: ${data.message}` : ""), kind);
                    tr.cells[3].innerHTML = "";
                    data.fights.forEach((f) => {
                        const a = document.createElement("a");
                        a.href = f.url;
                        a.textContent = f.title + (f.built ? "" : " (building…)");
                        a.className = "d-block";
                        tr.cells[3].appendChild(a);
                    });
                    if (data.status !== "parsed" || data.fights.every((f) => f.built)) {
                        counts.done += 1;
                        updateSummary();
                        return;
                    }
                }
            } catch (e) {
                /* retry */
            }
            await new Promise((r) => window.setTimeout(r, 2000));
        }
    }

    function pump() {
        while (active < 2 && queue.length) {
            const item = queue.shift();
            active += 1;
            upload(item)
                .catch((err) => setStatus(item.tr, `Upload failed: ${err.message}`, "danger"))
                .finally(() => {
                    active -= 1;
                    pump();
                });
        }
    }

    function takeFiles(list) {
        Array.from(list).forEach((f) => consider(f));
    }

    async function walkEntry(entry) {
        if (entry.isFile) {
            await new Promise((resolve) => entry.file((f) => { consider(f); resolve(); }, resolve));
        } else if (entry.isDirectory) {
            const reader = entry.createReader();
            let batch;
            do {
                batch = await new Promise((resolve) => reader.readEntries(resolve, () => resolve([])));
                for (const child of batch) {
                    await walkEntry(child);
                }
            } while (batch.length);
        }
    }

    const zone = document.getElementById("dropzone");
    ["dragenter", "dragover"].forEach((name) =>
        zone.addEventListener(name, (e) => {
            e.preventDefault();
            zone.classList.add("active");
        }),
    );
    ["dragleave", "drop"].forEach((name) => zone.addEventListener(name, () => zone.classList.remove("active")));
    zone.addEventListener("drop", (e) => {
        e.preventDefault();
        const items = e.dataTransfer.items;
        if (items && items.length && items[0].webkitGetAsEntry) {
            Array.from(items).forEach((item) => {
                const entry = item.webkitGetAsEntry();
                if (entry) {
                    walkEntry(entry);
                }
            });
        } else {
            takeFiles(e.dataTransfer.files);
        }
    });
    document.getElementById("file-input").addEventListener("change", (e) => takeFiles(e.target.files));
    document.getElementById("folder-input").addEventListener("change", (e) => takeFiles(e.target.files));
    window.fightlogUpload = { consider, counts };
})();
