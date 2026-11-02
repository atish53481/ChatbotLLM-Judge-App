// Web app shell: drag the bar between the side panel and the dashboard to
// resize the panel. The width is kept per browser; double-click restores it.
(function () {
  const KEY = "llmJudge:panelWidth";
  const DEFAULT = 420;
  const MIN = 300;
  const panel = document.getElementById("panel");
  const splitter = document.getElementById("splitter");
  // Leave the dashboard room to show its cards.
  const max = () => Math.max(MIN, window.innerWidth - 360);

  function apply(width, save) {
    const px = Math.round(Math.min(max(), Math.max(MIN, width)));
    panel.style.width = `${px}px`;
    splitter.setAttribute("aria-valuenow", String(px));
    if (!save) return;
    try {
      localStorage.setItem(KEY, String(px));
    } catch {
      // Storage blocked: the width lasts until the page reloads.
    }
  }

  let saved = null;
  try {
    saved = Number(localStorage.getItem(KEY)) || null;
  } catch {
    saved = null;
  }
  apply(saved || DEFAULT, false);

  const resizing = () => document.body.classList.contains("resizing");
  splitter.addEventListener("pointerdown", (event) => {
    splitter.setPointerCapture(event.pointerId);
    document.body.classList.add("resizing");
  });
  splitter.addEventListener("pointermove", (event) => {
    if (resizing()) apply(event.clientX, false);
  });
  const stop = () => {
    if (!resizing()) return;
    document.body.classList.remove("resizing");
    apply(panel.getBoundingClientRect().width, true);
  };
  splitter.addEventListener("pointerup", stop);
  splitter.addEventListener("pointercancel", stop);
  splitter.addEventListener("dblclick", () => apply(DEFAULT, true));
  splitter.addEventListener("keydown", (event) => {
    const step = event.shiftKey ? 80 : 20;
    const width = panel.getBoundingClientRect().width;
    if (event.key === "ArrowLeft") apply(width - step, true);
    else if (event.key === "ArrowRight") apply(width + step, true);
    else return;
    event.preventDefault();
  });
  window.addEventListener("resize", () => apply(panel.getBoundingClientRect().width, false));
})();
