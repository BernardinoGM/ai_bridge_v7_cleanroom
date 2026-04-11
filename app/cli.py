from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from typing import Any

import httpx

from app.terminal import TERMINAL_TEMPORARY_MESSAGE


DEFAULT_BASE_URL = "https://getaibridge.com"
DEFAULT_API_PATH = "/terminal/messages"
PASTE_HELP = "/paste for multi-line input, /send to submit, /cancel to discard."


@dataclass(frozen=True)
class TerminalCliResult:
    text: str
    task_id: str | None


def _terminal_headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def _extract_text(payload: dict[str, Any]) -> str | None:
    content = payload.get("content")
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
                return item["text"]
    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        if isinstance(message, dict) and isinstance(message.get("content"), str):
            return message["content"]
    return None


def _normalize_prompt(prompt: str) -> str:
    return " ".join(prompt.strip().lower().split())


def _should_continue_task(prompt: str, active_task_id: str | None) -> bool:
    if not active_task_id:
        return False
    normalized = _normalize_prompt(prompt)
    if not normalized:
        return False
    if normalized in {"continue", "same file", "same bug", "same task", "same repo", "same error"}:
        return True
    if normalized in {"1", "2", "3", "option 1", "option 2", "option 3", "i mean i choose 1", "i mean i will choose 1"}:
        return True
    return False


def send_terminal_request(
    prompt: str,
    api_key: str,
    *,
    base_url: str | None = None,
    client: httpx.Client | None = None,
    task_id: str | None = None,
    task_action: str | None = None,
) -> TerminalCliResult:
    if not prompt.strip():
        return TerminalCliResult(text="", task_id=task_id)
    url = f"{(base_url or os.environ.get('AB_BASE_URL') or DEFAULT_BASE_URL).rstrip('/')}{DEFAULT_API_PATH}"
    payload: dict[str, Any] = {
        "mode": "smart",
        "source_surface": "ab_cli",
        "messages": [{"role": "user", "content": prompt}],
    }
    if task_id:
        payload["task_id"] = task_id
    if task_action:
        payload["task_action"] = task_action
    close_client = client is None
    http_client = client or httpx.Client(timeout=45.0)
    try:
        response = http_client.post(url, headers=_terminal_headers(api_key), json=payload)
        if response.status_code != 200:
            return TerminalCliResult(text=TERMINAL_TEMPORARY_MESSAGE, task_id=task_id)
        data = response.json()
        return TerminalCliResult(
            text=_extract_text(data) or TERMINAL_TEMPORARY_MESSAGE,
            task_id=data.get("task_id") or task_id,
        )
    except Exception:
        return TerminalCliResult(text=TERMINAL_TEMPORARY_MESSAGE, task_id=task_id)
    finally:
        if close_client:
            http_client.close()


def send_terminal_prompt(
    prompt: str,
    api_key: str,
    *,
    base_url: str | None = None,
    client: httpx.Client | None = None,
) -> str:
    return send_terminal_request(prompt, api_key, base_url=base_url, client=client).text


def _interactive_repl(api_key: str, base_url: str | None = None) -> int:
    print(f"AI Bridge terminal ready. Enter sends. {PASTE_HELP} Ctrl-D exits.")
    compose_mode = False
    compose_lines: list[str] = []
    active_task_id: str | None = None

    def _submit(prompt: str) -> None:
        nonlocal active_task_id
        result = send_terminal_request(
            prompt,
            api_key,
            base_url=base_url,
            task_id=active_task_id,
            task_action="continue" if _should_continue_task(prompt, active_task_id) else None,
        )
        active_task_id = result.task_id or active_task_id
        print(result.text)

    while True:
        try:
            prompt = input("aibridge> " if not compose_mode else "paste> ")
        except EOFError:
            print()
            return 0
        if compose_mode:
            command = prompt.strip()
            if command == "/send":
                message = "\n".join(compose_lines).strip()
                compose_lines = []
                compose_mode = False
                if not message:
                    print("Paste buffer is empty.")
                    continue
                _submit(message)
                continue
            if command == "/cancel":
                compose_lines = []
                compose_mode = False
                print("Paste buffer discarded.")
                continue
            compose_lines.append(prompt)
            continue
        command = prompt.strip()
        if not command:
            continue
        if command == "/paste":
            compose_mode = True
            compose_lines = []
            print("Paste mode enabled. /send submits, /cancel discards.")
            continue
        if command in {"/send", "/cancel"}:
            print(PASTE_HELP)
            continue
        _submit(command)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aibridge", description="AI Bridge terminal")
    parser.add_argument("prompt", nargs="*", help="Optional prompt to send immediately")
    args = parser.parse_args(argv)

    api_key = (os.environ.get("AB_API_KEY") or "").strip()
    if not api_key:
        print('Set AB_API_KEY before launching AI Bridge.', file=sys.stderr)
        return 1

    prompt = " ".join(args.prompt).strip()
    if prompt:
        print(send_terminal_prompt(prompt, api_key))
        return 0

    if not sys.stdin.isatty():
        piped = sys.stdin.read().strip()
        if piped:
            print(send_terminal_request(piped, api_key).text)
            return 0

    return _interactive_repl(api_key)


if __name__ == "__main__":
    raise SystemExit(main())
