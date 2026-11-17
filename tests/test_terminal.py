from __future__ import annotations

import io
import json
from types import SimpleNamespace

import httpx
import pytest

from engrai_server.cli import build_parser, main
from engrai_server.terminal import (
    ChatSession, ClientError, chat_command, gateway_url, run_chat,
)


def completion(text="hello"):
    return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": text}}]})


def event(delta=None, finish=None):
    return "data: " + json.dumps({"choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}]}) + "\n\n"


def streamed(text="hello"):
    return httpx.Response(200, text=(
        ": heartbeat\n\n" + event({"role": "assistant"})
        + event({"reasoning_content": "private reasoning"})
        + event({"content": text}) + event(finish="stop")
        + 'data: {"choices": [], "usage": {"total_tokens": 10}}\n\n'
        + "data: [DONE]\n\n"
    ), headers={"content-type": "text/event-stream"})


@pytest.mark.parametrize("stream", [False, True])
def test_turns_resend_complete_history_and_sessions_are_isolated(stream, capsys):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        assert request.url.path == "/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer api"
        return streamed() if stream else completion()

    session = ChatSession("alpha", "Be brief")
    with httpx.Client(base_url="http://gateway/", transport=httpx.MockTransport(handler)) as client:
        session.turn(client, "api", "first", stream=stream)
        session.turn(client, "api", "second", stream=stream)
        ChatSession("alpha").turn(client, "api", "separate", stream=stream)
    assert requests[1] == {"model": "alpha", "stream": stream, "messages": [
        {"role": "system", "content": "Be brief"},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "second"},
    ]}
    assert requests[2]["messages"] == [{"role": "user", "content": "separate"}]
    assert capsys.readouterr().out == "hello\nhello\nhello\n"


@pytest.mark.parametrize("failure", [
    httpx.Response(401, json={"detail": {"error": {"message": "Invalid API key"}}}),
    httpx.Response(502, text="upstream unavailable"),
    httpx.Response(200, text="not JSON"),
    httpx.Response(200, json={"choices": []}),
])
def test_failed_turn_does_not_change_history(failure):
    session = ChatSession("alpha")
    replies = iter([completion("one"), failure, completion("two")])
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return next(replies)

    with httpx.Client(base_url="http://gateway/", transport=httpx.MockTransport(handler)) as client:
        session.turn(client, "api", "first", stream=False)
        old = list(session.messages)
        with pytest.raises(ClientError):
            session.turn(client, "api", "failed", stream=False)
        assert session.messages == old
        session.turn(client, "api", "retry", stream=False)
    assert bodies[-1]["messages"] == [*old, {"role": "user", "content": "retry"}]


@pytest.mark.parametrize("tail", [
    "", "data: [DONE]\n\n", "data: broken\n\n", "data: []\n\n",
    'data: {"error": {"message": "context full"}}\n\n',
    event(finish="stop"),
])
def test_broken_stream_rolls_back(tail):
    session = ChatSession("alpha", "system")
    with httpx.Client(base_url="http://gateway/", transport=httpx.MockTransport(
        lambda request: httpx.Response(200, text=event({"content": "partial"}) + tail)
    )) as client:
        with pytest.raises(ClientError):
            session.turn(client, "api", "question", stream=True)
    assert session.messages == [{"role": "system", "content": "system"}]


def test_interrupted_stream_is_closed_and_not_committed():
    class InterruptStream(httpx.SyncByteStream):
        closed = False

        def __iter__(self):
            yield event({"content": "partial"}).encode()
            raise KeyboardInterrupt

        def close(self):
            self.closed = True

    stream = InterruptStream()
    session = ChatSession("alpha")
    with httpx.Client(base_url="http://gateway/", transport=httpx.MockTransport(
        lambda request: httpx.Response(200, stream=stream)
    )) as client:
        with pytest.raises(KeyboardInterrupt):
            session.turn(client, "api", "question", stream=True)
    assert stream.closed
    assert session.messages == []


def test_profile_commands_clear_history_only_after_success():
    args = SimpleNamespace(admin_token="admin", api_key="api")
    session = ChatSession("alpha", "system")
    paths = []

    def handler(request):
        paths.append(request.url.path)
        assert request.headers["authorization"] == "Bearer admin"
        if request.url.path == "/control/models/broken/load":
            return httpx.Response(404, json={"detail": "Unknown model profile"})
        return httpx.Response(200, json={"ok": True})

    with httpx.Client(base_url="http://gateway/", transport=httpx.MockTransport(handler)) as client:
        session.messages.append({"role": "user", "content": "old"})
        before = list(session.messages)
        with pytest.raises(ClientError, match="Unknown model profile"):
            chat_command("/load broken", session, client, args)
        assert session.profile == "alpha" and session.messages == before
        chat_command("/load alpha", session, client, args)
        assert session.messages == before
        chat_command("/load beta", session, client, args)
        assert session.profile == "beta"
        assert session.messages == [{"role": "system", "content": "system"}]
        chat_command("/unload", session, client, args)
        assert session.profile is None
        with pytest.raises(ClientError, match="No profile selected"):
            session.turn(client, "api", "question", stream=False)
        assert chat_command("/exit", session, client, args) is False
    assert paths == ["/control/models/broken/load", "/control/models/alpha/load",
                     "/control/models/beta/load", "/control/models/unload"]


def test_piped_multiline_prompt_is_one_request(monkeypatch, capsys):
    args = build_parser().parse_args(["chat", "alpha", "--api-key", "api", "--no-stream"])
    monkeypatch.setattr("sys.stdin", io.StringIO("first line\nsecond line\n"))
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return completion()

    with httpx.Client(base_url="http://gateway/", transport=httpx.MockTransport(handler)) as client:
        assert run_chat(client, args) == 0
    assert requests == [{"model": "alpha", "stream": False, "messages": [
        {"role": "user", "content": "first line\nsecond line\n"},
    ]}]
    assert capsys.readouterr().out == "hello\n"


def test_interactive_retry_clear_and_exit(monkeypatch, capsys):
    args = build_parser().parse_args(["chat", "alpha", "--api-key", "api", "--no-stream", "--system", "system"])
    monkeypatch.setattr("sys.stdin", SimpleNamespace(isatty=lambda: True))
    lines = iter(["", "first", "retry", "/clear", "fresh", "/quit"])
    monkeypatch.setattr("builtins.input", lambda: next(lines))
    replies = iter([httpx.Response(503, json={"detail": "busy"}), completion(), completion()])
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return next(replies)

    with httpx.Client(base_url="http://gateway/", transport=httpx.MockTransport(handler)) as client:
        assert run_chat(client, args) == 0
    assert [len(body["messages"]) for body in requests] == [2, 2, 2]
    assert requests[-1]["messages"][-1]["content"] == "fresh"
    assert "HTTP 503: busy" in capsys.readouterr().err


@pytest.mark.parametrize("interrupt,code", [(EOFError, 0), (KeyboardInterrupt, 130)])
def test_prompt_exit_never_calls_gateway(monkeypatch, interrupt, code):
    args = build_parser().parse_args(["chat", "alpha", "--api-key", "api"])
    monkeypatch.setattr("sys.stdin", SimpleNamespace(isatty=lambda: True))

    def read():
        raise interrupt

    def handler(request):
        pytest.fail("exit must not unload the worker")

    monkeypatch.setattr("builtins.input", read)
    with httpx.Client(base_url="http://gateway/", transport=httpx.MockTransport(handler)) as client:
        assert run_chat(client, args) == code


@pytest.mark.parametrize("command,path", [
    (["load", "alpha"], "/control/models/alpha/load"),
    (["unload"], "/control/models/unload"),
])
def test_cli_lifecycle_uses_gateway_admin_auth(monkeypatch, tmp_path, capsys, command, path):
    from engrai_server.settings import get_settings

    monkeypatch.setenv("ENGRAI_HOME", str(tmp_path))
    monkeypatch.setenv("ENGRAI_ADMIN_TOKEN", "admin-secret")
    monkeypatch.setenv("ENGRAI_SERVER_URL", "https://gateway.test")
    real_client = httpx.Client
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr("engrai_server.terminal.httpx.Client", lambda **kwargs: real_client(
        **kwargs, transport=httpx.MockTransport(handler)
    ))
    try:
        assert main([*command, "--json"]) == 0
    finally:
        get_settings.cache_clear()
    assert json.loads(capsys.readouterr().out) == {"ok": True}
    assert len(requests) == 1
    assert str(requests[0].url) == "https://gateway.test" + path
    assert requests[0].method == "POST"
    assert requests[0].headers["authorization"] == "Bearer admin-secret"


@pytest.mark.parametrize("host,expected", [
    ("0.0.0.0", "127.0.0.1"), ("::", "127.0.0.1"),
    ("::1", "[::1]"), ("192.0.2.1", "192.0.2.1"),
])
def test_client_url_uses_bind_configuration(monkeypatch, host, expected):
    monkeypatch.delenv("ENGRAI_SERVER_URL", raising=False)
    args = SimpleNamespace(url=None)
    settings = SimpleNamespace(bind_host=host, bind_port=9400)
    assert gateway_url(args, settings) == f"http://{expected}:9400/"
    args.url = "https://remote.test/engrai/"
    assert gateway_url(args, settings) == "https://remote.test/engrai/"


@pytest.mark.parametrize("timeout", ["0", "-1", "nan", "inf"])
def test_timeout_must_be_finite_and_positive(timeout):
    with pytest.raises(SystemExit):
        build_parser().parse_args(["chat", "alpha", "--timeout", timeout])


@pytest.mark.parametrize("url", ["localhost:8400", "ftp://server", "http://user:pass@server", "http://server?key=secret"])
def test_invalid_gateway_urls_are_rejected(url):
    with pytest.raises(ClientError):
        gateway_url(SimpleNamespace(url=url), None)


def test_remote_models_uses_api_auth_and_filters_images(capsys):
    def handler(request):
        assert request.url.path == "/v1/models"
        assert request.headers["authorization"] == "Bearer api"
        return httpx.Response(200, json={"data": [
            {"id": "text", "owned_by": "self"},
            {"id": "image", "owned_by": "self-image"},
        ]})

    with httpx.Client(base_url="http://gateway/", transport=httpx.MockTransport(handler)) as client:
        chat_command("/models", ChatSession("text"), client, SimpleNamespace(api_key="api"))
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "text\n"


@pytest.mark.parametrize("mode", ["success", "missing-key", "connection-error", "empty-input"])
def test_chat_entrypoint_credentials_and_errors(monkeypatch, tmp_path, capsys, mode):
    from engrai_server.settings import get_settings

    monkeypatch.setenv("ENGRAI_HOME", str(tmp_path))
    monkeypatch.delenv("ENGRAI_API_KEY", raising=False)
    if mode != "missing-key":
        monkeypatch.setenv("ENGRAI_API_KEY", "client-key")
    monkeypatch.setattr("sys.stdin", io.StringIO("" if mode == "empty-input" else "hello"))
    real_client = httpx.Client

    def handler(request):
        assert mode not in {"missing-key", "empty-input"}
        assert request.headers["authorization"] == "Bearer client-key"
        if mode == "connection-error":
            raise httpx.ConnectError("Connection refused", request=request)
        return completion()

    monkeypatch.setattr("engrai_server.terminal.httpx.Client", lambda **kwargs: real_client(
        **kwargs, transport=httpx.MockTransport(handler)
    ))
    try:
        assert main(["chat", "alpha", "--no-stream"]) == (0 if mode == "success" else 1)
    finally:
        get_settings.cache_clear()
    output = capsys.readouterr()
    if mode == "success":
        assert output.out == "hello\n" and output.err == ""
    else:
        assert output.out == "" and "Error:" in output.err
