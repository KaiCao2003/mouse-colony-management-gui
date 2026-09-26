(() => {
  "use strict";

  const root = document.querySelector("[data-photo-import]");
  if (!root) return;

  const uploadForm = root.querySelector("[data-photo-form]");
  const photoFile = uploadForm.elements.namedItem("photo_file");
  const readButton = uploadForm.querySelector('button[type="submit"]');
  const status = root.querySelector("[data-photo-status]");
  const preview = root.querySelector("[data-photo-preview]");
  const rowList = root.querySelector("[data-photo-rows]");
  const rowTemplate = root.querySelector("[data-photo-row]");
  const applyButton = root.querySelector("[data-photo-apply]");
  const csrfMeta = document.querySelector('meta[name="csrf-token"]');
  const mouseForms = [...document.querySelectorAll(".edit-animal-form")];
  const targets = new Map(mouseForms.map((form) => [form.id, form]));
  let reading = false;
  let cageMatches = false;

  function showStatus(message) {
    status.textContent = message;
    status.hidden = !message;
  }

  function resetPreview() {
    preview.hidden = true;
    rowList.replaceChildren();
    cageMatches = false;
    applyButton.disabled = true;
    showStatus("");
  }

  function showPreview(result) {
    const cageCard = result.cage_card_id || "Not detected";
    const cageLabel = root.querySelector("[data-photo-cage]");
    cageLabel.replaceChildren();
    if (result.cage_id !== null) {
      const link = document.createElement("a");
      link.href = `${root.dataset.basePath}/cages/${result.cage_id}`;
      link.textContent = cageCard;
      cageLabel.append(link);
    } else {
      cageLabel.textContent = cageCard;
    }
    root.querySelector("[data-photo-line]").textContent = result.line || "";
    cageMatches = Boolean(result.cage_card_id) &&
      result.cage_card_id.trim().toUpperCase() === root.dataset.cageCardId.trim().toUpperCase() &&
      (result.cage_id === null || result.cage_id === Number(root.dataset.cageId));

    for (const recognized of result.rows) {
      const row = rowTemplate.content.firstElementChild.cloneNode(true);
      const target = row.querySelector('[name="target"]');
      for (const form of mouseForms) {
        const legacyId = form.elements.namedItem("legacy_id").value.trim();
        const label = legacyId ? `${form.dataset.publicId} · ${legacyId}` : form.dataset.publicId;
        target.add(new Option(label, form.id));
      }
      const matches = mouseForms.filter((form) => recognized.mouse_id && (
        form.dataset.publicId === recognized.mouse_id ||
        form.elements.namedItem("legacy_id").value.trim() === recognized.mouse_id
      ));
      if (matches.length === 1) target.value = matches[0].id;
      row.querySelector('[name="mouse_id"]').value = recognized.mouse_id || "";
      row.querySelector('[name="sex"]').value = recognized.sex || "";
      row.querySelector('[name="dob"]').value = recognized.dob || "";
      row.querySelector("[data-photo-genotype]").textContent = recognized.genotype || "";
      const genotype = row.querySelector('[name="genotype"]');
      if ([...genotype.options].some((option) => option.value === recognized.genotype)) {
        genotype.value = recognized.genotype;
      }
      rowList.append(row);
    }
    preview.hidden = false;
    applyButton.disabled = !cageMatches || result.rows.length === 0;
    if (!result.cage_card_id) showStatus("Cage ID not detected. Try another photo.");
    else if (!cageMatches) showStatus("This photo belongs to a different cage.");
    else if (result.rows.length === 0) showStatus("No mice detected.");
    else showStatus("");
  }

  photoFile.addEventListener("change", resetPreview);

  uploadForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    event.stopPropagation();
    if (reading) return;
    resetPreview();
    const formData = new FormData(uploadForm);
    reading = true;
    photoFile.disabled = true;
    readButton.setAttribute("aria-disabled", "true");
    readButton.textContent = "Reading…";
    uploadForm.setAttribute("aria-busy", "true");
    try {
      const response = await fetch(uploadForm.action, {
        method: "POST",
        body: formData,
        headers: { Accept: "application/json", "X-CSRF-Token": csrfMeta.content },
        credentials: "same-origin",
        cache: "no-store",
      });
      const refreshedToken = response.headers.get("X-CSRF-Token");
      if (refreshedToken) csrfMeta.content = refreshedToken;
      if (!(response.headers.get("content-type") || "").includes("application/json")) {
        throw new Error("Reload the page and try again.");
      }
      const result = await response.json();
      if (!response.ok) {
        throw new Error(typeof result.detail === "string" ? result.detail : "Could not read this photo.");
      }
      showPreview(result);
    } catch (error) {
      showStatus(error instanceof Error ? error.message : "Could not read this photo.");
    } finally {
      reading = false;
      photoFile.disabled = false;
      readButton.removeAttribute("aria-disabled");
      readButton.textContent = "Read photo";
      uploadForm.removeAttribute("aria-busy");
    }
  });

  applyButton.addEventListener("click", () => {
    if (reading || !cageMatches) return;
    if (mouseForms.some((form) => form.dataset.submitting === "true")) {
      showStatus("Wait for the mouse save to finish.");
      return;
    }
    const assignments = [];
    const selected = new Set();
    for (const row of rowList.rows) {
      const targetId = row.querySelector('[name="target"]').value;
      if (targetId === "skip") continue;
      const form = targets.get(targetId);
      if (!form || !form.isConnected) {
        showStatus("Choose a mouse or Skip row for each row.");
        return;
      }
      if (selected.has(targetId)) {
        showStatus("Choose each target mouse only once.");
        return;
      }
      if ([...row.querySelectorAll("input, select")].some((control) => !control.reportValidity())) return;
      selected.add(targetId);
      assignments.push({ row, form });
    }

    let changed = 0;
    for (const { row, form } of assignments) {
      let mouseChanged = false;
      for (const [source, destination] of [["mouse_id", "legacy_id"], ["sex", "sex"], ["dob", "dob"], ["genotype", "genotype"]]) {
        const value = row.querySelector(`[name="${source}"]`).value.trim();
        const control = form.elements.namedItem(destination);
        if (source === "mouse_id" && value === form.dataset.publicId) continue;
        if (!value || value === control.value) continue;
        control.value = value;
        control.dispatchEvent(new Event("input", { bubbles: true }));
        control.dispatchEvent(new Event("change", { bubbles: true }));
        mouseChanged = true;
      }
      if (mouseChanged) changed += 1;
    }
    showStatus(changed ? `Applied to ${changed} ${changed === 1 ? "mouse" : "mice"}. Save all mice to save.` : "No changes to apply.");
  });
})();
