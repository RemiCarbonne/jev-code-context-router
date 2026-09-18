"""Hermes Agent plugin adapter.

The installer/vendors this directory together with the core package. No Hermes
imports are required: registration uses the public plugin context contract.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

from .jev_context_router import ContextRouter, Settings
from .jev_context_router.providers import JevSelector


def _session_cwd(home: Path, session_id: str) -> Path:
    database = home / "state.db"
    if session_id and database.is_file():
        try:
            connection = sqlite3.connect(database)
            row = connection.execute("SELECT git_repo_root,cwd FROM sessions WHERE id=?", (session_id,)).fetchone()
            connection.close()
            for value in row or ():
                if value and Path(value).is_dir():
                    return Path(value).resolve()
        except sqlite3.Error:
            pass
    return Path.cwd().resolve()


def _recent_users(history: Any) -> tuple[str, ...]:
    if not isinstance(history, list):
        return ()
    return tuple(
        str(message.get("content") or "")[:3000]
        for message in history[-8:]
        if isinstance(message, dict) and message.get("role") == "user" and isinstance(message.get("content"), str)
    )


def register(ctx) -> None:
    home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))

    def inject_context(session_id: str = "", user_message: Any = "", conversation_history: Any = None, **_: Any):
        query = user_message if isinstance(user_message, str) else str(user_message or "")
        cwd = _session_cwd(home, session_id)
        settings = Settings.load(cwd=cwd)
        selector = None
        if not settings.api_key:
            key_file = home / ".secrets" / "typesafe_api_key"
            try:
                key = key_file.read_text(encoding="utf-8").strip()
            except OSError:
                key = ""
            if key:
                selector = JevSelector(key, settings)
        result = ContextRouter(settings, selector).route(query, cwd=cwd, recent_user_messages=_recent_users(conversation_history))
        if result.context:
            return {"context": result.context}
        if result.status == "repository-unresolved":
            return {"context": '<code_context status="repository-unresolved">Ask which repository this coding task targets; do not infer it from unrelated assistant history.</code_context>'}
        return None

    ctx.register_hook("pre_llm_call", inject_context)
