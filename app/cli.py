from __future__ import annotations

import argparse
import os
import sys
from typing import Any

import httpx

from app.terminal import TERMINAL_TEMPORARY_MESSAGE


DEFAULT_BASE_URL = "https://getaibridge.com"
DEFAULT_API_PATH = "/terminal/messages"


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


def send_terminal_prompt(
    prompt: str,
    api_key: str,
    *,
    base_url: str | None = None,
    client: httpx.Client | None = None,
) -> str:
    if not prompt.strip():
        return ""
    url = f"{(base_url or os.environ.get('AB_BASE_URL') or DEFAULT_BASE_URL).rstrip('/')}{DEFAULT_API_PATH}"
    payload = {
        "mode": "smart",
        "source_surface": "ab_cli",
        "messages": [{"role": "user", "content": prompt}],
    }
    close_client = client is None
    http_client = client or httpx.Client(timeout=45.0)
    try:
        response = http_client.post(url, headers=_terminal_headers(api_key), json=payload)
        if response.status_code != 200:
            return TERMINAL_TEMPORARY_MESSAGE
        data = response.json()
        return _extract_text(data) or TERMINAL_TEMPORARY_MESSAGE
    except Exception:
        return TERMINAL_TEMPORARY_MESSAGE
    finally:
        if close_client:
            http_client.close()


def _interactive_repl(api_key: str, base_url: str | None = None) -> int:
    print("AI Bridge terminal ready. Type a prompt and press Enter. Ctrl-D exits.")
    while True:
        try:
            prompt = input("ab> ").strip()
        except EOFError:
            print()
            return 0
        if not prompt:
            continue
        print(send_terminal_prompt(prompt, api_key, base_url=base_url))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ab", description="AI Bridge terminal")
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
            print(send_terminal_prompt(piped, api_key))
            return 0

    return _interactive_repl(api_key)


if __name__ == "__main__":
    raise SystemExit(main())
