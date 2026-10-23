// Dark (default) or light look for every page. Loaded in <head> before the
// stylesheets paint; the choice lives in localStorage (llmJudge:theme) and the
// storage event keeps the side panel, the dashboard and open tabs in step.
// Any button.theme-toggle flips it.
(function () {
  const KEY = "llmJudge:theme";
  const read = () => {
    try {
      return JSON.parse(localStorage.getItem(KEY)) === "light" ? "light" : "dark";
    } catch {
      return "dark";
    }
  };
  const label = (mode) => (mode === "dark" ? "☀ Light" : "☾ Dark");
  const apply = (mode) => {
    document.documentElement.dataset.theme = mode;
    document.querySelectorAll(".theme-toggle").forEach((button) => {
      button.textContent = label(mode);
      button.title = `Switch to the ${mode === "dark" ? "light" : "dark"} look`;
    });
  };
  apply(read());
  window.addEventListener("storage", (event) => {
    if (event.key === KEY) apply(read());
  });
  document.addEventListener("DOMContentLoaded", () => apply(read()));
  document.addEventListener("click", (event) => {
    const button = event.target.closest && event.target.closest(".theme-toggle");
    if (!button) return;
    const next = read() === "dark" ? "light" : "dark";
    try {
      localStorage.setItem(KEY, JSON.stringify(next));
    } catch {
      // Private window: the look still changes for this page.
    }
    apply(next);
  });
})();
