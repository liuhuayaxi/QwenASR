/* Shared light/dark theme preference for every QwenASR page. */
(() => {
  const storageKey = "qwenasr-theme";
  const media = window.matchMedia?.("(prefers-color-scheme: dark)");

  function readTheme() {
    try {
      const value = window.localStorage.getItem(storageKey);
      return value === "light" || value === "dark" ? value : "";
    } catch {
      return "";
    }
  }

  const systemTheme = () => media?.matches ? "dark" : "light";
  const preferredTheme = readTheme() || systemTheme();
  document.documentElement.dataset.theme = preferredTheme;
  document.documentElement.style.colorScheme = preferredTheme;

  function setMetaThemeColor(theme) {
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.content = theme === "dark" ? "#0c0d0f" : "#f6f7f8";
  }

  function updateToggle(theme) {
    document.querySelectorAll("[data-theme-toggle]").forEach((button) => {
      const next = theme === "dark" ? "light" : "dark";
      button.dataset.theme = theme;
      button.setAttribute("aria-pressed", theme === "dark" ? "true" : "false");
      button.setAttribute("aria-label", next === "dark" ? "切换深色模式" : "切换浅色模式");
      button.title = next === "dark" ? "切换深色模式" : "切换浅色模式";
      const icon = button.querySelector("[data-theme-icon]");
      const label = button.querySelector("[data-theme-label]");
      if (icon) icon.textContent = theme === "dark" ? "☀" : "☾";
      if (label) label.textContent = theme === "dark" ? "浅色" : "深色";
    });
  }

  function applyTheme(theme, persist = true) {
    const next = theme === "dark" ? "dark" : "light";
    document.documentElement.dataset.theme = next;
    document.documentElement.style.colorScheme = next;
    if (persist) {
      try { window.localStorage.setItem(storageKey, next); } catch { /* private browsing */ }
    }
    setMetaThemeColor(next);
    updateToggle(next);
  }

  function setup() {
    applyTheme(readTheme() || systemTheme(), false);
    document.querySelectorAll("[data-theme-toggle]").forEach((button) => {
      button.addEventListener("click", () => {
        applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
      });
    });
    media?.addEventListener?.("change", () => {
      if (!readTheme()) applyTheme(systemTheme(), false);
    });
    window.addEventListener("storage", (event) => {
      if (event.key === storageKey || event.key === null) applyTheme(readTheme() || systemTheme(), false);
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", setup, { once: true });
  else setup();
})();
