(() => {
  "use strict";

  const csrfMeta = document.querySelector('meta[name="csrf-token"]');
  const clientNotice = document.querySelector("[data-client-notice]");
  const splitForm = document.querySelector("#split-form");
  const batchEditForm = document.querySelector("[data-batch-edit-form]");
  const animalSelectors = [...document.querySelectorAll(".animal-selector")];
  const selectionCount = document.querySelector("[data-selection-count]");
  const workspacePanels = [...document.querySelectorAll("[data-workspace-panel]")];
  const workspaceTabLinks = [...document.querySelectorAll("[data-workspace-tab-link]")];
  const aopsResultsHeading = document.querySelector("#aops-results-title");
  const serverErrorNotice = document.querySelector(".notice--error");
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
  const animalEditForms = [...document.querySelectorAll(".edit-animal-form")];
  const saveAllMiceButton = document.querySelector("[data-save-all-mice]");
  const mouseSaveSummary = document.querySelector("[data-mouse-save-summary]");
  const savedAnimalValues = new Map();
  let savingAllMice = false;
  let activeAnimalSaves = 0;

  function workspaceTabFromHash() {
    const requestedTab = window.location.hash.slice(1);
    return workspacePanels.some((panel) => panel.dataset.workspacePanel === requestedTab)
      ? requestedTab
      : "cages";
  }

  function isWorkspaceTabHash() {
    const requestedTab = window.location.hash.slice(1);
    return workspacePanels.some((panel) => panel.dataset.workspacePanel === requestedTab);
  }

  function focusAopsResults() {
    if (
      serverErrorNotice instanceof HTMLElement ||
      !(aopsResultsHeading instanceof HTMLElement) ||
      window.location.hash !== "#aops-review"
    ) {
      return;
    }
    const hasReconciliationQuery = [...new URLSearchParams(window.location.search).keys()].some(
      (key) => /aops|reconcil|run_id/i.test(key),
    );
    if (!hasReconciliationQuery) return;

    window.requestAnimationFrame(() => {
      aopsResultsHeading.focus({ preventScroll: true });
      aopsResultsHeading.scrollIntoView({
        behavior: reducedMotion.matches ? "auto" : "smooth",
        block: "start",
      });
    });
  }

  function focusAopsError() {
    if (!(serverErrorNotice instanceof HTMLElement) || window.location.hash !== "#aops-review") {
      return;
    }
    serverErrorNotice.tabIndex = -1;
    window.requestAnimationFrame(() => {
      serverErrorNotice.focus({ preventScroll: true });
      serverErrorNotice.scrollIntoView({
        behavior: reducedMotion.matches ? "auto" : "smooth",
        block: "start",
      });
    });
  }

  function activateWorkspaceTab(tabName, scrollToPanel = false) {
    if (!workspacePanels.length) return;
    const focusedElement = document.activeElement;
    const focusMovesWithPanel =
      focusedElement instanceof HTMLElement &&
      workspacePanels.some(
        (panel) => panel.dataset.workspacePanel !== tabName && panel.contains(focusedElement),
      );

    for (const panel of workspacePanels) {
      const isActive = panel.dataset.workspacePanel === tabName;
      panel.hidden = !isActive;
      panel.setAttribute("aria-hidden", isActive ? "false" : "true");
    }

    for (const link of workspaceTabLinks) {
      const isActive = link.dataset.workspaceTabLink === tabName;
      link.classList.toggle("is-active", isActive);
      link.setAttribute("aria-selected", isActive ? "true" : "false");
      link.tabIndex = isActive ? 0 : -1;
    }

    if (scrollToPanel || focusMovesWithPanel) {
      window.requestAnimationFrame(() => {
        const panel = document.getElementById(tabName);
        if (scrollToPanel) panel?.scrollIntoView({ block: "start" });
        if (focusMovesWithPanel) {
          panel?.querySelector("[data-workspace-heading]")?.focus({ preventScroll: true });
        }
      });
    }
  }

  if (workspacePanels.length) {
    const hadHash = Boolean(window.location.hash);
    if (!window.location.hash) {
      window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}#cages`);
    }
    activateWorkspaceTab(workspaceTabFromHash(), hadHash && isWorkspaceTabHash());
    focusAopsError();
    focusAopsResults();

    window.addEventListener("hashchange", () => {
      activateWorkspaceTab(workspaceTabFromHash(), isWorkspaceTabHash());
      focusAopsError();
      focusAopsResults();
    });

    for (const [index, link] of workspaceTabLinks.entries()) {
      link.addEventListener("keydown", (event) => {
        let nextIndex;
        if (event.key === "ArrowRight") nextIndex = (index + 1) % workspaceTabLinks.length;
        else if (event.key === "ArrowLeft") nextIndex = (index - 1 + workspaceTabLinks.length) % workspaceTabLinks.length;
        else if (event.key === "Home") nextIndex = 0;
        else if (event.key === "End") nextIndex = workspaceTabLinks.length - 1;
        else return;

        event.preventDefault();
        const nextLink = workspaceTabLinks[nextIndex];
        window.location.hash = nextLink.dataset.workspaceTabLink || "cages";
        nextLink.focus();
      });
    }
  }

  const cageTable = document.querySelector(".cage-table");
  const interactiveRowTarget = [
    "a",
    "button",
    "input",
    "select",
    "textarea",
    "label",
    "summary",
    "details",
    "form",
    '[role="button"]',
    '[role="link"]',
    '[contenteditable]:not([contenteditable="false"])',
  ].join(", ");

  cageTable?.addEventListener("click", (event) => {
    if (
      !(event instanceof MouseEvent) ||
      event.defaultPrevented ||
      event.button !== 0 ||
      event.metaKey ||
      event.ctrlKey ||
      event.shiftKey ||
      event.altKey
    ) {
      return;
    }

    const target = event.target;
    if (!(target instanceof Element)) return;

    const row = target.closest("tr[data-cage-row-href]");
    if (!(row instanceof HTMLTableRowElement) || !cageTable.contains(row)) return;
    if (target.closest(interactiveRowTarget)) return;

    const selection = window.getSelection();
    if (selection && !selection.isCollapsed) return;

    const href = row.dataset.cageRowHref;
    if (!href) return;

    const destination = new URL(href, window.location.href);
    if (destination.origin !== window.location.origin) return;
    window.location.assign(destination.href);
  });

  function csrfToken() {
    return csrfMeta?.getAttribute("content") || "";
  }

  function showError(message) {
    if (!clientNotice) {
      window.alert(message);
      return;
    }
    clientNotice.dataset.kind = "error";
    clientNotice.textContent = message;
    clientNotice.hidden = false;
    clientNotice.tabIndex = -1;
    clientNotice.focus({ preventScroll: true });
    clientNotice.scrollIntoView({ behavior: reducedMotion.matches ? "auto" : "smooth", block: "center" });
  }

  function errorMessage(payload, fallback) {
    if (!payload || typeof payload !== "object") return fallback;
    if (typeof payload.message === "string" && payload.message.trim()) return payload.message;
    if (typeof payload.error === "string" && payload.error.trim()) return payload.error;
    if (typeof payload.detail === "string" && payload.detail.trim()) return payload.detail;
    if (Array.isArray(payload.detail)) {
      const messages = payload.detail
        .map((item) => (item && typeof item.msg === "string" ? item.msg : ""))
        .filter(Boolean);
      if (messages.length) return messages.join(" ");
    }
    return fallback;
  }

  async function responseError(response) {
    const fallback = `Could not save this change (HTTP ${response.status}).`;
    const contentType = response.headers.get("content-type") || "";
    if (!contentType.toLowerCase().includes("application/json")) return fallback;
    try {
      return errorMessage(await response.json(), fallback);
    } catch {
      return fallback;
    }
  }

  async function postForm(form, formData) {
    const token = csrfToken();
    if (!token) {
      throw new Error("This page is missing its local security token. Reload the page and try again.");
    }
    const response = await fetch(form.action, {
      method: "POST",
      body: formData,
      headers: {
        Accept: "text/html,application/json",
        "X-CSRF-Token": token,
      },
      credentials: "same-origin",
      redirect: "follow",
      cache: "no-store",
    });
    const refreshedToken = response.headers.get("X-CSRF-Token");
    if (csrfMeta && refreshedToken) csrfMeta.setAttribute("content", refreshedToken);
    if (!response.ok) throw new Error(await responseError(response));
    return response;
  }

  function pendingState(form, active, submitter = null) {
    const controls = [...form.querySelectorAll("button, input, select, textarea")];
    for (const control of controls) {
      if (active) {
        control.dataset.wasDisabled = control.disabled ? "true" : "false";
        if (control === submitter) control.setAttribute("aria-disabled", "true");
        else control.disabled = true;
      } else {
        control.disabled = control.dataset.wasDisabled === "true";
        control.removeAttribute("aria-disabled");
        delete control.dataset.wasDisabled;
      }
    }

    if (!(submitter instanceof HTMLButtonElement || submitter instanceof HTMLInputElement)) return;
    if (active) {
      submitter.dataset.originalLabel = submitter.value || submitter.textContent || "";
      const pendingLabel = submitter.dataset.pendingLabel || "Saving…";
      if (submitter instanceof HTMLInputElement) submitter.value = pendingLabel;
      else submitter.textContent = pendingLabel;
      submitter.setAttribute("aria-busy", "true");
    } else {
      const original = submitter.dataset.originalLabel;
      if (original !== undefined) {
        if (submitter instanceof HTMLInputElement) submitter.value = original;
        else submitter.textContent = original;
      }
      delete submitter.dataset.originalLabel;
      submitter.removeAttribute("aria-busy");
    }
  }

  window.addEventListener("pageshow", () => {
    for (const form of document.querySelectorAll('form[data-submitting="true"]')) {
      if (!(form instanceof HTMLFormElement)) continue;
      const submitter = form.querySelector("[data-original-label]");
      delete form.dataset.submitting;
      pendingState(form, false, submitter);
    }
  });

  function animalFormValues(form) {
    return new URLSearchParams(new FormData(form)).toString();
  }

  function dirtyAnimalForms() {
    return animalEditForms.filter(
      (form) => form.dataset.submitting !== "true" && animalFormValues(form) !== savedAnimalValues.get(form),
    );
  }

  function updateAnimalSaveControls() {
    const count = dirtyAnimalForms().length;
    if (saveAllMiceButton) {
      saveAllMiceButton.disabled = savingAllMice || activeAnimalSaves > 0 || count === 0;
    }
    if (mouseSaveSummary) {
      mouseSaveSummary.textContent = savingAllMice || activeAnimalSaves > 0
        ? "Saving mice…"
        : count ? `${count} ${count === 1 ? "mouse has" : "mice have"} unsaved changes` : "";
    }
  }

  async function saveAnimalForm(form, submitter) {
    const formData = new FormData(form);
    const status = form.querySelector("[data-mouse-save-status]");
    form.dataset.submitting = "true";
    pendingState(form, true, submitter);
    activeAnimalSaves += 1;
    updateAnimalSaveControls();
    let saved = false;
    try {
      const response = await postForm(form, formData);
      const target = new URL(response.url || window.location.href, window.location.href);
      if (target.origin !== window.location.origin || target.pathname !== window.location.pathname) {
        throw new Error("The save could not be confirmed. Your edits are still here; check your session before retrying.");
      }
      // Validation failures also redirect to a 200 HTML page, so check the result before marking a draft saved.
      if (target.searchParams.get("kind") === "error") {
        throw new Error(target.searchParams.get("message") || "Could not save this mouse.");
      }
      const savedPage = new DOMParser().parseFromString(await response.text(), "text/html");
      const savedForm = savedPage.getElementById(form.id);
      if (!(savedForm instanceof HTMLFormElement)) {
        throw new Error("The save could not be confirmed. Your edits are still here; try again.");
      }
      for (const control of form.querySelectorAll("input, select, textarea")) {
        control.value = savedForm.elements.namedItem(control.name).value;
      }
      const age = form.closest(".animal-card").querySelector("[data-birth-date]");
      if (age) {
        age.dataset.birthDate = form.elements.namedItem("dob").value;
        age.textContent = ageFromBirthDate(age.dataset.birthDate);
      }
      saved = true;
      if (status) status.textContent = "Saved";
    } catch (error) {
      if (status) status.textContent = "Not saved — your edits are still here";
      throw error;
    } finally {
      delete form.dataset.submitting;
      pendingState(form, false, submitter);
      activeAnimalSaves -= 1;
      if (saved) savedAnimalValues.set(form, animalFormValues(form));
      updateAnimalSaveControls();
    }
  }

  for (const form of animalEditForms) {
    savedAnimalValues.set(form, animalFormValues(form));
    const updateDraft = () => {
      const status = form.querySelector("[data-mouse-save-status]");
      if (status) {
        status.textContent = animalFormValues(form) !== savedAnimalValues.get(form) ? "Unsaved changes" : "";
      }
      updateAnimalSaveControls();
    };
    form.addEventListener("input", updateDraft);
    form.addEventListener("change", updateDraft);
  }
  const mouseSaveActions = document.querySelector("[data-mouse-save-actions]");
  if (mouseSaveActions) mouseSaveActions.hidden = false;
  updateAnimalSaveControls();

  saveAllMiceButton?.addEventListener("click", async () => {
    if (savingAllMice || activeAnimalSaves > 0) return;
    const forms = dirtyAnimalForms();
    if (forms.some((form) => !form.reportValidity())) return;
    savingAllMice = true;
    clientNotice && (clientNotice.hidden = true);
    updateAnimalSaveControls();
    const failures = [];
    for (const form of forms) {
      try {
        await saveAnimalForm(form, form.querySelector('button[type="submit"]'));
      } catch (error) {
        const mouseId = form.closest(".animal-card").querySelector(".animal-identity strong").textContent;
        failures.push(`${mouseId}: ${error instanceof Error ? error.message : "Could not save."}`);
      }
    }
    savingAllMice = false;
    updateAnimalSaveControls();
    if (failures.length) {
      showError(`Saved ${forms.length - failures.length} of ${forms.length} mice. ${failures.join(" ")}`);
    } else if (mouseSaveSummary && dirtyAnimalForms().length === 0) {
      mouseSaveSummary.textContent = `Saved ${forms.length} ${forms.length === 1 ? "mouse" : "mice"}.`;
    }
  });

  if (batchEditForm instanceof HTMLFormElement) {
    for (const property of batchEditForm.querySelectorAll("[data-batch-property]")) {
      const toggle = property.querySelector('input[type="checkbox"]');
      const valueControl = property.querySelector("select, input:not([type=\"checkbox\"])");
      if (!(toggle instanceof HTMLInputElement) || !valueControl) continue;
      valueControl.addEventListener("input", () => {
        toggle.checked = true;
      });
      valueControl.addEventListener("change", () => {
        toggle.checked = true;
      });
    }
  }

  document.addEventListener("submit", async (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement) || form.method.toLowerCase() !== "post") return;

    event.preventDefault();
    if (form.dataset.submitting === "true") return;

    if (form.matches(".edit-animal-form")) {
      if (savingAllMice) return;
      clientNotice && (clientNotice.hidden = true);
      try {
        await saveAnimalForm(form, event.submitter);
      } catch (error) {
        showError(error instanceof Error ? error.message : "Could not save this mouse.");
      }
      return;
    }

    if (
      form.matches("[data-batch-edit-form]") &&
      !form.querySelector('input[type="checkbox"]:checked')
    ) {
      showError("Choose at least one mouse property to update.");
      return;
    }

    const submitter = event.submitter;
    const confirmation = submitter?.dataset.confirm || form.dataset.confirm;
    if (confirmation && !window.confirm(confirmation)) return;
    const returnHash = form.dataset.returnHash || "";

    const formData = new FormData(form);
    if (form.id === "split-form" && formData.getAll("animal_ids").length === 0) {
      showError("Select at least one active mouse before creating the split cage.");
      return;
    }

    clientNotice && (clientNotice.hidden = true);
    form.dataset.submitting = "true";
    pendingState(form, true, submitter);

    try {
      const response = await postForm(form, formData);

      const target = new URL(response.url || window.location.href, window.location.href);
      if (target.origin !== window.location.origin) {
        throw new Error("The server returned an unexpected redirect.");
      }
      if (/^#[A-Za-z][A-Za-z0-9:_.-]*$/.test(returnHash)) target.hash = returnHash;
      const current = new URL(window.location.href);
      if (
        target.origin === current.origin &&
        target.pathname === current.pathname &&
        target.search === current.search
      ) {
        window.history.replaceState(window.history.state, "", target.href);
        window.location.reload();
        return;
      }
      window.location.assign(target.href);
    } catch (error) {
      form.dataset.submitting = "false";
      pendingState(form, false, submitter);
      updateSelection();
      showError(error instanceof Error ? error.message : "Could not save this change.");
    }
  });

  function updateSelection() {
    if (!splitForm || !selectionCount) return;
    const selected = animalSelectors.filter((checkbox) => checkbox.checked);
    selectionCount.textContent = `${selected.length} ${selected.length === 1 ? "mouse" : "mice"} selected`;
    for (const checkbox of animalSelectors) {
      checkbox.closest(".animal-card")?.classList.toggle("is-selected", checkbox.checked);
    }
  }

  for (const checkbox of animalSelectors) checkbox.addEventListener("change", updateSelection);
  updateSelection();

  function ageFromBirthDate(value) {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return "Unknown";
    const [year, month, day] = value.split("-").map(Number);
    const birth = Date.UTC(year, month - 1, day);
    const now = new Date();
    const today = Date.UTC(now.getFullYear(), now.getMonth(), now.getDate());
    const days = Math.floor((today - birth) / 86400000);
    if (!Number.isFinite(days) || days < 0) return "Unknown";
    if (days < 14) return `${days} ${days === 1 ? "day" : "days"}`;
    if (days < 70) {
      const weeks = Math.floor(days / 7);
      return `${weeks} ${weeks === 1 ? "week" : "weeks"}`;
    }
    if (days < 730) {
      const months = Math.floor(days / 30.4375);
      return `${months} ${months === 1 ? "month" : "months"}`;
    }
    const years = Math.floor(days / 365.2425);
    const months = Math.floor((days - years * 365.2425) / 30.4375);
    return months > 0 ? `${years}y ${months}m` : `${years} ${years === 1 ? "year" : "years"}`;
  }

  for (const element of document.querySelectorAll("[data-birth-date]")) {
    element.textContent = ageFromBirthDate(element.dataset.birthDate || "");
  }

  function focusDeepLinkTarget() {
    const targetId = window.location.hash.slice(1);
    if (!/^(cage-details|cage-actions|mice|mouse-\d+)$/.test(targetId)) return;
    const target = document.getElementById(targetId);
    if (!target) return;
    target.tabIndex = -1;
    target.focus({ preventScroll: true });
  }

  if (!workspacePanels.length) {
    window.requestAnimationFrame(focusDeepLinkTarget);
    window.addEventListener("hashchange", () => window.requestAnimationFrame(focusDeepLinkTarget));
  }
})();
