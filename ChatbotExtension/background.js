// Service worker: opens the side panel from the toolbar button, opens the
// dashboard tab, and tells the user when a run finishes (a notification plus a
// badge on the toolbar icon), so a long run can be left in another tab.

importScripts("lib/announce.js");

const BACKEND_URL = "http://127.0.0.1:8765";
const DASHBOARD_URL = chrome.runtime.getURL("dashboard/dashboard.html");
const BADGE_COLORS = { pass: "#0c7a0c", fail: "#c62828", error: "#b36b00" };

chrome.sidePanel
  .setPanelBehavior({ openPanelOnActionClick: true })
  .catch((error) => console.error("LLM Judge: side panel setup failed", error));

// Focuses an open dashboard tab, or opens one. getContexts finds this
// extension's own pages without the "tabs" permission.
async function showDashboard() {
  const [page] = await chrome.runtime.getContexts({ contextTypes: ["TAB"], documentUrls: [DASHBOARD_URL] });
  if (page && page.tabId >= 0) {
    await chrome.tabs.update(page.tabId, { active: true });
    await chrome.windows.update(page.windowId, { focused: true });
  } else {
    await chrome.tabs.create({ url: DASHBOARD_URL });
  }
}

// summary: {outcome: "pass" | "fail" | "error", badge, title, message}, built by
// jobAnnouncement in lib/announce.js from a finished job.
async function runFinished(summary) {
  await chrome.action.setBadgeText({ text: summary.badge || "" });
  await chrome.action.setBadgeBackgroundColor({ color: BADGE_COLORS[summary.outcome] || BADGE_COLORS.error });
  await chrome.notifications.create(`run-${Date.now()}`, {
    type: "basic",
    iconUrl: chrome.runtime.getURL("icons/icon-128.png"),
    title: summary.title,
    message: summary.message,
    priority: summary.outcome === "pass" ? 0 : 1,
  });
}

// Jobs this browser started and has not announced yet: {jobId: {target, metrics}}.
// Kept in session storage because the worker itself can be stopped between checks.
async function watched() {
  return (await chrome.storage.session.get("watchedJobs")).watchedJobs || {};
}

// Checks and new watches take turns on one chain: a job is never announced
// twice, and a job added while a check waits on the backend is never lost.
let checking = Promise.resolve();
function inTurn(step, what) {
  checking = checking.then(step).catch((error) => console.error(`LLM Judge: ${what} failed`, error));
  return checking;
}

function checkJobs() {
  return inTurn(checkJobsNow, "job check");
}

function watchJob(jobId, target, metrics) {
  return inTurn(async () => {
    const jobs = await watched();
    jobs[jobId] = { target, metrics };
    await chrome.storage.session.set({ watchedJobs: jobs });
    await chrome.alarms.create("watch-jobs", { periodInMinutes: 0.5 });
  }, "watching the run");
}

async function checkJobsNow() {
  const jobs = await watched();
  for (const [jobId, info] of Object.entries(jobs)) {
    let job;
    try {
      const response = await fetch(`${BACKEND_URL}/api/jobs/${jobId}`);
      if (response.status === 404) { delete jobs[jobId]; continue; }
      job = await response.json();
    } catch {
      continue;  // backend unreachable: try again at the next alarm
    }
    if (job.status === "queued" || job.status === "running") continue;
    delete jobs[jobId];
    const summary = jobAnnouncement(job, info.target, info.metrics);
    if (summary) await runFinished(summary);
  }
  await chrome.storage.session.set({ watchedJobs: jobs });
  if (!Object.keys(jobs).length) await chrome.alarms.clear("watch-jobs");
}

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === "watch-jobs") checkJobs();
});

chrome.notifications.onClicked.addListener((id) => {
  if (!id.startsWith("run-")) return;
  chrome.notifications.clear(id);
  showDashboard().catch((error) => console.error("LLM Judge: could not open the dashboard", error));
});

chrome.runtime.onMessage.addListener((message, sender) => {
  // Only this extension's own pages may use these.
  if (sender.id !== chrome.runtime.id) return false;

  if (message.type === "OPEN_DASHBOARD") {
    chrome.tabs.create({ url: DASHBOARD_URL });
  } else if (message.type === "WATCH_JOB" && message.jobId) {
    watchJob(message.jobId, message.target, message.metrics || []);
  } else if (message.type === "JOB_ENDED") {
    checkJobs();
  } else if (message.type === "CLEAR_BADGE") {
    chrome.action.setBadgeText({ text: "" });
  }
  return false;
});
