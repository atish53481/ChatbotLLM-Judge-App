import json
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from backend.targets.http_client import HttpTargetClient

URL = "https://shop.example/api/chat"


def _config(**overrides):
    config = {
        "url": URL,
        "method": "POST",
        "headers": {"Content-Type": "application/json", "Cookie": "s=1", "Content-Length": "99"},
        "body_template": '{"conversation_id": "c1", "message": "{{message}}"}',
        "response_path": "data.text",
    }
    config.update(overrides)
    return config


def _response(text, content_type="application/json", status=200):
    resp = MagicMock()
    resp.status_code = status
    resp.ok = status < 400
    resp.text = text
    resp.headers = {"Content-Type": content_type}
    resp.json.side_effect = lambda: json.loads(text)
    return resp


@patch("backend.targets.http_client.requests.request")
def test_fills_message_into_json_body_and_reads_reply(mock_request):
    mock_request.return_value = _response('{"data": {"text": "hello there"}}')
    reply = HttpTargetClient(_config()).chat('say "hi"\nnow')
    assert reply.reply == "hello there"
    assert reply.mode == "http"
    method, url = mock_request.call_args.args
    assert (method, url) == ("POST", URL)
    sent = json.loads(mock_request.call_args.kwargs["data"])
    assert sent == {"conversation_id": "c1", "message": 'say "hi"\nnow'}
    headers = mock_request.call_args.kwargs["headers"]
    assert headers["Cookie"] == "s=1"
    assert "Content-Length" not in headers


@patch("backend.targets.http_client.requests.request")
def test_form_body_is_url_encoded(mock_request):
    mock_request.return_value = _response('{"answer": "ok"}')
    config = _config(
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        body_template="q={{message}}&lang=en",
        response_path="answer",
    )
    HttpTargetClient(config).chat("a b&c")
    assert mock_request.call_args.kwargs["data"] == b"q=a+b%26c&lang=en"


@patch("backend.targets.http_client.requests.request")
def test_get_puts_message_in_url(mock_request):
    mock_request.return_value = _response("plain answer", content_type="text/plain")
    config = _config(method="GET", url=URL + "?q={{message}}", body_template="", response_path="")
    reply = HttpTargetClient(config).chat("where is it?")
    assert mock_request.call_args.args[1] == URL + "?q=where%20is%20it%3F"
    assert mock_request.call_args.kwargs["data"] is None
    assert reply.reply == "plain answer"


@patch("backend.targets.http_client.requests.request")
def test_stream_joins_text_across_events(mock_request):
    stream = (
        'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"Hel"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"lo"}}]}\n\n'
        "data: [DONE]\n\n"
    )
    mock_request.return_value = _response(stream, content_type="text/event-stream")
    reply = HttpTargetClient(_config(response_path="choices.0.delta.content")).chat("hi")
    assert reply.reply == "Hello"


@patch("backend.targets.http_client.requests.request")
def test_missing_reply_path_shows_the_response(mock_request):
    mock_request.return_value = _response('{"other": 1}')
    with pytest.raises(ValueError, match=r"data\.text.*Response was: \{\"other\": 1\}"):
        HttpTargetClient(_config()).chat("hi")


@patch("backend.targets.http_client.requests.request")
def test_http_error_shows_status_and_body(mock_request):
    mock_request.return_value = _response("forbidden: csrf", content_type="text/plain", status=403)
    with pytest.raises(ValueError, match="HTTP 403.*csrf"):
        HttpTargetClient(_config()).chat("hi")


@pytest.mark.parametrize("config", [
    _config(url="ftp://x"),
    _config(url=None),
    _config(body_template='{"message": "fixed"}'),
    _config(method="DELETE"),
    _config(method="GET", url=URL + "?q={{message}}"),
    _config(headers={"x": 1}),
    _config(response_path=3),
])
def test_invalid_config_raises_valueerror(config):
    with pytest.raises(ValueError):
        HttpTargetClient(config)


@patch("backend.targets.http_client.requests.request")
def test_history_goes_where_the_body_keeps_it(mock_request):
    mock_request.return_value = _response('{"data": {"text": "ok"}}')
    config = _config(body_template='{"message": "{{message}}", "history": []}')
    history = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
    HttpTargetClient(config).chat("again", history=history)
    assert json.loads(mock_request.call_args.kwargs["data"]) == {"message": "again", "history": history}


@patch("backend.targets.http_client.requests.request")
def test_chat_style_messages_get_history_before_the_question(mock_request):
    mock_request.return_value = _response('{"choices": [{"message": {"content": "ok"}}]}')
    config = _config(
        body_template='{"model": "m", "messages": [{"role": "system", "content": "be nice"}, '
                      '{"role": "user", "content": "{{message}}"}]}',
        response_path="choices.0.message.content")
    HttpTargetClient(config).chat("q2", history=[{"role": "user", "content": "q1"},
                                                 {"role": "assistant", "content": "a1"}])
    sent = json.loads(mock_request.call_args.kwargs["data"])["messages"]
    assert [m["content"] for m in sent] == ["be nice", "q1", "a1", "q2"]


@patch("backend.targets.http_client.requests.request")
def test_retrieved_context_is_read_from_context_path(mock_request):
    mock_request.return_value = _response(json.dumps({
        "data": {"text": "answer"},
        "sources": [{"title": "Refunds", "content": "Refunds take 7 days."}, "Returns: 30 days."],
    }))
    reply = HttpTargetClient(_config(context_path="sources")).chat("q")
    assert reply.retrieval_context == ["Refunds take 7 days.", "Returns: 30 days."]
    assert HttpTargetClient(_config()).chat("q").retrieval_context is None


from backend import usage


@patch("backend.targets.http_client.requests.request")
def test_send_counts_a_target_call(mock_request):
    usage.reset()
    mock_request.return_value = _response('{"data": {"text": "ok"}}')
    HttpTargetClient(_config()).chat("hi")
    assert usage.snapshot()["target_calls"] == 1
    usage.reset()


# --- Stage 1: non-answers, message length, busy chatbots, clear errors --------

@patch("backend.targets.http_client.requests.request")
def test_service_message_reply_is_an_error_not_an_answer(mock_request):
    from backend.targets.reply_check import ChatbotUnavailable
    mock_request.return_value = _response('{"data": {"text": "You have reached your daily message limit."}}')
    with pytest.raises(ChatbotUnavailable, match="service message"):
        HttpTargetClient(_config()).chat("hi")
    # A chatbot whose real answers look like that can switch the check off.
    reply = HttpTargetClient(_config(check_replies=False)).chat("hi")
    assert reply.reply == "You have reached your daily message limit."


@patch("backend.targets.http_client.requests.request")
def test_message_over_the_chatbots_limit_is_not_sent(mock_request):
    from backend.targets.http_client import MessageTooLong
    client = HttpTargetClient(_config(max_message_length=10))
    with pytest.raises(MessageTooLong, match="10"):
        client.chat("x" * 11)
    mock_request.assert_not_called()


@patch("backend.targets.http_client._sleep")
@patch("backend.targets.http_client.requests.request")
def test_busy_chatbot_is_retried_after_its_retry_after(mock_request, mock_sleep):
    busy = _response("slow down", status=429)
    busy.headers["Retry-After"] = "3"
    mock_request.side_effect = [busy, _response('{"data": {"text": "ok"}}')]
    assert HttpTargetClient(_config()).chat("hi").reply == "ok"
    assert mock_request.call_count == 2
    mock_sleep.assert_called_once_with(3.0)


@patch("backend.targets.http_client._sleep")
@patch("backend.targets.http_client.requests.request")
def test_busy_chatbot_gives_up_after_three_retries(mock_request, mock_sleep):
    mock_request.return_value = _response("unavailable", status=503)
    with pytest.raises(ValueError, match="busy"):
        HttpTargetClient(_config()).chat("hi")
    assert mock_request.call_count == 4
    assert [c.args[0] for c in mock_sleep.call_args_list] == [2.0, 4.0, 8.0]


@patch("backend.targets.http_client.requests.request")
def test_rejected_request_says_to_refresh_the_curl(mock_request):
    mock_request.return_value = _response("forbidden", status=403)
    with pytest.raises(ValueError, match="fresh cURL"):
        HttpTargetClient(_config()).chat("hi")


@patch("backend.targets.http_client.requests.request")
def test_timeout_and_unreachable_chatbot_have_plain_messages(mock_request):
    import requests
    mock_request.side_effect = requests.Timeout("read timed out")
    with pytest.raises(ValueError, match="did not answer within"):
        HttpTargetClient(_config()).chat("hi")
    mock_request.side_effect = requests.ConnectionError("refused")
    with pytest.raises(ValueError, match="Could not reach the chatbot"):
        HttpTargetClient(_config()).chat("hi")


@patch("backend.targets.http_client._clock")
@patch("backend.targets.http_client._sleep")
@patch("backend.targets.http_client.requests.request")
def test_send_delay_spaces_out_messages(mock_request, mock_sleep, mock_clock):
    mock_request.return_value = _response('{"data": {"text": "ok"}}')
    # Sent at 100.0; the second message is ready at 100.5, so it waits 1.5 s.
    mock_clock.side_effect = [100.0, 100.5, 102.0]
    client = HttpTargetClient(_config(send_delay=2))
    client.chat("one")
    client.chat("two")
    mock_sleep.assert_called_once_with(1.5)


@pytest.mark.parametrize("overrides, error", [
    ({"max_message_length": 0}, "max_message_length"),
    ({"max_message_length": "500"}, "max_message_length"),
    ({"send_delay": -1}, "send_delay"),
    ({"send_delay": 120}, "send_delay"),
    ({"check_replies": "yes"}, "check_replies"),
])
def test_stage_one_settings_are_validated(overrides, error):
    with pytest.raises(ValueError, match=error):
        HttpTargetClient(_config(**overrides))


def test_stage_one_settings_are_optional():
    client = HttpTargetClient(_config(max_message_length=None, send_delay=None))
    assert client.max_message_length is None and client.send_delay == 0 and client.check_replies


@patch("backend.targets.http_client.requests.request")
def test_send_delay_holds_across_clients_and_threads(mock_request):
    # Two runs of one job talk to the same paced chatbot at once: still one
    # message at a time, send_delay apart.
    lock, live, peak, sent = threading.Lock(), [0], [0], []

    def request(*_args, **_kwargs):
        with lock:
            live[0] += 1
            peak[0] = max(peak[0], live[0])
            sent.append(time.monotonic())
        time.sleep(0.02)
        with lock:
            live[0] -= 1
        return _response('{"data": {"text": "ok"}}')

    mock_request.side_effect = request
    clients = [HttpTargetClient(_config(send_delay=0.05)) for _ in range(2)]
    threads = [threading.Thread(target=lambda c=c: [c.chat("hi") for _ in range(2)]) for c in clients]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert peak[0] == 1
    gaps = [b - a for a, b in zip(sent, sent[1:])]
    assert len(sent) == 4 and min(gaps) >= 0.05
