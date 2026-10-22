// What a finished run says in the browser notification and the tab-title mark.
// Loaded by the web app's pages.

// What the notification and tab-title mark say when a run ends; null when there
// is nothing to announce (nothing ran, or the user pressed Stop themselves).
function runAnnouncement(target, keys, results, metrics) {
  if (!results.length) return null;
  const stopped = results.some((r) => r.status === "cancelled") || results.length < keys.length;
  if (stopped) return null;
  const title = (key) => (metrics.find((m) => m.key === key) || {}).title || key;
  const passed = results.filter((r) => r.status === "pass");
  const failed = results.filter((r) => r.status === "fail");
  const errors = results.filter((r) => r.status === "error");
  const parts = [];
  if (passed.length) parts.push(`${passed.length} passed`);
  if (failed.length) parts.push(`${failed.length} failed (${failed.map((r) => title(r.key)).join(", ")})`);
  if (errors.length) parts.push(`${errors.length} could not run (${errors.map((r) => title(r.key)).join(", ")})`);
  let message = `${target.name}: ${parts.join(" · ")}`;
  // One metric that could not run: say why (a service message, an expired session...).
  if (errors.length === 1 && results.length === 1) message += `. ${errors[0].error || ""}`;
  const outcome = failed.length ? "fail" : errors.length ? "error" : "pass";
  return {
    outcome,
    badge: failed.length ? String(failed.length) : errors.length ? "!" : "✓",
    title: `LLM Judge · ${results.length === 1 ? title(results[0].key) : "run"} ${outcome === "pass" ? "passed" : "finished"}`,
    message: message.length > 300 ? `${message.slice(0, 297)}…` : message,
  };
}

// What a finished job says: its results, why it failed, or that it was cut off.
function jobAnnouncement(job, target, metrics) {
  if (job.status === "interrupted") {
    return { outcome: "error", badge: "!", title: "LLM Judge · run interrupted",
             message: `${target.name}: the backend stopped before this run finished; run it again.` };
  }
  if (job.status === "error") {
    return { outcome: "error", badge: "!", title: "LLM Judge · run failed",
             message: `${target.name}: ${job.error || "the run could not start"}` };
  }
  if (job.status !== "done") return null;  // cancelled: the user stopped it
  return runAnnouncement(target, job.metric_keys, job.results, metrics);
}
