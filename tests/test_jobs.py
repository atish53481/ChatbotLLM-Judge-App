import threading

from backend import cancel, storage
from backend.dashboard import jobs as jobs_module
from backend.dashboard.jobs import JobFailed, JobQueue
from backend.dashboard.runner import RunCancelled


def _queue(tmp_path, run_metric):
    conn = storage.init_db(str(tmp_path / "q.db"))
    return JobQueue(conn, run_metric), conn


def test_jobs_run_in_order_one_at_a_time(tmp_path):
    seen = []
    queue, _ = _queue(tmp_path, lambda job, key, p: seen.append((job["id"], key)) or {"key": key, "status": "pass"})
    first = queue.submit(1, ["a", "b"], {})
    second = queue.submit(1, ["c"], {})
    assert queue.get(second)["status"] == "queued"
    assert queue.process_next() and queue.get(second)["status"] == "queued"
    assert queue.process_next() and not queue.process_next()
    assert seen == [(first, "a"), (first, "b"), (second, "c")]
    job = queue.get(first)
    assert job["status"] == "done" and [r["key"] for r in job["results"]] == ["a", "b"]
    assert job["current"] is None and job["finished_at"]


def test_a_failing_metric_does_not_stop_the_job(tmp_path):
    def run(job, key, p):
        return {"key": key, "status": "error", "error": "HTTP 500"} if key == "a" else {"key": key, "status": "pass"}
    queue, _ = _queue(tmp_path, run)
    job_id = queue.submit(1, ["a", "b"], {})
    queue.process_next()
    assert [r["status"] for r in queue.get(job_id)["results"]] == ["error", "pass"]
    assert queue.get(job_id)["status"] == "done"


def test_job_failed_ends_the_job_with_its_message(tmp_path):
    def run(job, key, p):
        raise JobFailed("chatbot deleted")
    queue, _ = _queue(tmp_path, run)
    job_id = queue.submit(1, ["a", "b"], {})
    queue.process_next()
    job = queue.get(job_id)
    assert job["status"] == "error" and job["error"] == "chatbot deleted" and job["results"] == []


def test_cancel_while_running_stops_at_the_next_step(tmp_path):
    holder = {}

    def run(job, key, on_progress):
        holder["queue"].cancel(job["id"])
        try:
            on_progress(0, 1, "chat", "q")
        except RunCancelled:
            return {"key": key, "status": "cancelled"}
        return {"key": key, "status": "pass"}

    queue, _ = _queue(tmp_path, run)
    holder["queue"] = queue
    job_id = queue.submit(1, ["a", "b"], {})
    queue.process_next()
    job = queue.get(job_id)
    assert job["status"] == "cancelled" and [r["key"] for r in job["results"]] == ["a"]


def test_cancel_while_queued_never_starts(tmp_path):
    calls = []
    queue, _ = _queue(tmp_path, lambda job, key, p: calls.append(key) or {"key": key, "status": "pass"})
    job_id = queue.submit(1, ["a"], {})
    assert queue.cancel(job_id)
    assert not queue.process_next()
    assert calls == [] and queue.get(job_id)["status"] == "cancelled"
    assert not queue.cancel(job_id)


def test_cancel_all_stops_every_chatbots_running_and_queued_jobs(tmp_path):
    holder, calls = {}, []

    def run(job, key, on_progress):
        calls.append(key)
        assert holder["queue"].cancel_all() == holder["ids"]
        return {"key": key, "status": "pass"}

    queue, _ = _queue(tmp_path, run)
    holder["queue"] = queue
    holder["ids"] = [queue.submit(1, ["a", "b"], {}), queue.submit(2, ["c"], {})]
    queue.process_next()
    assert not queue.process_next()  # the other chatbot's queued job never starts
    assert calls == ["a"]
    assert [queue.get(i)["status"] for i in holder["ids"]] == ["cancelled", "cancelled"]
    assert queue.cancel_all() == []


def test_stop_reaches_the_next_chatbot_or_judge_call(tmp_path):
    holder, sent = {}, []

    def run(job, key, on_progress):
        cancel.check()
        sent.append("first call")
        holder["queue"].cancel(job["id"])
        try:
            cancel.check()  # what the chatbot client and the judge call before each request
        except RunCancelled:
            return {"key": key, "status": "cancelled"}
        sent.append("second call")
        return {"key": key, "status": "pass"}

    queue, _ = _queue(tmp_path, run)
    holder["queue"] = queue
    job_id = queue.submit(1, ["a"], {})
    queue.process_next()
    assert sent == ["first call"] and queue.get(job_id)["status"] == "cancelled"
    cancel.check()  # outside a job nothing is stopped


def test_a_job_picks_its_own_number_of_parallel_metrics(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs_module, "METRIC_CONCURRENCY", 1)
    running, peak, lock = [0], [0], threading.Lock()
    both = threading.Barrier(2, timeout=5)

    def run(job, key, p):
        with lock:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        both.wait()  # only returns when two metrics are in flight together
        with lock:
            running[0] -= 1
        return {"key": key, "status": "pass"}

    queue, _ = _queue(tmp_path, run)
    job_id = queue.submit(1, ["a", "b"], {"metric_concurrency": 2})
    queue.process_next()
    assert queue.get(job_id)["status"] == "done" and peak[0] == 2
    assert jobs_module.clamp_metric_concurrency(0) == 1 and jobs_module.clamp_metric_concurrency(99) == 8


def test_live_progress_is_visible_while_running(tmp_path):
    seen, holder = {}, {}

    def run(job, key, on_progress):
        on_progress(1, 4, "judge", "Where is my order?")
        seen.update(holder["queue"].get(job["id"])["current"])
        return {"key": key, "status": "pass"}

    queue, _ = _queue(tmp_path, run)
    holder["queue"] = queue
    queue.submit(1, ["a"], {})
    queue.process_next()
    assert seen["metric_key"] == "a" and seen["index"] == 0 and seen["count"] == 1
    assert (seen["done"], seen["total"], seen["phase"], seen["question"]) == (1, 4, "judge", "Where is my order?")
    assert seen["started_at"].endswith("Z")


def test_worker_thread_runs_submitted_jobs(tmp_path):
    finished = threading.Event()

    def run(job, key, p):
        finished.set()
        return {"key": key, "status": "pass"}

    queue, _ = _queue(tmp_path, run)
    queue.start()
    queue.submit(1, ["a"], {})
    assert finished.wait(5)


def test_an_unexpected_error_ends_the_job_instead_of_leaving_it_running(tmp_path):
    def run(job, key, p):
        if key == "a":
            raise ValueError("judge config broken")
        return {"key": key, "status": "pass"}
    queue, _ = _queue(tmp_path, run)
    job_id = queue.submit(1, ["a", "b"], {})
    assert queue.process_next()
    job = queue.get(job_id)
    assert job["status"] == "error" and job["error"] == "ValueError: judge config broken"
    assert job["current"] is None and job["finished_at"]
    # The queue keeps working after it.
    later = queue.submit(1, ["b"], {})
    assert queue.process_next() and queue.get(later)["status"] == "done"


def test_metrics_of_a_job_run_side_by_side(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs_module, "METRIC_CONCURRENCY", 2)
    barrier, seen, holder = threading.Barrier(2, timeout=5), {}, {}

    def run(job, key, on_progress):
        on_progress(0, 1, "chat", key)
        barrier.wait()  # both metrics must be running at once to get past here
        seen[key] = [c["metric_key"] for c in holder["queue"].get(job["id"])["running"]]
        barrier.wait()
        return {"key": key, "status": "pass"}

    queue, _ = _queue(tmp_path, run)
    holder["queue"] = queue
    job_id = queue.submit(1, ["a", "b"], {})
    queue.process_next()
    job = queue.get(job_id)
    assert job["status"] == "done"
    assert [r["key"] for r in job["results"]] == ["a", "b"]
    assert seen == {"a": ["a", "b"], "b": ["a", "b"]}


def test_job_failed_stops_the_metric_running_beside_it(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs_module, "METRIC_CONCURRENCY", 2)
    failed = threading.Event()

    def run(job, key, on_progress):
        if key == "a":
            failed.set()
            raise JobFailed("chatbot deleted")
        failed.wait(timeout=5)
        on_progress(1, 2, "judge", "q")  # the next step sees the job has ended
        return {"key": key, "status": "pass"}

    queue, _ = _queue(tmp_path, run)
    job_id = queue.submit(1, ["a", "b"], {})
    queue.process_next()
    job = queue.get(job_id)
    assert job["status"] == "error" and job["error"] == "chatbot deleted"
    assert job["results"] == []


def test_pairwise_jobs_stay_one_metric_at_a_time(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs_module, "METRIC_CONCURRENCY", 4)
    live, peak, lock = [0], [0], threading.Lock()

    def run(job, key, on_progress):
        with lock:
            live[0] += 1
            peak[0] = max(peak[0], live[0])
        threading.Event().wait(0.05)
        with lock:
            live[0] -= 1
        return {"key": key, "status": "pass"}

    queue, _ = _queue(tmp_path, run)
    queue.submit(1, ["a", "b", "c"], {"kind": "pairwise"})
    queue.process_next()
    assert peak[0] == 1
