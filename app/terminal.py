from __future__ import annotations

from app.config import Settings


TERMINAL_TEMPORARY_MESSAGE = "This workflow is temporarily unavailable. Please retry in a moment."


def build_terminal_setup_commands(raw_key: str | None, settings: Settings) -> list[str]:
    key_line = (
        f'export AB_API_KEY="{raw_key}"'
        if raw_key
        else '# Generate a fresh AB key to get a copy-ready terminal setup block.'
    )
    return [
        key_line,
        settings.terminal_cli_command,
    ]

