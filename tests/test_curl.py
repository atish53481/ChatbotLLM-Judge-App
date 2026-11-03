import json

import pytest

from backend.targets.curl import (
    MESSAGE_PLACEHOLDER,
    find_reply_path,
    find_stream_reply_path,
    parse_curl,
    prepare_body,
    prepare_url,
)

CHROME_CURL = r"""curl 'https://shop.example/api/chat' \
  -H 'accept: application/json' \
  -H 'content-type: application/json' \
  -b 'session=abc123; csrftoken=zz' \
  -H 'x-csrf-token: zz' \
  --data-raw '{"conversation_id":"c1","message":"Where is my order?","stream":false}' \
  --compressed"""


def test_parse_chrome_copy_as_curl():
    parsed = parse_curl(CHROME_CURL)
    assert parsed["url"] == "https://shop.example/api/chat"
    assert parsed["method"] == "POST"
    assert parsed["headers"]["content-type"] == "application/json"
    assert parsed["headers"]["Cookie"] == "session=abc123; csrftoken=zz"
    assert parsed["headers"]["x-csrf-token"] == "zz"
    assert json.loads(parsed["body"])["message"] == "Where is my order?"


# Chrome switches to bash $'...' quoting when the body holds a quote or escape.
CHROME_ANSI_C_CURL = r"""curl --url 'http://localhost:5173/chat' \
  -H 'Content-Type: application/json' \
  -b '_ga=GA1.1.1; theme=dark' \
  -H 'sec-ch-ua: "Google Chrome";v="153", "Not_A Brand";v="8"' \
  --data-raw $'{"message":"What\'s your refund policy?","history":[{"role":"assistant","content":"Hi! I\'m ShopBot.\\nAsk me."}]}'"""


def test_parse_ansi_c_quoted_body():
    parsed = parse_curl(CHROME_ANSI_C_CURL)
    assert parsed["url"] == "http://localhost:5173/chat"
    assert parsed["method"] == "POST"
    assert parsed["headers"]["Cookie"] == "_ga=GA1.1.1; theme=dark"
    assert parsed["headers"]["sec-ch-ua"] == '"Google Chrome";v="153", "Not_A Brand";v="8"'
    body = json.loads(parsed["body"])
    assert body["message"] == "What's your refund policy?"
    assert body["history"][0]["content"] == "Hi! I'm ShopBot.\nAsk me."
    template, found = prepare_body(parsed["body"])
    assert found == "What's your refund policy?"
    assert json.loads(template) == {"message": MESSAGE_PLACEHOLDER, "history": []}


def test_unclosed_ansi_c_string_is_reported():
    with pytest.raises(ValueError, match="not closed"):
        parse_curl("curl https://a.example --data-raw $'{\"m\":1}")


def test_explicit_method_and_get_without_body():
    assert parse_curl("curl -X PUT https://a.example/x -d 'q=1'")["method"] == "PUT"
    parsed = parse_curl("curl 'https://a.example/ask?q=hi'")
    assert parsed["method"] == "GET"
    assert parsed["body"] == ""


@pytest.mark.parametrize("bad", [
    "wget https://a.example",
    "curl -H 'x: y'",
    # 'curl ^"https://a.example^" -H ^"x: y^"'  # now supported after fix
    "curl 'https://a.example",
])
def test_rejects_unusable_commands(bad):
    with pytest.raises(ValueError):
        parse_curl(bad)


def test_finds_message_by_key_and_empties_history():
    body = json.dumps({"message": "Where is my order?",
                       "history": [{"role": "user", "content": "Where is my order?"}]})
    template, found = prepare_body(body)
    assert found == "Where is my order?"
    assert json.loads(template) == {"message": MESSAGE_PLACEHOLDER, "history": []}


def test_chat_messages_keep_system_and_last_user_turn():
    body = json.dumps({"model": "m", "messages": [
        {"role": "system", "content": "be nice"}, {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "yo"}, {"role": "user", "content": "refund?"}]})
    template, found = prepare_body(body)
    assert found == "refund?"
    assert json.loads(template)["messages"] == [
        {"role": "system", "content": "be nice"},
        {"role": "user", "content": MESSAGE_PLACEHOLDER},
    ]


def test_nested_query_key_and_ids_left_alone():
    template, found = prepare_body(json.dumps({"conversation_id": "abc", "payload": {"query": "shipping cost"}}))
    assert found == "shipping cost"
    assert json.loads(template) == {"conversation_id": "abc", "payload": {"query": MESSAGE_PLACEHOLDER}}


def test_sample_message_overrides_detection():
    body = json.dumps({"message": "ignored", "text": "hi \"there\""})
    template, found = prepare_body(body, 'hi "there"')
    assert found == 'hi "there"'
    assert json.loads(template) == {"message": "ignored", "text": MESSAGE_PLACEHOLDER}


def test_form_body_and_query_string():
    assert prepare_body("q=hello+there&lang=en") == (f"q={MESSAGE_PLACEHOLDER}&lang=en", "hello there")
    url, found = prepare_url("https://a.example/ask?question=hi%20there&k=1")
    assert url == f"https://a.example/ask?question={MESSAGE_PLACEHOLDER}&k=1"
    assert found == "hi there"
    assert prepare_url("https://a.example/chat") == ("https://a.example/chat", "")


@pytest.mark.parametrize("response, path", [
    ({"reply": "x", "mode": "live"}, "reply"),
    ({"choices": [{"message": {"content": "y"}}]}, "choices.0.message.content"),
    ({"data": {"answer": {"text": "z"}}}, "data.answer.text"),
    ([{"generated_text": "w"}], "0.generated_text"),
    ({"id": "1", "weird": "the whole long answer"}, "weird"),
])
def test_find_reply_path(response, path):
    assert find_reply_path(response) == path


def test_find_stream_reply_path_skips_role_events():
    events = [{"choices": [{"delta": {"role": "assistant"}}]},
              {"choices": [{"delta": {"content": "He"}}]},
              {"choices": [{"delta": {"content": "y"}}]}]
    assert find_stream_reply_path(events) == "choices.0.delta.content"
