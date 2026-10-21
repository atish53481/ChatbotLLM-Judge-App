"""Summary statistics for one run's case scores: the average alone hides a
run where half the cases score 1.0 and half 0.0."""
from __future__ import annotations

import statistics

HISTOGRAM_BINS = 10


def summary_stats(scores: list[float | None], passed: list[bool] | None = None) -> dict | None:
    """n, mean, median, min, max, population std, a 10-bin histogram over
    [0, 1] and the share of cases that passed; None when nothing was scored."""
    values = [float(s) for s in scores if s is not None]
    if not values:
        return None
    bins = [0] * HISTOGRAM_BINS
    for v in values:
        bins[min(HISTOGRAM_BINS - 1, max(0, int(v * HISTOGRAM_BINS)))] += 1
    stats = {
        "n": len(values),
        "mean": round(statistics.fmean(values), 4),
        "median": round(statistics.median(values), 4),
        "min": round(min(values), 4),
        "max": round(max(values), 4),
        "std": round(statistics.pstdev(values), 4),
        "histogram": bins,
    }
    if passed is not None and passed:
        stats["pass_rate"] = round(sum(1 for p in passed if p) / len(passed), 4)
    return stats


def percentile(values: list[float], pct: float) -> float | None:
    """Nearest-rank percentile (pct in 0..100); None for no values."""
    ordered = sorted(v for v in values if v is not None)
    if not ordered:
        return None
    rank = max(1, min(len(ordered), round(pct / 100 * len(ordered) + 0.5)))
    return round(ordered[rank - 1], 4)
