from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

_HERMES_ADAPTER = '''from __future__ import annotations
import os, sqlite3
from pathlib import Path
from typing import Any
from .jev_context_router import ContextRouter, Settings
from .jev_context_router.providers import JevSelector

def _cwd(home: Path, session_id: str) -> Path:
    db=home/'state.db'
    if session_id and db.is_file():
        try:
            con=sqlite3.connect(db); row=con.execute("SELECT git_repo_root,cwd FROM sessions WHERE id=?",(session_id,)).fetchone(); con.close()
            for value in row or ():
                if value and Path(value).is_dir(): return Path(value).resolve()
        except sqlite3.Error: pass
    return Path.cwd().resolve()

def register(ctx):
    home=Path(os.environ.get("HERMES_HOME", Path.home()/'.hermes'))
    def inject_context(session_id="",user_message="",conversation_history=None,**_):
        query=user_message if isinstance(user_message,str) else str(user_message or "")
        cwd=_cwd(home,session_id); settings=Settings.load(cwd=cwd); selector=None
        if not settings.api_key:
            try: key=(home/'.secrets'/'typesafe_api_key').read_text(encoding='utf-8').strip()
            except OSError: key=''
            if key: selector=JevSelector(key,settings)
        recent=tuple(str(m.get('content') or '')[:3000] for m in (conversation_history or [])[-8:] if isinstance(m,dict) and m.get('role')=='user' and isinstance(m.get('content'),str))
        result=ContextRouter(settings,selector).route(query,cwd=cwd,recent_user_messages=recent)
        if result.context: return {'context':result.context}
        if result.status=='repository-unresolved': return {'context':'<code_context status="repository-unresolved">Ask which repository this coding task targets; do not guess from unrelated history.</code_context>'}
        return None
    ctx.register_hook('pre_llm_call',inject_context)
'''
_HERMES_MANIFEST = '''manifest_version: 2
api_version: 1
name: jev-context-router
version: 0.3.0
description: "Repository-aware Jev context routing for coding turns."
author: DazzStudio
kind: standalone
license: MIT
provides_hooks:
  - pre_llm_call
'''


def install_hermes(home: Path, enable: bool = False) -> Path:
    home = home.expanduser().resolve()
    target = home / "plugins" / "jev_context_router"
    package_target = target / "jev_context_router"
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    source = Path(__file__).resolve().parent
    shutil.copytree(source, package_target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (target / "__init__.py").write_text(_HERMES_ADAPTER, encoding="utf-8")
    (target / "plugin.yaml").write_text(_HERMES_MANIFEST, encoding="utf-8")
    if enable:
        subprocess.run(["hermes", "plugins", "enable", "jev-context-router"], check=True)
    return target


def install_claude(project: Path) -> Path:
    project = project.expanduser().resolve()
    settings_path = project / ".claude" / "settings.local.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8")) if settings_path.is_file() else {}
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Refusing to overwrite invalid JSON: {settings_path}") from exc
    hooks = data.setdefault("hooks", {}).setdefault("UserPromptSubmit", [])
    entry = {"matcher": "", "hooks": [{"type": "command", "command": "jev-context claude-hook", "timeout": 30}]}
    if entry not in hooks:
        hooks.append(entry)
    settings_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return settings_path


def install_codex(home: Path | None = None, apply: bool = False, mode: str = "cli") -> str | Path:
    if mode == "mcp":
        command = ["codex", "mcp", "add", "jev-context", "--", "jev-context", "mcp-serve"]
        if apply:
            subprocess.run(command, check=True)
        return " ".join(command)
    if mode != "cli":
        raise ValueError(f"Unsupported Codex integration mode: {mode}")

    home = (home or Path(os.environ.get("CODEX_HOME", "~/.codex"))).expanduser().resolve()
    hooks_path = home / "hooks.json"
    if not apply:
        return f"jev-context install codex --mode cli --home {home} --apply"
    hooks_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(hooks_path.read_text(encoding="utf-8")) if hooks_path.is_file() else {}
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Refusing to overwrite invalid JSON: {hooks_path}") from exc
    groups = data.setdefault("hooks", {}).setdefault("UserPromptSubmit", [])
    entry = {
        "hooks": [{
            "type": "command",
            "command": "jev-context codex-hook",
            "timeout": 30,
            "statusMessage": "Selecting repository context",
            "additionalContextLimit": 16000,
        }]
    }
    if entry not in groups:
        groups.append(entry)
    hooks_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return hooks_path
