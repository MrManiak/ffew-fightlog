/* Fight list, curators only: tick fights, then delete or restore them in one go. */
(() => {
    "use strict";

    const form = document.getElementById("bulk-form");
    if (!form) {
        return;
    }
    const all = document.getElementById("bulk-all");
    const boxes = Array.from(document.querySelectorAll(".bulk-check"));
    const count = document.getElementById("bulk-count");
    const idle = count.textContent;
    const deleteButton = document.getElementById("bulk-delete");
    const restoreButton = document.getElementById("bulk-restore");

    function refresh() {
        const ticked = boxes.filter((b) => b.checked);
        const deleted = ticked.filter((b) => b.dataset.deleted === "1").length;
        const live = ticked.length - deleted;
        // Each button only acts on the fights it applies to: Delete on visible ones, Restore on deleted ones.
        deleteButton.disabled = live === 0;
        if (restoreButton) {
            restoreButton.disabled = deleted === 0;
        }
        count.textContent = ticked.length ? `${ticked.length} selected` : idle;
        all.checked = ticked.length > 0 && ticked.length === boxes.length;
        all.indeterminate = ticked.length > 0 && ticked.length < boxes.length;
    }

    all.addEventListener("change", () => {
        boxes.forEach((b) => {
            b.checked = all.checked;
        });
        refresh();
    });
    boxes.forEach((b) => b.addEventListener("change", refresh));
    // Confirm on the click, not on submit: Alliance Auth locks a form at its first submit event,
    // cancelled or not, so a declined confirmation there would leave both buttons dead.
    deleteButton.addEventListener("click", (e) => {
        if (!window.confirm(form.dataset.confirmDelete)) {
            e.preventDefault();
        }
    });
    refresh();
})();
