(() => {
  "use strict";

  const csrfMeta = document.querySelector('meta[name="csrf-token"]');
  const clientNotice = document.querySelector("[data-client-notice]");
  const splitForm = document.querySelector("#split-form");
  const animalSelectors = [...document.querySelectorAll(".animal-selector")];
  const selectionCount = document.querySelector("[data-selection-count]");
  const workspacePanels = [...document.querySelectorAll("[data-workspace-panel]")];
  const workspaceTabLinks = [...document.querySelectorAll("[data-workspace-tab-link]")];
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

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

    window.addEventListener("hashchange", () => {
      activateWorkspaceTab(workspaceTabFromHash(), isWorkspaceTabHash());
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

  document.addEventListener("submit", async (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement) || form.method.toLowerCase() !== "post") return;

    event.preventDefault();
    if (form.dataset.submitting === "true") return;

    const submitter = event.submitter;
    const confirmation = submitter?.dataset.confirm || form.dataset.confirm;
    if (confirmation && !window.confirm(confirmation)) return;
    const returnHash = form.dataset.returnHash || "";

    const token = csrfToken();
    if (!token) {
      showError("This page is missing its local security token. Reload the page and try again.");
      return;
    }

    const formData = new FormData(form);
    if (form.id === "split-form" && formData.getAll("animal_ids").length === 0) {
      showError("Select at least one active mouse before creating the split cage.");
      return;
    }

    clientNotice && (clientNotice.hidden = true);
    form.dataset.submitting = "true";
    pendingState(form, true, submitter);

    try {
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

      const target = new URL(response.url || window.location.href, window.location.href);
      if (target.origin !== window.location.origin) {
        throw new Error("The server returned an unexpected redirect.");
      }
      if (/^#[A-Za-z][A-Za-z0-9:_.-]*$/.test(returnHash)) target.hash = returnHash;
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
