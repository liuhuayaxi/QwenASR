/* Shared paper, sepia and dark appearance for every QwenASR page. */
(() => {
  const storageKey = "qwenasr-theme";
  const themes = {
    light: { label: "白纸", icon: "☀", next: "sepia", color: "#f7f8f5" },
    sepia: { label: "暖纸", icon: "◐", next: "dark", color: "#f1ede2" },
    dark: { label: "深色", icon: "☾", next: "light", color: "#000000" },
  };
  const root = document.documentElement;
  const media = window.matchMedia?.("(prefers-color-scheme: dark)");

  function readTheme() {
    try {
      const value = window.localStorage.getItem(storageKey);
      return Object.hasOwn(themes, value) ? value : "";
    } catch {
      return "";
    }
  }

  const systemTheme = () => media?.matches ? "dark" : "light";
  root.dataset.theme = readTheme() || systemTheme();

  function setMetaThemeColor(theme) {
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.content = themes[theme].color;
  }

  function updateToggle(theme) {
    document.querySelectorAll("[data-theme-toggle]").forEach((button) => {
      const { label, icon, next } = themes[theme];
      button.dataset.theme = theme;
      button.title = `当前${label}，切换至${themes[next].label}`;
      button.setAttribute("aria-label", button.title);
      const iconElement = button.querySelector("[data-theme-icon]");
      const labelElement = button.querySelector("[data-theme-label]");
      if (iconElement) iconElement.textContent = icon;
      if (labelElement) labelElement.textContent = label;
    });
  }

  function applyTheme(theme, persist = true) {
    root.dataset.theme = theme;
    if (persist) {
      try { window.localStorage.setItem(storageKey, theme); } catch { /* private browsing */ }
    }
    setMetaThemeColor(theme);
    updateToggle(theme);
  }

  function setupGlassLight() {
    const pointer = matchMedia("(hover: hover) and (pointer: fine)");
    const reduced = matchMedia("(prefers-reduced-motion: reduce), (prefers-reduced-transparency: reduce), (prefers-contrast: more), (forced-colors: active)");
    const glassBars = ".app-header, .pv-bar, .note-dialog-header, .course-sidebar, .note-toc";
    let frame = 0;
    let surface, x, y;

    // One update per frame while moving across glass. Idle bars remain static.
    document.addEventListener("pointermove", (event) => {
      if (!pointer.matches || reduced.matches || event.pointerType === "touch") return;
      surface = event.target.closest?.(glassBars);
      if (!surface) return;
      x = event.clientX;
      y = event.clientY;
      if (frame) return;
      frame = requestAnimationFrame(() => {
        frame = 0;
        if (!surface?.isConnected) return;
        const bounds = surface.getBoundingClientRect();
        surface.style.setProperty("--glass-x", `${x - bounds.left}px`);
        surface.style.setProperty("--glass-y", `${y - bounds.top}px`);
      });
    }, { passive: true });
  }

  function setup() {
    applyTheme(readTheme() || systemTheme(), false);
    document.querySelectorAll("[data-theme-toggle]").forEach((button) => {
      button.addEventListener("click", () => {
        applyTheme(themes[root.dataset.theme].next);
      });
    });
    media?.addEventListener?.("change", () => {
      if (!readTheme()) applyTheme(systemTheme(), false);
    });
    window.addEventListener("storage", (event) => {
      if (event.key === storageKey || event.key === null) applyTheme(readTheme() || systemTheme(), false);
    });
    setupGlassLight();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", setup, { once: true });
  else setup();
})();
