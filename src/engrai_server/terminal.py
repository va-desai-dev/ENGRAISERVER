"""Small HTTP clients for profile control and process-local terminal chat.

No gateway, worker, or UI imports belong here. Conversation state is committed
only after a complete response, and disappears when this process exits.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections.abc import Iterator
from typing import Any
from urllib.parse import quote

import httpx


class ClientError(Exception):
    """An actionable client or gateway error, safe to display without a traceback."""


def positive_seconds(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be a finite number greater than zero")
    return number


def add_client_commands(subparsers: Any, add_shared: Any) -> None:
    for name, help_text in (
        ("load", "load a text profile on the running gateway"),
        ("unload", "unload the gateway's active text profile"),
        ("chat", "chat with a text profile using terminal-local history"),
    ):
        parser = subparsers.add_parser(name, help=help_text)
        if name != "unload":
            parser.add_argument("profile", help="saved text route ID (see routes)")
        parser.add_argument("--url", help="gateway root URL (or ENGRAI_SERVER_URL)")
        parser.add_argument(
            "--admin-token", help="admin password/session token (or ENGRAI_ADMIN_TOKEN)"
        )
        parser.add_argument(
            "--timeout", type=positive_seconds, default=600,
            help="HTTP inactivity timeout in seconds (default: 600)",
        )
        if name == "chat":
            parser.add_argument("--api-key", help="client API key (or ENGRAI_API_KEY)")
            parser.add_argument("--system", help="system instruction for this chat")
            parser.add_argument("--no-stream", action="store_true", help="print each reply in full")
            parser.add_argument(
                "--hide-thinking", action="store_true",
                help="do not show the model's reasoning (shown muted by default on a terminal)",
            )
        else:
            parser.add_argument("--json", action="store_true", help="print the gateway response")
        add_shared(parser, client=True)
        parser.set_defaults(func=command_client)


def gateway_url(args: argparse.Namespace, settings: Any) -> str:
    url = args.url or os.environ.get("ENGRAI_SERVER_URL")
    if not url:
        host = settings.bind_host
        if host in {"0.0.0.0", "::", "[::]"}:
            host = "127.0.0.1"
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        url = f"http://{host}:{settings.bind_port}"
    parsed = httpx.URL(url)
    if parsed.scheme not in {"http", "https"} or not parsed.host:
        raise ClientError("--url must be an http:// or https:// gateway root URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ClientError("--url must not contain credentials, a query, or a fragment")
    return str(parsed).rstrip("/") + "/"


def bearer(secret: str | None, variable: str) -> dict[str, str]:
    if not secret or not secret.strip():
        raise ClientError(
            f"Set {variable} or pass its corresponding credential flag "
            "(not needed on the gateway's own host while it is running)."
        )
    return {"Authorization": f"Bearer {secret.strip()}"}


def error_message(payload: Any) -> str:
    if isinstance(payload, dict):
        for key in ("error", "detail", "message"):
            if key in payload:
                return error_message(payload[key])
    return str(payload)


def check_response(response: httpx.Response) -> None:
    if response.is_success:
        return
    response.read()
    try:
        detail = error_message(response.json())
    except ValueError:
        detail = response.text[:500] or response.reason_phrase
    raise ClientError(f"HTTP {response.status_code}: {detail}")


def json_object(response: httpx.Response) -> dict[str, Any]:
    check_response(response)
    try:
        payload = response.json()
    except ValueError as exc:
        raise ClientError("Gateway returned invalid JSON.") from exc
    if not isinstance(payload, dict):
        raise ClientError("Gateway returned an unexpected response.")
    if "error" in payload:
        raise ClientError(error_message(payload))
    return payload


def profile_control(client: httpx.Client, token: str | None, profile: str | None) -> dict[str, Any]:
    if profile is not None and not profile.strip():
        raise ClientError("A nonempty profile ID is required.")
    path = f"control/models/{quote(profile, safe='')}/load" if profile is not None else "control/models/unload"
    return json_object(client.post(path, headers=bearer(token, "ENGRAI_ADMIN_TOKEN")))


def sse_data(response: httpx.Response) -> Iterator[str]:
    """Decode SSE records, including comments and multi-line data fields."""
    data: list[str] = []
    for line in response.iter_lines():
        if not line:
            if data:
                yield "\n".join(data)
                data.clear()
        elif line.startswith("data:"):
            data.append(line[5:].removeprefix(" "))
    if data:
        yield "\n".join(data)


DIM = "\033[2m"
RESET = "\033[0m"


class ThinkingPrinter:
    """Print reasoning muted, like ollama, and keep it out of the answer.

    Reasoning is only for the person watching: it is never committed to the
    chat history, and it is never written to a redirected stdout, so piping
    a reply into a file or another program yields only the answer.
    """

    def __init__(self, show: bool):
        self.show = show and sys.stdout.isatty()
        self.dim = self.show and not os.environ.get("NO_COLOR")
        self.thinking = False

    def reasoning(self, text: str) -> None:
        if not self.show or not text:
            return
        if not self.thinking:
            self.thinking = True
            if self.dim:
                print(DIM, end="")
        print(text, end="", flush=True)

    def content(self, text: str) -> None:
        self.end()
        print(text, end="", flush=True)

    def end(self) -> None:
        if self.thinking:
            self.thinking = False
            print(RESET if self.dim else "", end="")
            print("\n", flush=True)  # one blank line between thinking and answer


class ChatSession:
    def __init__(self, profile: str, system: str | None = None, *, show_thinking: bool = True):
        self.profile: str | None = profile
        self.system = system
        self.show_thinking = show_thinking
        self.clear()

    def clear(self) -> None:
        self.messages: list[dict[str, str]] = (
            [{"role": "system", "content": self.system}] if self.system else []
        )

    def turn(self, client: httpx.Client, key: str | None, prompt: str, *, stream: bool) -> None:
        if not self.profile:
            raise ClientError("No profile selected. Use /load PROFILE first.")
        pending = [*self.messages, {"role": "user", "content": prompt}]
        payload = {"model": self.profile, "messages": pending, "stream": stream}
        headers = bearer(key, "ENGRAI_API_KEY")
        printer = ThinkingPrinter(self.show_thinking)
        if stream:
            answer = self._stream(client, headers, payload, printer)
        else:
            result = json_object(client.post("v1/chat/completions", headers=headers, json=payload))
            try:
                message = result["choices"][0]["message"]
                answer = message["content"]
                if not isinstance(answer, str):
                    raise TypeError
            except (KeyError, IndexError, TypeError) as exc:
                raise ClientError("Gateway returned no text assistant message.") from exc
            reasoning = message.get("reasoning_content")
            if isinstance(reasoning, str):
                printer.reasoning(reasoning.strip())
            printer.content(answer)
            print()
        # Failed requests, interrupted replies and partial streams never become
        # context for the next turn. Each process owns its own message list.
        self.messages = [*pending, {"role": "assistant", "content": answer}]

    @staticmethod
    def _stream(
        client: httpx.Client, headers: dict[str, str], payload: dict[str, Any],
        printer: ThinkingPrinter,
    ) -> str:
        pieces: list[str] = []
        finished = False
        had_reasoning = False
        try:
            with client.stream("POST", "v1/chat/completions", headers=headers, json=payload) as response:
                check_response(response)
                for data in sse_data(response):
                    if data == "[DONE]":
                        if not finished:
                            raise ClientError("Reply ended without a completion marker; turn discarded.")
                        return "".join(pieces)
                    try:
                        event = json.loads(data)
                        if "error" in event:
                            raise ClientError(error_message(event))
                        for choice in event.get("choices", []):
                            if choice.get("index", 0) != 0:
                                continue
                            delta = choice.get("delta", {})
                            reasoning = delta.get("reasoning_content")
                            if reasoning is not None:
                                if not isinstance(reasoning, str):
                                    raise TypeError
                                had_reasoning = True
                                # Leading newlines after <think> just push the
                                # muted block down; drop them before it starts.
                                printer.reasoning(reasoning if printer.thinking else reasoning.lstrip("\n"))
                            content = delta.get("content")
                            if content is not None:
                                if not isinstance(content, str):
                                    raise TypeError
                                if not pieces:
                                    content = content.lstrip("\n") if had_reasoning else content
                                if content or pieces:
                                    pieces.append(content)
                                    printer.content(content)
                            if choice.get("finish_reason") is not None:
                                finished = True
                    except (ValueError, TypeError, AttributeError) as exc:
                        raise ClientError("Gateway returned an invalid chat stream; turn discarded.") from exc
            raise ClientError("Reply stream disconnected before [DONE]; turn discarded.")
        finally:
            printer.end()  # never leave the terminal dimmed, even on Ctrl+C
            print()


CHAT_HELP = """/help           Show commands
/models         List the gateway's available text route IDs
/load PROFILE   Load/select a profile; switching profiles clears history
/unload         Unload the shared text worker and clear this chat
/clear          Clear local history, retaining the system instruction
/think          Show or hide the model's muted reasoning
/exit, /quit    Exit; leave the server and loaded model running
Ctrl+C cancels a reply; Ctrl+C at the prompt or Ctrl+D exits.
"""


def chat_command(line: str, session: ChatSession, client: httpx.Client, args: argparse.Namespace) -> bool:
    command, _, argument = line.partition(" ")
    argument = argument.strip()
    if command in {"/exit", "/quit"} and not argument:
        return False
    if command == "/help" and not argument:
        print(CHAT_HELP, file=sys.stderr)
    elif command == "/think" and not argument:
        session.show_thinking = not session.show_thinking
        print(f"Thinking {'shown' if session.show_thinking else 'hidden'}.", file=sys.stderr)
    elif command == "/clear" and not argument:
        session.clear()
        print("Chat history cleared.", file=sys.stderr)
    elif command == "/models" and not argument:
        result = json_object(client.get("v1/models", headers=bearer(args.api_key, "ENGRAI_API_KEY")))
        for model in result.get("data", []):
            if model.get("owned_by") != "self-image":
                print(model["id"], file=sys.stderr)
    elif command == "/load" and argument:
        profile_control(client, args.admin_token, argument)
        if session.profile != argument:
            session.clear()
        session.profile = argument
        print(f"Loaded {argument}.", file=sys.stderr)
    elif command == "/unload" and not argument:
        profile_control(client, args.admin_token, None)
        session.profile = None
        session.clear()
        print("Text profile unloaded; chat history cleared.", file=sys.stderr)
    else:
        print("Unknown command or arguments. Use /help.", file=sys.stderr)
    return True


def run_chat(client: httpx.Client, args: argparse.Namespace) -> int:
    bearer(args.api_key, "ENGRAI_API_KEY")
    session = ChatSession(args.profile, args.system, show_thinking=not getattr(args, "hide_thinking", False))
    if not sys.stdin.isatty():
        prompt = sys.stdin.read()
        if not prompt.strip():
            raise ClientError("No prompt received on stdin.")
        session.turn(client, args.api_key, prompt, stream=not args.no_stream)
        return 0

    # Optional stdlib line editing; no transcript is written to disk.
    try:
        import readline  # noqa: F401
    except ImportError:
        pass
    print(f"Chat: {session.profile}. /help for commands. History lasts until exit.", file=sys.stderr)
    while True:
        try:
            print(">>> ", end="", file=sys.stderr, flush=True)
            line = input().strip()
        except EOFError:
            print(file=sys.stderr)
            return 0
        except KeyboardInterrupt:
            print(file=sys.stderr)
            return 130
        if not line:
            continue
        try:
            if line.startswith("/"):
                if not chat_command(line, session, client, args):
                    return 0
            else:
                session.turn(client, args.api_key, line, stream=not args.no_stream)
        except KeyboardInterrupt:
            print("\nInterrupted; incomplete turn discarded. Server state may still be changing.", file=sys.stderr)
        except (ClientError, httpx.HTTPError) as exc:
            print(f"Error: {exc}", file=sys.stderr)


def owner_token(base_url: str, settings: Any) -> str | None:
    """The gateway's owner-only CLI token, but only for a gateway on this host.

    Never sent to a remote --url: the token belongs to the local gateway, and
    a different server has no business seeing it.
    """
    from .local_token import LOOPBACK_HOSTS, local_token_path, read_local_token

    if httpx.URL(base_url).host.strip("[]") not in LOOPBACK_HOSTS:
        return None
    return read_local_token(local_token_path(settings.credentials_path))


def command_client(args: argparse.Namespace) -> int:
    from .settings import get_settings

    settings = get_settings()
    args.admin_token = args.admin_token or settings.control_admin_token
    if args.command == "chat":
        args.api_key = args.api_key or os.environ.get("ENGRAI_API_KEY")
    try:
        base_url = gateway_url(args, settings)
        # Explicit credentials win; otherwise a local shell as the service
        # owner is trusted, since it could read credentials.json anyway.
        local = None
        if not args.admin_token or (args.command == "chat" and not args.api_key):
            local = owner_token(base_url, settings)
        args.admin_token = args.admin_token or local
        if args.command == "chat":
            args.api_key = args.api_key or local
        with httpx.Client(
            base_url=base_url,
            timeout=httpx.Timeout(args.timeout, connect=min(10, args.timeout)),
        ) as client:
            if args.command == "chat":
                return run_chat(client, args)
            result = profile_control(client, args.admin_token, getattr(args, "profile", None))
            if args.json:
                print(json.dumps(result, indent=2))
            else:
                print(f"Loaded {args.profile}." if args.command == "load" else "Text profile unloaded.")
            return 0
    except KeyboardInterrupt:
        print("\nInterrupted. The gateway remains running.", file=sys.stderr)
        return 130
    except (ClientError, httpx.HTTPError, httpx.InvalidURL) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
