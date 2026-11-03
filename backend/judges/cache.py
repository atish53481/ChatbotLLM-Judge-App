"""Judge result cache to avoid calling the LLM for identical evaluations.

Key = normalized (test_case, metric_criteria, judge_model).
Per-job in-memory cache (cleared when job ends). Optional persistence per run.

This is additive and off by default (new run option `cache_judge_results`).
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from backend import usage


class JudgeCache:
    def __init__(self):
        self._cache: dict[str, dict] = {}
        self._hits = 0
        self._misses = 0

    def key(self, test_case: dict, metric: dict, judge_model: str) -> str:
        """Deterministic key for identical evaluation."""
        data = {
            "input": test_case.get("input"),
            "actual_output": test_case.get("actual_output"),
            "expected_output": test_case.get("expected_output"),
            "context": test_case.get("context"),
            "tools_called": test_case.get("tools_called"),
            "criteria": metric.get("criteria"),
            "evaluation_steps": metric.get("evaluation_steps"),
            "model": judge_model,
        }
        # Normalize to avoid small differences in ordering or whitespace
        normalized = json.dumps(data, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(normalized.encode()).hexdigest()

    def get(self, key: str) -> dict | None:
        result = self._cache.get(key)
        if result is not None:
            self._hits += 1
            usage.record_judge_cache_hit()
            return result
        self._misses += 1
        return None

    def set(self, key: str, result: dict) -> None:
        self._cache[key] = result

    def stats(self) -> dict:
        return {
            "hits": self._hits,
            "misses": self._misses,
            "cached": len(self._cache),
        }

    def clear(self) -> None:
        self._cache.clear()
        self._hits = self._misses = 0


# Global per-process cache (one per job in practice, cleared in jobs.py)
_judge_cache = JudgeCache()


def get_cache() -> JudgeCache:
    return _judge_cache


def clear_cache() -> None:
    _judge_cache.clear()
