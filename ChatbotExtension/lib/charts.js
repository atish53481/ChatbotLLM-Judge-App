// Chart builders shared by the side panel and the dashboard (Chart.js 4).
// Every metric scores 0..1. Most are higher-is-better (threshold = minimum);
// violation-rate metrics are lower-is-better (threshold = maximum).
// Colors come from lib/theme.css at draw time, so light and dark mode swap in
// one place. The series palette passed the dataviz palette validator; in light
// mode aqua, yellow and magenta sit below 3:1 on the surface, so every chart
// keeps a legend and a table view.

Chart.defaults.font.family = 'system-ui, -apple-system, "Segoe UI", sans-serif';
Chart.defaults.font.size = 12;

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function withAlpha(hex, alpha) {
  const value = hex.replace("#", "");
  const [r, g, b] = [0, 2, 4].map((i) => parseInt(value.slice(i, i + 2), 16));
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

function metricColor(metricKey, catalog) {
  // A metric keeps its color everywhere: the color follows its catalog
  // position, never its position in a filtered list.
  const index = Math.max(0, catalog.findIndex((m) => m.key === metricKey));
  return cssVar(`--series-${(index % 7) + 1}`);
}

function chartInk() {
  return {
    surface: cssVar("--surface-1"),
    raised: cssVar("--surface-raised"),
    primary: cssVar("--text-primary"),
    secondary: cssVar("--text-secondary"),
    muted: cssVar("--text-muted"),
    grid: cssVar("--grid"),
    axis: cssVar("--axis"),
    border: cssVar("--border"),
  };
}

function sharedOptions(ink, legendKeyHeight) {
  return {
    responsive: true,
    maintainAspectRatio: false,
    animation: false,
    color: ink.secondary,
    plugins: {
      legend: {
        position: "top",
        align: "start",
        labels: { color: ink.secondary, boxWidth: 14, boxHeight: legendKeyHeight },
      },
      tooltip: {
        backgroundColor: ink.raised,
        titleColor: ink.secondary,
        bodyColor: ink.primary,
        borderColor: ink.border,
        borderWidth: 1,
        padding: 8,
        boxWidth: 12,
        boxHeight: 2,
      },
    },
  };
}

// Value + pass/fail at each bar tip: text ink for the words, the status color
// only on the ✓/✕ icon.
const scoreLabels = {
  id: "scoreLabels",
  afterDatasetsDraw(chart, _args, options) {
    const rows = options.rows;
    const meta = chart.getDatasetMeta(0);
    if (!rows || meta.hidden) return;
    const { ctx } = chart;
    ctx.save();
    ctx.font = `12px ${Chart.defaults.font.family}`;
    ctx.textBaseline = "middle";
    meta.data.forEach((bar, i) => {
      const row = rows[i];
      if (!row) return;
      const x = bar.x + 6;
      const text = `${formatScore(row.score)} ${row.passed ? "pass" : "fail"}`;
      // A surface-colored plate keeps the label readable where the dashed
      // threshold line crosses it.
      ctx.fillStyle = options.surfaceColor;
      ctx.fillRect(x - 3, bar.y - 9, 20 + ctx.measureText(text).width, 18);
      ctx.fillStyle = row.passed ? options.goodColor : options.criticalColor;
      ctx.fillText(row.passed ? "✓" : "✕", x, bar.y);
      ctx.fillStyle = options.textColor;
      ctx.fillText(text, x + 14, bar.y);
    });
    ctx.restore();
  },
};

// Vertical hairline at the hovered run on the trend chart.
const crosshair = {
  id: "crosshair",
  afterDatasetsDraw(chart, _args, options) {
    const active = chart.tooltip ? chart.tooltip.getActiveElements() : [];
    if (!active.length) return;
    const x = active[0].element.x;
    const { top, bottom } = chart.chartArea;
    const { ctx } = chart;
    ctx.save();
    ctx.strokeStyle = options.color;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(x, top);
    ctx.lineTo(x, bottom);
    ctx.stroke();
    ctx.restore();
  },
};

// rows: /api/runs/latest rows ({metric_key, score, passed, ts});
// catalog: /api/metrics; kind: "bar" | "radar".
function latestChartConfig(rows, catalog, kind) {
  const ink = chartInk();
  const byKey = new Map(catalog.map((m) => [m.key, m]));
  // Catalog order keeps each metric in the same place from run to run.
  const ordered = catalog
    .map((m) => rows.find((r) => r.metric_key === m.key))
    .filter(Boolean);
  const labels = ordered.map((r) => byKey.get(r.metric_key).title);
  const scores = ordered.map((r) => r.score);
  const thresholds = ordered.map((r) => byKey.get(r.metric_key).threshold);
  const accent = cssVar("--series-1");
  const threshold = {
    label: "Threshold",
    data: thresholds,
    borderColor: ink.secondary,
    borderWidth: 1,
    borderDash: [4, 4],
    pointRadius: 0,
    pointHitRadius: 12,
    fill: false,
  };
  const tooltipLabel = (item) => {
    if (item.datasetIndex === 1) return ` minimum ${formatScore(item.raw)}`;
    const row = ordered[item.dataIndex];
    return ` ${formatScore(item.raw)} · ${row.passed ? "pass" : "fail"} · ${formatRunTime(row.ts)}`;
  };

  if (kind === "radar") {
    const options = sharedOptions(ink, 2);
    options.plugins.tooltip.callbacks = { label: tooltipLabel };
    options.scales = {
      r: {
        min: 0,
        max: 1,
        ticks: { stepSize: 0.2, color: ink.muted, backdropColor: ink.surface, backdropPadding: 2 },
        grid: { color: ink.grid },
        angleLines: { color: ink.grid },
        pointLabels: { color: ink.secondary },
      },
    };
    return {
      type: "radar",
      data: {
        labels,
        datasets: [
          {
            label: "Score",
            data: scores,
            borderColor: accent,
            backgroundColor: withAlpha(accent, 0.1),
            borderWidth: 2,
            fill: true,
            pointRadius: 4,
            pointHoverRadius: 6,
            pointHitRadius: 12,
            pointBackgroundColor: accent,
            pointBorderColor: ink.surface,
            pointBorderWidth: 2,
          },
          threshold,
        ],
      },
      options,
    };
  }

  const options = sharedOptions(ink, 8);
  options.indexAxis = "y";
  options.layout = { padding: { right: 84 } };
  options.plugins.tooltip.callbacks = { label: tooltipLabel };
  options.plugins.scoreLabels = {
    rows: ordered,
    surfaceColor: ink.surface,
    textColor: ink.secondary,
    goodColor: cssVar("--status-good"),
    criticalColor: cssVar("--status-critical"),
  };
  options.scales = {
    x: {
      min: 0,
      max: 1,
      ticks: { stepSize: 0.2, color: ink.muted },
      grid: { color: ink.grid },
      border: { color: ink.axis },
    },
    y: {
      ticks: { color: ink.secondary },
      grid: { display: false },
      border: { color: ink.axis },
    },
  };
  return {
    type: "bar",
    data: {
      labels,
      datasets: [
        {
          label: "Score",
          data: scores,
          backgroundColor: accent,
          hoverBackgroundColor: cssVar("--series-1-hover"),
          maxBarThickness: 24,
          borderSkipped: false,
          borderRadius: { topLeft: 0, bottomLeft: 0, topRight: 4, bottomRight: 4 },
        },
        { ...threshold, type: "line", indexAxis: "y" },
      ],
    },
    options,
    plugins: [scoreLabels],
  };
}

// Runs started within this long of each other are one run of the chatbot:
// the metrics of one Run finish seconds apart, and should share an x position.
const RUN_BATCH_GAP_MS = 15 * 60 * 1000;
// More lines than this are unreadable: the chart then shows one per category.
const MAX_TREND_LINES = 8;
// Colors for categories past the seven series colors.
const EXTRA_COLORS = ["#2bb3c0", "#a3785a", "#9aa0a6"];

// Groups run timestamps into batches; returns the batch start labels and a
// lookup from each timestamp to its batch index.
function runBatches(times) {
  const sorted = [...new Set(times)].sort();
  const starts = [];
  const indexOf = new Map();
  let last = null;
  for (const ts of sorted) {
    const t = new Date(ts).getTime();
    if (last === null || t - last > RUN_BATCH_GAP_MS) starts.push(ts);
    indexOf.set(ts, starts.length - 1);
    last = t;
  }
  return { starts, indexOf };
}

// A metric's latest score in each batch (null where it did not run).
function scoresByBatch(history, batches) {
  const out = batches.starts.map(() => null);
  for (const r of [...history].sort((a, b) => (a.ts < b.ts ? -1 : 1))) out[batches.indexOf.get(r.ts)] = r.score;
  return out;
}

function lineStyle(color, ink, fillArea) {
  return {
    borderColor: color,
    backgroundColor: fillArea ? withAlpha(color, 0.1) : color,
    fill: fillArea ? "origin" : false,
    clip: 6, // keeps markers at a score of 1.0 whole at the top edge
    borderWidth: 2,
    borderCapStyle: "round",
    borderJoinStyle: "round",
    spanGaps: true,
    pointRadius: 4,
    pointHoverRadius: 6,
    pointHitRadius: 12,
    pointBackgroundColor: color,
    pointBorderColor: ink.surface,
    pointBorderWidth: 2,
  };
}

// One line per category: the average score of its metrics in each run, with
// lower-is-better metrics counted as 1 − score so every line reads "up is good".
function categoryDatasets(shown, historyByMetric, batches, ink) {
  const groups = new Map();
  for (const m of shown) {
    const key = m.ui_category || m.group || "other";
    if (!groups.has(key)) groups.set(key, { label: m.ui_category_label || key, metrics: [] });
    groups.get(key).metrics.push(m);
  }
  return [...groups.values()].map((g, i) => {
    const series = g.metrics.map((m) => scoresByBatch(historyByMetric[m.key], batches)
      .map((s) => (s == null ? null : m.direction === "lower" ? 1 - s : s)));
    const data = batches.starts.map((_, b) => {
      const values = series.map((s) => s[b]).filter((v) => v != null);
      return values.length ? values.reduce((a, v) => a + v, 0) / values.length : null;
    });
    const color = i < 7 ? cssVar(`--series-${i + 1}`) : EXTRA_COLORS[(i - 7) % EXTRA_COLORS.length];
    return { label: `${g.label} (${g.metrics.length})`, data, metricTitles: g.metrics.map((m) => m.title),
      ...lineStyle(color, ink, false) };
  });
}

// historyByMetric: { metricKey: /api/history rows } for the metrics to show;
// catalog: /api/metrics (for stable colors and thresholds).
function trendChartConfig(historyByMetric, catalog) {
  const ink = chartInk();
  const shown = catalog.filter((m) => (historyByMetric[m.key] || []).length > 0);
  const batches = runBatches(shown.flatMap((m) => historyByMetric[m.key].map((r) => r.ts)));
  const times = batches.starts;
  const byCategory = shown.length > MAX_TREND_LINES;
  const single = shown.length === 1;
  const datasets = byCategory ? categoryDatasets(shown, historyByMetric, batches, ink) : shown.map((m) => {
    const color = metricColor(m.key, catalog);
    return {
      label: m.title,
      data: scoresByBatch(historyByMetric[m.key], batches),
      borderColor: color,
      backgroundColor: single ? withAlpha(color, 0.1) : color,
      fill: single ? "origin" : false,
      clip: 6, // keeps markers at a score of 1.0 whole at the top edge
      borderWidth: 2,
      borderCapStyle: "round",
      borderJoinStyle: "round",
      spanGaps: true,
      pointRadius: 4,
      pointHoverRadius: 6,
      pointHitRadius: 12,
      pointBackgroundColor: color,
      pointBorderColor: ink.surface,
      pointBorderWidth: 2,
    };
  });
  if (single) {
    datasets.push({
      label: "Threshold",
      data: times.map(() => shown[0].threshold),
      borderColor: ink.secondary,
      borderWidth: 1,
      borderDash: [4, 4],
      pointRadius: 0,
      pointHitRadius: 0,
      fill: false,
    });
  }
  if (times.length === 1 && datasets.length > 1) return oneRunBars(datasets, times[0], ink, byCategory);
  const options = sharedOptions(ink, 2);
  options.interaction = { mode: "index", intersect: false };
  options.plugins.tooltip.callbacks = {
    title: (items) => (items.length ? `Run of ${items[0].label}` : ""),
    label: (item) => ` ${formatScore(item.raw)}  ${item.dataset.label}`,
    // Category lines name the metrics they average.
    afterBody: (items) => (byCategory && items.length === 1 && items[0].dataset.metricTitles
      ? [`Average of: ${items[0].dataset.metricTitles.join(", ")}`] : []),
  };
  options.plugins.legend.labels.usePointStyle = true;
  options.plugins.legend.labels.pointStyle = "line";
  options.plugins.subtitle = {
    display: byCategory,
    text: "One line per category: average score of its metrics in each run (lower-is-better counted as 1 − score). Pick one metric for its own line.",
    color: ink.muted, align: "start", padding: { bottom: 6 }, font: { size: 11 },
  };
  options.plugins.crosshair = { color: ink.axis };
  options.scales = {
    x: {
      ticks: { color: ink.muted, maxRotation: 0, autoSkip: true },
      grid: { display: false },
      border: { color: ink.axis },
    },
    y: {
      min: 0,
      max: 1,
      ticks: { stepSize: 0.2, color: ink.muted },
      grid: { color: ink.grid },
      border: { color: ink.axis },
    },
  };
  return {
    type: "line",
    data: { labels: times.map(formatRunTime), datasets },
    options,
    plugins: [crosshair],
  };
}

// A trend needs two runs: with one, each line's single score is a bar instead
// of a column of dots at the chart's edge.
function oneRunBars(datasets, ts, ink, byCategory) {
  const lines = datasets.filter((d) => d.label !== "Threshold" && d.data[0] != null);
  const options = sharedOptions(ink, 8);
  options.plugins.legend.display = false;
  options.plugins.subtitle = {
    display: true, color: ink.muted, align: "start", padding: { bottom: 8 }, font: { size: 11 },
    text: `One run so far (${formatRunTime(ts)}): run again to see the trend.`
      + (byCategory ? " Bars average each category's metrics (lower-is-better counted as 1 − score)." : ""),
  };
  options.plugins.tooltip.callbacks = {
    label: (item) => ` ${formatScore(item.raw)}`,
    afterBody: (items) => {
      const titles = items.length ? lines[items[0].dataIndex].metricTitles : null;
      return titles ? [`Average of: ${titles.join(", ")}`] : [];
    },
  };
  options.scales = {
    x: { ticks: { color: ink.secondary, maxRotation: 0, autoSkip: false }, grid: { display: false }, border: { color: ink.axis } },
    y: { min: 0, max: 1, ticks: { stepSize: 0.2, color: ink.muted }, grid: { color: ink.grid }, border: { color: ink.axis } },
  };
  return {
    type: "bar",
    data: {
      labels: lines.map((d) => d.label),
      datasets: [{
        label: "Score",
        data: lines.map((d) => d.data[0]),
        backgroundColor: lines.map((d) => d.borderColor),
        maxBarThickness: 40,
        borderRadius: { topLeft: 4, topRight: 4, bottomLeft: 0, bottomRight: 0 },
        borderSkipped: false,
      }],
    },
    options,
  };
}

function drawChart(canvas, previous, config) {
  if (previous) previous.destroy();
  return new Chart(canvas, config);
}
