(() => {
  "use strict";

  const root = document.querySelector("[data-photo-upload]");
  if (!root) return;

  const form = root.querySelector("[data-photo-upload-form]");
  const file = form.elements.namedItem("photo_file");
  const button = form.querySelector('button[type="submit"]');
  const status = root.querySelector("[data-photo-upload-status]");
  const preview = root.querySelector("[data-photo-upload-result]");
  const rows = root.querySelector("[data-photo-upload-rows]");
  const openCage = root.querySelector("[data-photo-upload-open]");
  const csrfMeta = document.querySelector('meta[name="csrf-token"]');
  let recognized = null;
  let reading = false;

  function showStatus(message) {
    status.textContent = message;
    status.hidden = !message;
  }

  function resetPreview() {
    recognized = null;
    preview.hidden = true;
    openCage.hidden = true;
    rows.replaceChildren();
    showStatus("");
  }

  function showPreview(result) {
    recognized = result;
    root.querySelector("[data-photo-upload-cage]").textContent = result.cage_card_id || "Not detected";
    root.querySelector("[data-photo-upload-line]").textContent = result.line || "";
    for (const mouse of result.rows) {
      const row = document.createElement("tr");
      const sex = { M: "♂ Male", F: "♀ Female" }[mouse.sex] || "";
      for (const value of [mouse.mouse_id, sex, mouse.dob, mouse.genotype]) {
        const cell = document.createElement("td");
        cell.textContent = value || "";
        row.append(cell);
      }
      rows.append(row);
    }
    preview.hidden = false;
    if (result.cage_id !== null) {
      openCage.href = `${root.dataset.basePath}/cages/${result.cage_id}#cage-actions`;
      openCage.hidden = false;
    } else {
      showStatus(result.cage_card_id ? "Cage not found." : "Cage ID not detected. Try another photo.");
    }
    if (result.cage_id !== null && result.rows.length === 0) showStatus("No mice detected.");
  }

  file.addEventListener("change", resetPreview);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    event.stopPropagation();
    if (reading) return;
    resetPreview();
    const data = new FormData(form);
    reading = true;
    file.disabled = true;
    button.setAttribute("aria-disabled", "true");
    button.textContent = "Reading…";
    form.setAttribute("aria-busy", "true");
    try {
      const response = await fetch(form.action, {
        method: "POST",
        body: data,
        headers: { Accept: "application/json", "X-CSRF-Token": csrfMeta.content },
        credentials: "same-origin",
        cache: "no-store",
      });
      const token = response.headers.get("X-CSRF-Token");
      if (token) csrfMeta.content = token;
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
      file.disabled = false;
      button.removeAttribute("aria-disabled");
      button.textContent = "Read photo";
      form.removeAttribute("aria-busy");
    }
  });

  openCage.addEventListener("click", (event) => {
    if (!recognized) {
      event.preventDefault();
      return;
    }
    try {
      sessionStorage.setItem("mouseline:photo-import", JSON.stringify(recognized));
    } catch {
      event.preventDefault();
      showStatus("Could not carry the photo preview to the cage. Try again.");
    }
  });
})();
