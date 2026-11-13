"""``llmop`` command line (also ``python -m llmop``).

    llmop init                     create config.yaml and set the admin password
    llmop serve [--tunnel]         run the gateway (optionally with a public URL)
    llmop add-user NAME            add an account to config.yaml (comments are kept)
    llmop set-password NAME        change an account's password
    llmop hash-password            print a password hash for config.yaml
    llmop check                    validate config.yaml and test the LLM backends
    llmop usage / keys / revoke-key
    llmop tunnel                   only the tunnel, for a server that already runs

The global ``--config PATH`` (default: ``$LLMOP_CONFIG`` or ``config.yaml``) may be
given before or after the command. ``$LLMOP_DATA_DIR`` overrides
``server.data_dir`` (the Docker image sets it to ``/data``).

Console output is plain ASCII on purpose: Windows consoles may not be UTF-8.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import getpass
import ipaddress
import json
import logging
import os
import re
import secrets
import shutil
import socket
import sys
import threading
import time
import webbrowser
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Callable

import yaml

from . import __version__
from .config import BACKEND_TYPES, DEFAULT_URLS, ROLES, Config, ConfigError, load_config, parse_config
from .security import hash_password
from .tunnel import (
    PUBLIC_URL_FILE,
    WINGET_HINT,
    TunnelManager,
    clear_stale_public_url,
    conflicting_config_files,
    find_cloudflared,
)

ENV_CONFIG = "LLMOP_CONFIG"
ENV_DATA_DIR = "LLMOP_DATA_DIR"
ENV_ADMIN_PASSWORD = "LLMOP_ADMIN_PASSWORD"
ENV_IN_CONTAINER = "LLMOP_IN_CONTAINER"
PLACEHOLDER_PREFIX = "REPLACE_ME"
MIN_PASSWORD_LEN = 8
USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,63}$")
DB_NAME = "llmop.sqlite3"
LOG_LEVELS = ("critical", "error", "warning", "info", "debug", "trace")
# cloudflared keeps idle connections to the origin for 90 s; uvicorn must keep them
# open longer or a request can race with uvicorn closing the socket (random 502s).
KEEP_ALIVE_SECONDS = 100


class CliError(Exception):
    """A problem to show the operator (no traceback). ``code`` is the exit code."""

    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.message = message
        self.code = code


# ---------------------------------------------------------------- output helpers
def _out(text: str = "") -> None:
    print(text, flush=True)


def _err(text: str) -> None:
    print(f"[X] {text}", file=sys.stderr, flush=True)


def _warn(text: str) -> None:
    print(f"[!] {text}", file=sys.stderr, flush=True)


def _safe_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError, OSError):
            pass


def _fmt_ts(ts: float | None) -> str:
    if not ts:
        return "-"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def _fmt_int(n: int | None) -> str:
    return f"{int(n or 0):,}"


def _fmt_limit(n: int) -> str:
    return "unlimited" if not n else f"{n:,}"


def _table(headers: list[str], rows: list[list[str]]) -> str:
    cols = [headers] + rows
    widths = [max(len(str(r[i])) for r in cols) for i in range(len(headers))]
    lines = ["  ".join(str(h).ljust(w) for h, w in zip(headers, widths)).rstrip()]
    lines.append("  ".join("-" * w for w in widths))
    for r in rows:
        lines.append("  ".join(str(c).ljust(w) for c, w in zip(r, widths)).rstrip())
    return "\n".join(lines)


# ---------------------------------------------------------------- config helpers
def _config_path(args) -> Path:
    return Path(os.path.expanduser(str(args.config)))


def _load(args) -> Config:
    path = _config_path(args)
    if not path.exists():
        raise CliError(
            f"{path} not found. Create it with:  llmop init\n"
            "    (Windows: double-click scripts\\windows\\Setup.bat)",
            2,
        )
    try:
        cfg = load_config(path)
    except yaml.YAMLError as exc:
        raise CliError(f"{path} is not valid YAML:\n{exc}", 2) from exc
    except (ConfigError, ValueError, TypeError) as exc:  # e.g. tokens_per_day: lots
        raise CliError(f"{path}: {exc}", 2) from exc
    data_dir = os.environ.get(ENV_DATA_DIR, "").strip()
    if data_dir:
        cfg.server.data_dir = data_dir
    return cfg


def placeholder_accounts(cfg: Config) -> list[str]:
    return [u for u, a in cfg.accounts.items() if a.password_hash.startswith(PLACEHOLDER_PREFIX)]


def preflight(cfg: Config) -> tuple[list[str], list[str]]:
    """Problems that stop ``serve`` (errors) and ones it only mentions (warnings)."""
    errors: list[str] = []
    warnings: list[str] = []
    in_container = os.environ.get(ENV_IN_CONTAINER) == "1"
    for user in placeholder_accounts(cfg):
        if in_container:
            how = ("docker compose run --rm --no-deps llmop hash-password\n"
                   "    and paste the printed hash into config.yaml as password_hash")
        else:
            how = (f"llmop set-password {user}\n"
                   "    (or: llmop init --force, or llmop hash-password and paste the hash)")
        errors.append(
            f"account '{user}' still has the placeholder password hash "
            f"({PLACEHOLDER_PREFIX}...). Set a real password first:\n    {how}"
        )
    for user, acc in cfg.accounts.items():
        if not acc.password_hash.startswith(PLACEHOLDER_PREFIX) and not acc.password_hash.startswith("scrypt$"):
            warnings.append(f"account '{user}': password_hash does not look like an llmop hash; "
                            "that account cannot log in (run: llmop set-password " + user + ")")
    if not cfg.accounts:
        warnings.append("config.yaml has no accounts; nobody can log in (llmop add-user NAME)")
    if not any(a.is_admin for a in cfg.accounts.values()) and cfg.accounts:
        warnings.append("no account has role: admin (the admin page will be unreachable)")
    if not cfg.backends:
        warnings.append("config.yaml has no backends; there is no LLM to talk to")
    return errors, warnings


def is_loopback_host(host: str) -> bool:
    h = (host or "").strip().strip("[]")
    if h.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def connect_host(host: str) -> str:
    """Address to *connect* to for a listen address (0.0.0.0 -> 127.0.0.1)."""
    h = (host or "").strip().strip("[]")
    return "127.0.0.1" if h in ("", "0.0.0.0", "::", "*") else h


def local_base_url(host: str, port: int) -> str:
    """URL to reach the server from this machine (the tunnel connects here)."""
    h = connect_host(host)
    if ":" in h:
        h = f"[{h}]"
    return f"http://{h}:{port}"


def forwarded_allow_ips(cfg: Config) -> str:
    items = ["127.0.0.1", "::1"]
    for p in cfg.server.trusted_proxies:
        if p and p not in items:
            items.append(str(p))
    return ",".join(items)


def port_available(host: str, port: int) -> tuple[bool, str]:
    h = (host or "").strip().strip("[]") or "0.0.0.0"
    try:
        infos = socket.getaddrinfo(h, port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE)
    except socket.gaierror:
        return True, ""  # let uvicorn report a bad host name
    family, socktype, proto, _, addr = infos[0]
    sock = socket.socket(family, socktype, proto)
    try:
        if os.name != "nt":  # same as uvicorn; on Windows SO_REUSEADDR means "steal the port"
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(addr)
        return True, ""
    except OSError as exc:
        return False, str(exc)
    finally:
        sock.close()


def port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _open_store(cfg: Config, *, create: bool = False):
    from .store import Store

    path = cfg.data_path / DB_NAME
    if not create and not path.exists():
        return None
    return Store(path)


# ---------------------------------------------------------------- YAML text editing
# PyYAML drops comments when it writes a file, and config.yaml is mostly comments for
# the operator. So edits are done on the text, then verified by parsing the result:
# the edit is kept only if the parsed YAML equals the parsed original with the
# intended change applied. Otherwise we fall back to a plain YAML dump (comments lost).


def _split_lines(text: str) -> tuple[list[str], str, bool]:
    bom = text.startswith("﻿")
    if bom:
        text = text[1:]
    nl = "\r\n" if "\r\n" in text else "\n"
    return text.replace("\r\n", "\n").split("\n"), nl, bom


def _join_lines(lines: list[str], nl: str, bom: bool) -> str:
    return ("﻿" if bom else "") + nl.join(lines)


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _is_content(line: str) -> bool:
    s = line.strip()
    return bool(s) and not s.startswith("#")


def _find_top_section(lines: list[str], key: str) -> tuple[int, int, str] | None:
    """``(header_index, end_index_exclusive, rest_of_header_line)`` of ``key:``."""
    pat = re.compile(rf"^{re.escape(key)}\s*:(.*)$")
    for i, line in enumerate(lines):
        m = pat.match(line)
        if not m:
            continue
        end = len(lines)
        for j in range(i + 1, len(lines)):
            s = lines[j]
            if s and not s[0].isspace() and not s.startswith("#"):
                end = j
                break
        return i, end, m.group(1)
    return None


def _yaml_scalar(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_yaml_scalar(v) for v in value) + "]"
    s = str(value)
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.@-]*", s):
        try:
            if yaml.safe_load(s) == s:
                return s
        except yaml.YAMLError:
            pass
    return json.dumps(s, ensure_ascii=False)


def _entry_lines(entry: dict, indent: int) -> list[str]:
    pad = " " * indent
    out: list[str] = []
    for key, val in entry.items():
        if isinstance(val, dict):
            out.append(f"{pad}{key}:")
            out.extend(_entry_lines(val, indent + 2))
        elif key == "password_hash":
            out.append(f"{pad}{key}: {json.dumps(str(val))}")
        else:
            out.append(f"{pad}{key}: {_yaml_scalar(val)}")
    return out


def _text_add_account(text: str, username: str, entry: dict) -> str | None:
    lines, nl, bom = _split_lines(text)
    sec = _find_top_section(lines, "accounts")
    if sec is None:
        while lines and lines[-1] == "":
            lines.pop()
        lines += ["", "accounts:", f"  {_yaml_scalar(username)}:", *_entry_lines(entry, 4), ""]
        return _join_lines(lines, nl, bom)
    start, end, rest = sec
    rest_val = rest.split(" #", 1)[0].strip()
    if rest_val in ("{}", "null", "~"):
        lines[start] = "accounts:"
    elif rest_val:
        return None  # flow style on one line: use the YAML fallback
    indent = next((_indent_of(lines[j]) for j in range(start + 1, end) if _is_content(lines[j])), None)
    if indent is None:  # only commented examples: follow their indentation
        indent = next((_indent_of(lines[j]) for j in range(start + 1, end)
                       if lines[j].strip() and lines[j][0] == " "), 2)
    if indent <= 0:
        return None
    last = start
    for j in range(start + 1, end):
        if lines[j].strip() and lines[j][0] in " \t":
            last = j
    block = [f"{' ' * indent}{_yaml_scalar(username)}:", *_entry_lines(entry, indent + 2)]
    lines[last + 1:last + 1] = block
    return _join_lines(lines, nl, bom)


def _text_set_password(text: str, username: str, pw_hash: str) -> str | None:
    lines, nl, bom = _split_lines(text)
    sec = _find_top_section(lines, "accounts")
    if sec is None:
        return None
    start, end, _ = sec
    indent = next((_indent_of(lines[j]) for j in range(start + 1, end) if _is_content(lines[j])), None)
    if not indent:
        return None
    names = "|".join(re.escape(q) for q in (username, f'"{username}"', f"'{username}'"))
    key_re = re.compile(rf"^ {{{indent}}}(?:{names})\s*:\s*(#.*)?$")
    for j in range(start + 1, end):
        if not key_re.match(lines[j]):
            continue
        child_indent: int | None = None
        hash_line: int | None = None
        k = j + 1
        while k < end:
            s = lines[k]
            if _is_content(s):
                ind = _indent_of(s)
                if ind <= indent:
                    break
                if child_indent is None:
                    child_indent = ind
                if ind == child_indent and re.match(r"^\s*password_hash\s*:", s):
                    hash_line = k
            k += 1
        new = f"{' ' * (child_indent or indent + 2)}password_hash: {json.dumps(pw_hash)}"
        if hash_line is not None:
            lines[hash_line] = new
        else:
            lines.insert(j + 1, new)
        return _join_lines(lines, nl, bom)
    return None


def _text_set_first_backend(text: str, values: dict[str, str]) -> str | None:
    lines, nl, bom = _split_lines(text)
    sec = _find_top_section(lines, "backends")
    if sec is None:
        return None
    start, end, _ = sec
    first = next((j for j in range(start + 1, end)
                  if _is_content(lines[j]) and lines[j].lstrip().startswith("- ")), None)
    if first is None:
        return None
    dash = _indent_of(lines[first])
    item_end = end
    for k in range(first + 1, end):
        if _is_content(lines[k]) and _indent_of(lines[k]) <= dash:
            item_end = k
            break
    seen: set[str] = set()
    pat = re.compile(r"^(\s*(?:-\s+)?)([a-z_]+)(\s*:\s*)([^#\s]+)(\s*(?:#.*)?)$")
    for k in range(first, item_end):
        m = pat.match(lines[k])
        if m and m.group(2) in values and m.group(2) not in seen:
            seen.add(m.group(2))
            lines[k] = m.group(1) + m.group(2) + m.group(3) + _yaml_scalar(values[m.group(2)]) + m.group(5)
    if seen != set(values):
        return None
    return _join_lines(lines, nl, bom)


def edit_config_text(
    text: str, text_edit: Callable[[str], str | None], raw_edit: Callable[[dict], dict]
) -> tuple[str, bool]:
    """Apply an edit; returns ``(new_text, comments_kept)``."""
    old_raw = yaml.safe_load(text) or {}
    if not isinstance(old_raw, dict):
        raise CliError("the config file's top level must be a mapping", 2)
    expected = raw_edit(copy.deepcopy(old_raw))
    try:
        candidate = text_edit(text)
    except Exception:  # a text edit must never be fatal; the fallback is always correct
        candidate = None
    if candidate is not None:
        try:
            if yaml.safe_load(candidate) == expected:
                return candidate, True
        except yaml.YAMLError:
            pass
    dumped = yaml.safe_dump(expected, sort_keys=False, allow_unicode=True, default_flow_style=False)
    return dumped, False


def _raw_add_account(raw: dict, username: str, entry: dict) -> dict:
    accounts = raw.get("accounts")
    if not isinstance(accounts, dict):
        accounts = {}
    accounts[username] = copy.deepcopy(entry)
    raw["accounts"] = accounts
    return raw


def _raw_set_password(raw: dict, username: str, pw_hash: str) -> dict:
    acc = raw["accounts"][username]
    if not isinstance(acc, dict):
        acc = {}
        raw["accounts"][username] = acc
    acc["password_hash"] = pw_hash
    return raw


def _raw_set_first_backend(raw: dict, values: dict[str, str]) -> dict:
    backends = raw.get("backends")
    if not isinstance(backends, list) or not backends or not isinstance(backends[0], dict):
        raw["backends"] = [dict(values, models=["*"])]
    else:
        backends[0].update(values)
    return raw


def _read_text(path: Path) -> str:
    with open(path, "r", encoding="utf-8", newline="") as fh:  # keep CRLF / BOM as they are
        return fh.read()


def _write_text(path: Path, text: str) -> None:
    """Atomic replace, so the running server never reads a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = text.encode("utf-8")
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    try:
        if path.exists():
            shutil.copymode(path, tmp)
        os.replace(tmp, path)
    except OSError:  # e.g. Windows: file held open by an editor
        path.write_bytes(data)
        try:
            tmp.unlink()
        except OSError:
            pass


def _validate_text(text: str, path: Path) -> Config:
    try:
        return parse_config(yaml.safe_load(text), path=path.resolve())
    except (ConfigError, yaml.YAMLError, ValueError, TypeError) as exc:
        raise CliError(f"the result would be an invalid config ({exc}); nothing was changed", 2) from exc


def _save_edit(path: Path, text: str, kept: bool) -> None:
    _write_text(path, text)
    if not kept:
        _warn(f"could not edit {path.name} in place; it was rewritten and its comments were removed")


# ---------------------------------------------------------------- passwords
def _check_password(pw: str) -> str:
    if len(pw) < MIN_PASSWORD_LEN:
        raise CliError(f"the password must be at least {MIN_PASSWORD_LEN} characters", 2)
    return pw


def ask_new_password(given: str | None, label: str, env_var: str | None = None) -> str:
    """``given`` (from --password) > ``$env_var`` > interactive prompt (typed twice)."""
    if given is None and env_var:
        given = os.environ.get(env_var) or None
    if given is not None:
        return _check_password(given)
    for _ in range(3):
        try:
            pw = getpass.getpass(f"New password for {label} (input is hidden): ")
            if len(pw) < MIN_PASSWORD_LEN:
                _err(f"too short: use at least {MIN_PASSWORD_LEN} characters")
                continue
            again = getpass.getpass("Type it again: ")
        except EOFError as exc:
            raise CliError("no password given (use --password, or run this in a terminal)", 2) from exc
        if pw != again:
            _err("the two passwords are different; try again")
            continue
        return pw
    raise CliError("no password was set", 1)


def generate_password() -> str:
    return secrets.token_urlsafe(12)


def _password_for(args, label: str, env_var: str | None = None) -> tuple[str, bool]:
    if getattr(args, "generate", False):
        return generate_password(), True
    return ask_new_password(getattr(args, "password", None), label, env_var), False


# ---------------------------------------------------------------- lazy hooks (mocked in tests)
def _build_state(cfg: Config):
    from .app import build_state

    return build_state(cfg)


def _create_app(state):
    from .app import create_app

    return create_app(state=state)


def _run_uvicorn(app, **kwargs) -> None:
    import uvicorn

    uvicorn.run(app, **kwargs)


def _backend_health(cfg: Config) -> list[dict]:
    """Same call as ``asyncio.run(state.router.health())`` without opening the database."""
    from .backends import BackendRouter
    from .config import ConfigHolder

    async def run() -> list[dict]:
        router = BackendRouter(ConfigHolder(cfg))
        try:
            return await asyncio.wait_for(router.health(), timeout=60)
        finally:
            await router.aclose()

    return asyncio.run(run())


def _example_text() -> str:
    try:
        from importlib import resources

        return (resources.files("llmop") / "examples" / "config.example.yaml").read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError, OSError) as exc:
        repo = Path(__file__).resolve().parents[2] / "config.example.yaml"
        if repo.exists():
            return repo.read_text(encoding="utf-8")
        raise CliError("the example config is missing from this installation", 2) from exc


# ---------------------------------------------------------------- commands
def cmd_init(args) -> int:
    path = _config_path(args)
    if path.exists() and not args.force:
        raise CliError(f"{path} already exists; nothing was changed.\n"
                       "    To start over use:  llmop init --force   (the old file is kept as .bak)\n"
                       "    To change a password:  llmop set-password admin", 1)
    text = _example_text()
    example = yaml.safe_load(text) or {}
    accounts = example.get("accounts") or {}
    admin = next((u for u, a in accounts.items() if isinstance(a, dict) and a.get("role") == "admin"),
                 next(iter(accounts), "admin"))
    _out(f"Creating {path}")
    password = ask_new_password(args.admin_password, f"the web login '{admin}'", ENV_ADMIN_PASSWORD)
    pw_hash = hash_password(password)
    if admin in accounts:
        text, kept = edit_config_text(text, lambda t: _text_set_password(t, admin, pw_hash),
                                      lambda r: _raw_set_password(r, admin, pw_hash))
    else:
        entry = {"role": "admin", "password_hash": pw_hash}
        text, kept = edit_config_text(text, lambda t: _text_add_account(t, admin, entry),
                                      lambda r: _raw_add_account(r, admin, entry))
    if args.backend_type or args.backend_url:
        btype = (args.backend_type or "ollama").lower()
        values = {"name": btype, "type": btype, "base_url": args.backend_url or DEFAULT_URLS[btype]}
        text, kept2 = edit_config_text(text, lambda t: _text_set_first_backend(t, values),
                                       lambda r: _raw_set_first_backend(r, values))
        kept = kept and kept2
    _validate_text(text, path)
    if path.exists():
        backup = path.with_name(path.name + ".bak")
        shutil.copy2(path, backup)
        _out(f"    old file saved as {backup}")
    _save_edit(path, text, kept)
    cfg = load_config(path)
    backend = cfg.backends[0] if cfg.backends else None
    _out(f"[OK] created {path}")
    _out(f"     web login: {admin} / the password you just typed")
    if backend:
        _out(f"     LLM backend: {backend.type} at {backend.base_url}")
    _out("")
    _out("Next steps:")
    _out("  1. Make sure your LLM server runs (Ollama: http://127.0.0.1:11434).  Test: llmop check")
    _out("  2. Start locally:      llmop serve            (Windows: Start.bat)")
    _out("     With a public URL:  llmop serve --tunnel   (Windows: Start-With-Tunnel.bat)")
    _out("  3. Add a friend:       llmop add-user NAME    (Windows: Add-User.bat)")
    return 0


def cmd_hash_password(args) -> int:
    pw = ask_new_password(args.password, "the account")
    _out(hash_password(pw))
    print("Paste this into config.yaml as:  password_hash: \"<the line above>\"",
          file=sys.stderr, flush=True)
    return 0


def cmd_add_user(args) -> int:
    path = _config_path(args)
    if not path.exists():
        raise CliError(f"{path} not found. Create it first with:  llmop init", 2)
    username = args.username
    if not USERNAME_RE.match(username):
        raise CliError("a username may use letters, digits and . _ @ - (max 64 characters)", 2)
    text = _read_text(path)
    raw = yaml.safe_load(text) or {}
    existing = raw.get("accounts") if isinstance(raw, dict) else None
    if isinstance(existing, dict) and username in {str(k) for k in existing}:
        raise CliError(f"account '{username}' already exists. To change its password: "
                       f"llmop set-password {username}", 1)
    password, generated = _password_for(args, f"'{username}'")
    entry: dict = {"role": args.role}
    if args.display_name:
        entry["display_name"] = args.display_name
    entry["password_hash"] = hash_password(password)
    if args.models:
        entry["models"] = [m.strip() for m in args.models.split(",") if m.strip()]
    quota = {k: getattr(args, k) for k in ("tokens_per_day", "tokens_per_month", "requests_per_minute")
             if getattr(args, k) is not None}
    if quota:
        entry["quota"] = quota
    new_text, kept = edit_config_text(text, lambda t: _text_add_account(t, username, entry),
                                      lambda r: _raw_add_account(r, username, entry))
    cfg = _validate_text(new_text, path)
    _save_edit(path, new_text, kept)
    acc = cfg.accounts[username]
    _out(f"[OK] added account '{username}' (role {acc.role}) to {path}")
    if generated:
        _out(f"     password: {password}")
        _out("     (shown only once - send it to them privately)")
    _out(f"     tokens/day {_fmt_limit(acc.quota.tokens_per_day)}, "
         f"tokens/month {_fmt_limit(acc.quota.tokens_per_month)}, "
         f"requests/minute {_fmt_limit(acc.quota.requests_per_minute)}")
    _out("     A running server picks this up automatically (no restart needed).")
    return 0


def cmd_set_password(args) -> int:
    path = _config_path(args)
    if not path.exists():
        raise CliError(f"{path} not found. Create it first with:  llmop init", 2)
    username = args.username
    text = _read_text(path)
    raw = yaml.safe_load(text) or {}
    accounts = raw.get("accounts") if isinstance(raw, dict) else None
    if not isinstance(accounts, dict) or username not in accounts:
        raise CliError(f"there is no account '{username}' in {path} (llmop add-user {username})", 1)
    password, generated = _password_for(args, f"'{username}'")
    pw_hash = hash_password(password)
    new_text, kept = edit_config_text(text, lambda t: _text_set_password(t, username, pw_hash),
                                      lambda r: _raw_set_password(r, username, pw_hash))
    cfg = _validate_text(new_text, path)
    _save_edit(path, new_text, kept)
    _out(f"[OK] new password set for '{username}'")
    if generated:
        _out(f"     password: {password}   (shown only once)")
    data_dir = os.environ.get(ENV_DATA_DIR, "").strip()
    if data_dir:
        cfg.server.data_dir = data_dir
    store = _open_store(cfg)
    if store is not None:
        try:
            n = store.delete_user_sessions(username)
        finally:
            store.close()
        if n:
            _out(f"     logged out {n} web session(s) of '{username}'")
    return 0


def _make_banner(cfg: Config, local: str, tunnel) -> list[str]:
    line = "=" * 66
    rows = [
        "",
        line,
        f"  llmop {__version__} - Local LLM Outsider Provider",
        "",
        f"  Local     : {local}/   (this computer only)",
        f"  Login     : {local}/ui/login",
        f"  API base  : {local}/v1",
    ]
    if cfg.server.public_url:
        rows.append(f"  Public    : {cfg.server.public_url.rstrip('/')}   (server.public_url)")
    if tunnel is not None:
        if tunnel.status == "error":
            rows.append("  Tunnel    : NOT running - see the message above")
        else:
            rows.append(f"  Tunnel    : {tunnel.mode} tunnel starting; the public URL appears below")
    elif not cfg.server.public_url:
        rows.append("  Public    : none (local only). For a public URL: llmop serve --tunnel")
    rows += [
        f"  Config    : {cfg.path}   (edits apply automatically)",
        f"  Data      : {cfg.data_path}",
        "  Stop      : press Ctrl+C or close this window",
        line,
        "",
    ]
    return rows


def cmd_serve(args) -> int:
    cfg = _load(args)
    host = args.host or cfg.server.host
    port = int(args.port or cfg.server.port)
    errors, warnings = preflight(cfg)
    for w in warnings:
        _warn(w)
    if errors:
        for e in errors:
            _err(e)
        _err("not starting.")
        return 2
    in_container = os.environ.get(ENV_IN_CONTAINER) == "1"
    if not is_loopback_host(host) and not in_container:
        bar = "!" * 66
        for row in (bar,
                    f"!!! WARNING: listening on {host}, not 127.0.0.1.",
                    "!!! Other computers on your network (and the internet, if your router",
                    "!!! forwards the port) can reach llmop directly. The Cloudflare tunnel",
                    "!!! does NOT need this. Use server.host: 127.0.0.1 unless you know why.",
                    bar):
            _warn(row)
    ok, why = port_available(host, port)
    if not ok:
        _err(f"cannot listen on {host}:{port} ({why}).")
        _err("Is llmop already running in another window? Otherwise another program uses the "
             "port (or Windows reserved it); try --port 8781 or change server.port.")
        return 2

    level = args.log_level or "info"
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.DEBUG if level == "trace" else level.upper(),
                            format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    local = local_base_url(host, port)
    want_tunnel = cfg.tunnel.enabled if args.tunnel is None else args.tunnel
    if args.tunnel is None and cfg.tunnel.mode == "off":
        want_tunnel = False
    state = _build_state(cfg)
    try:  # a dead URL from a killed tunnel window must not show up on the web page
        clear_stale_public_url(state.store, cfg.data_path)
    except Exception:
        pass
    tunnel = None
    try:
        if want_tunnel:
            settings = cfg.tunnel if cfg.tunnel.mode != "off" else replace(cfg.tunnel, mode="quick")
            tunnel = TunnelManager(settings, local, state.store, cfg.data_path,
                                   public_url=cfg.server.public_url)
            state.tunnel = tunnel
            if not tunnel.start():
                _warn("continuing without a public URL (local only)")
        for row in _make_banner(cfg, local, tunnel):
            _out(row)
        if args.open:
            timer = threading.Timer(1.5, webbrowser.open, args=(f"{local}/",))
            timer.daemon = True
            timer.start()
        app = _create_app(state)
        try:
            _run_uvicorn(
                app,
                host=host,
                port=port,
                proxy_headers=True,
                forwarded_allow_ips=forwarded_allow_ips(cfg),
                log_level=level,
                access_log=cfg.server.access_log,
                timeout_keep_alive=KEEP_ALIVE_SECONDS,
                server_header=False,
            )
        except KeyboardInterrupt:  # uvicorn re-raises Ctrl+C after a graceful shutdown
            pass
    finally:
        if tunnel is not None:
            _out("stopping the tunnel ...")
            tunnel.stop()
    _out("llmop stopped.")
    return 0


def cmd_tunnel(args) -> int:
    cfg = _load(args)
    local = (args.url or local_base_url(cfg.server.host, cfg.server.port)).rstrip("/")
    settings = cfg.tunnel if cfg.tunnel.mode != "off" else replace(cfg.tunnel, mode="quick")
    if args.cloudflared:
        settings = replace(settings, cloudflared=args.cloudflared)
    if not args.url and not port_open(connect_host(cfg.server.host), cfg.server.port):
        _warn(f"nothing answers at {local} yet. Start the server too (llmop serve / Start.bat); "
              "until then visitors get an error page.")
    _out("Only the tunnel runs in this window. If 'llmop serve --tunnel' already runs, close this:")
    _out("two tunnels for one server are pointless.")
    store = _open_store(cfg, create=True)
    mgr = TunnelManager(settings, local, store, cfg.data_path, public_url=cfg.server.public_url)
    try:
        if not mgr.start():
            return 1
        _out(f"Starting cloudflared for {local} ... (Ctrl+C to stop)")
        mgr.wait()
    except KeyboardInterrupt:
        _out("stopping the tunnel ...")
    finally:
        mgr.stop()
        if store is not None:
            store.close()
    return 0


def cmd_check(args) -> int:
    cfg = _load(args)
    problems = 0
    _out(f"[OK] config     {cfg.path}")
    _out(f"     server     {local_base_url(cfg.server.host, cfg.server.port)}   (host {cfg.server.host})")
    _out(f"     data       {cfg.data_path}")
    public = cfg.server.public_url or "(not fixed; a quick tunnel URL is used while the tunnel runs)"
    _out(f"     public URL {public}")
    if not is_loopback_host(cfg.server.host) and os.environ.get(ENV_IN_CONTAINER) != "1":
        _warn(f"server.host is {cfg.server.host}: reachable from other computers, not only the tunnel")
        problems += 1
    errors, warnings = preflight(cfg)
    for e in errors:
        _err(e)
    for w in warnings:
        _warn(w)
    problems += len(errors) + len(warnings)

    _out("")
    _out(f"Accounts ({len(cfg.accounts)}):")
    placeholders = set(placeholder_accounts(cfg))
    rows = []
    for name, acc in cfg.accounts.items():
        q = acc.quota
        state = "DISABLED" if acc.disabled else ("NO PASSWORD" if name in placeholders else "ok")
        rows.append([name, acc.role, state, ",".join(acc.models), _fmt_limit(q.tokens_per_day),
                     _fmt_limit(q.tokens_per_month), _fmt_limit(q.requests_per_minute),
                     _fmt_limit(q.max_tokens_per_request)])
    if rows:
        _out(_table(["user", "role", "status", "models", "tokens/day", "tokens/month",
                     "req/min", "max_tokens"], rows))

    _out("")
    t = cfg.tunnel
    exe = find_cloudflared(t.cloudflared)
    _out(f"Tunnel: enabled={t.enabled} mode={t.mode}  cloudflared: {exe or 'NOT FOUND'}")
    if exe is None:
        msg = f"cloudflared is not installed (needed for a public URL): {WINGET_HINT}"
        if t.enabled:
            _warn(msg)
            problems += 1
        else:
            _out(f"     {msg}")
    if t.mode == "named" and not t.token:
        _warn("tunnel.mode is named but tunnel.token is empty")
        problems += 1
    if t.mode != "named":
        for f in conflicting_config_files():
            _warn(f"{f} exists; quick tunnels will not start while it is there")
            problems += 1
    pub_file = cfg.data_path / PUBLIC_URL_FILE
    if pub_file.exists():
        _out(f"     last tunnel URL: {pub_file.read_text(encoding='utf-8').strip()}  ({pub_file})")

    _out("")
    _out("Backends:")
    try:
        health = _backend_health(cfg)
    except Exception as exc:  # the backends package may be missing or broken
        _warn(f"could not test the backends ({type(exc).__name__}: {exc})")
        for b in cfg.backends:
            _out(f"  {b.name:<12} {b.type:<9} {b.base_url}")
        return 1
    for h in health:
        models = h.get("models")
        names = [str(m) for m in models] if isinstance(models, (list, tuple)) else []
        count = h.get("model_count", len(names) if names else models)
        if h.get("reachable"):
            latency = f"  {h['latency_ms']} ms" if isinstance(h.get("latency_ms"), int) else ""
            _out(f"  [OK] {h.get('name')} ({h.get('type')}) {h.get('base_url', '')}  "
                 f"models: {count}{latency}")
            if names:
                more = f" ... (+{int(count) - 5})" if isinstance(count, int) and count > 5 else ""
                _out(f"       {', '.join(names[:5])}{more}")
            if count == 0:
                _warn(f"{h.get('name')} has no models yet (Ollama: ollama pull <model>)")
                problems += 1
        else:
            problems += 1
            _out(f"  [X]  {h.get('name')} ({h.get('type')}) {h.get('base_url', '')}  "
                 f"NOT reachable: {h.get('error') or 'unknown error'}")
    if not health:
        _out("  (none)")
    _out("")
    _out("All good." if not problems else f"{problems} thing(s) need attention (see above).")
    return 0 if not problems else 1


def cmd_usage(args) -> int:
    from .quota import day_start

    cfg = _load(args)
    store = _open_store(cfg)
    if store is None:
        _out(f"No usage recorded yet ({cfg.data_path / DB_NAME} does not exist).")
        return 0
    try:
        since = time.time() - max(1, args.days) * 86400
        today = day_start()
        if args.user:
            user = args.user
            s = store.usage_summary(user, since)
            acc = cfg.account(user)
            _out(f"{user}: last {args.days} days - {_fmt_int(s['requests'])} requests, "
                 f"{_fmt_int(s['total'])} tokens (prompt {_fmt_int(s['prompt'])}, "
                 f"completion {_fmt_int(s['completion'])})")
            if acc is not None:
                _out(f"today: {_fmt_int(store.tokens_since(user, today))} / "
                     f"{_fmt_limit(acc.quota.tokens_per_day)} tokens")
            else:
                _out("(this account is no longer in config.yaml)")
            days = store.usage_by_day(user, since, _utc_offset())
            if days:
                _out("")
                _out(_table(["day", "requests", "prompt", "completion", "total"],
                            [[d["day"], _fmt_int(d["requests"]), _fmt_int(d["prompt"]),
                              _fmt_int(d["completion"]), _fmt_int(d["total"])] for d in days]))
            models = store.usage_by_model(user, since)
            if models:
                _out("")
                _out(_table(["model", "requests", "tokens"],
                            [[m["model"], _fmt_int(m["requests"]), _fmt_int(m["total"])] for m in models]))
            return 0
        users = list(cfg.accounts)
        users += [r["username"] for r in store.usage_by_user(since) if r["username"] not in cfg.accounts]
        rows = []
        for user in users:
            s = store.usage_summary(user, since)
            acc = cfg.account(user)
            limit = _fmt_limit(acc.quota.tokens_per_day) if acc else "(removed)"
            rows.append([user, _fmt_int(s["requests"]), _fmt_int(s["prompt"]),
                         _fmt_int(s["completion"]), _fmt_int(s["total"]),
                         f"{_fmt_int(store.tokens_since(user, today))} / {limit}"])
        _out(f"Usage in the last {args.days} days:")
        _out(_table(["user", "requests", "prompt", "completion", "total", "today / daily limit"], rows))
    finally:
        store.close()
    return 0


def _utc_offset() -> int:
    from .quota import utc_offset_seconds

    return utc_offset_seconds()


def cmd_keys(args) -> int:
    cfg = _load(args)
    store = _open_store(cfg)
    if store is None:
        _out("No API keys yet.")
        return 0
    try:
        keys = store.list_api_keys(args.user, include_revoked=args.all)
    finally:
        store.close()
    if not keys:
        _out("No API keys" + (f" for '{args.user}'" if args.user else "") + ".")
        return 0
    now = time.time()
    rows = []
    for k in keys:
        if k.revoked_at:
            status = "revoked"
        elif k.expires_at and k.expires_at <= now:
            status = "expired"
        else:
            status = "active"
        rows.append([k.id, k.username, k.name or "-", k.display, _fmt_ts(k.created_at),
                     _fmt_ts(k.last_used_at), _fmt_ts(k.expires_at), status])
    _out(_table(["id", "user", "name", "key", "created", "last used", "expires", "status"], rows))
    return 0


def cmd_revoke_key(args) -> int:
    cfg = _load(args)
    store = _open_store(cfg)
    if store is None:
        raise CliError("there are no API keys yet", 1)
    try:
        ok = store.revoke_api_key(args.key_id)
    finally:
        store.close()
    if not ok:
        raise CliError(f"no active API key with id '{args.key_id}' (see: llmop keys)", 1)
    _out(f"[OK] revoked {args.key_id}; it stops working immediately")
    return 0


# ---------------------------------------------------------------- parser
EPILOG = """\
first time:
  llmop init                     create config.yaml (asks for the admin password)
  llmop check                    is everything configured / is the LLM reachable?
  llmop serve --tunnel --open    start with a public https://...trycloudflare.com URL
"""


def build_parser() -> argparse.ArgumentParser:
    default_config = os.environ.get(ENV_CONFIG) or "config.yaml"
    # --config is accepted before *and* after the command
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", "-c", default=argparse.SUPPRESS, metavar="PATH",
                        help="config file (default: $LLMOP_CONFIG or config.yaml)")

    p = argparse.ArgumentParser(
        prog="llmop",
        description="Share local LLMs (Ollama, vLLM, ...) with login, API keys and quotas.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--config", "-c", default=default_config, metavar="PATH",
                   help="config file (default: $LLMOP_CONFIG or config.yaml)")
    p.add_argument("--version", action="version", version=f"llmop {__version__}")
    sub = p.add_subparsers(dest="command", metavar="COMMAND")

    def add(name: str, func, help_: str) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, parents=[common], help=help_, description=help_)
        sp.set_defaults(func=func)
        return sp

    sp = add("init", cmd_init, "create config.yaml from the example and set the admin password")
    sp.add_argument("--force", action="store_true", help="overwrite an existing config (keeps a .bak)")
    sp.add_argument("--admin-password", metavar="PW",
                    help=f"admin password (default: ${ENV_ADMIN_PASSWORD}, else ask)")
    sp.add_argument("--backend-type", choices=BACKEND_TYPES, help="type of your LLM server (default ollama)")
    sp.add_argument("--backend-url", metavar="URL", help="URL of your LLM server, e.g. http://127.0.0.1:11434")

    sp = add("hash-password", cmd_hash_password, "print a password hash to paste into config.yaml")
    sp.add_argument("--password", metavar="PW", help="the password (default: ask, hidden)")

    sp = add("add-user", cmd_add_user, "add an account to config.yaml (keeps your comments)")
    sp.add_argument("username")
    sp.add_argument("--role", choices=ROLES, default="user")
    sp.add_argument("--password", metavar="PW", help="the password (default: ask, hidden)")
    sp.add_argument("--generate", action="store_true", help="generate a random password and print it")
    sp.add_argument("--display-name", metavar="TEXT")
    sp.add_argument("--models", metavar="LIST", help='comma separated models this account may use (default "*")')
    sp.add_argument("--tokens-per-day", type=int, metavar="N", help="0 = unlimited (default: default_quota)")
    sp.add_argument("--tokens-per-month", type=int, metavar="N")
    sp.add_argument("--requests-per-minute", type=int, metavar="N")

    sp = add("set-password", cmd_set_password, "change an account's password in config.yaml")
    sp.add_argument("username")
    sp.add_argument("--password", metavar="PW", help="the password (default: ask, hidden)")
    sp.add_argument("--generate", action="store_true", help="generate a random password and print it")

    sp = add("serve", cmd_serve, "run the gateway (web UI + OpenAI-compatible API)")
    sp.add_argument("--host", help="listen address (default: server.host, normally 127.0.0.1)")
    sp.add_argument("--port", type=int, help="port (default: server.port, normally 8780)")
    sp.add_argument("--tunnel", action=argparse.BooleanOptionalAction, default=None,
                    help="also start a Cloudflare tunnel (default: tunnel.enabled)")
    sp.add_argument("--open", action="store_true", help="open the web page in the browser")
    sp.add_argument("--log-level", choices=LOG_LEVELS, default="info")

    sp = add("tunnel", cmd_tunnel, "run only the Cloudflare tunnel, for a server that already runs")
    sp.add_argument("--url", help="local server URL (default: from config, http://127.0.0.1:8780)")
    sp.add_argument("--cloudflared", metavar="PATH", help="path to cloudflared(.exe)")

    add("check", cmd_check, "validate config.yaml, list accounts, test the LLM backends")

    sp = add("usage", cmd_usage, "token usage per account")
    sp.add_argument("--days", type=int, default=30)
    sp.add_argument("--user", metavar="NAME", help="details for one account")

    sp = add("keys", cmd_keys, "list API keys (never shows the secret part)")
    sp.add_argument("--user", metavar="NAME")
    sp.add_argument("--all", action="store_true", help="include revoked keys")

    sp = add("revoke-key", cmd_revoke_key, "revoke an API key by its id (see: llmop keys)")
    sp.add_argument("key_id", metavar="ID")
    return p


def _exit_code(exc: SystemExit) -> int:
    if exc.code is None:
        return 0
    if isinstance(exc.code, int):
        return exc.code
    print(exc.code, file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    _safe_console()
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # --help / --version / usage errors
        return _exit_code(exc)
    func = getattr(args, "func", None)
    if func is None:
        parser.print_help()
        return 1
    try:
        return int(func(args) or 0)
    except CliError as exc:
        _err(exc.message)
        return exc.code
    except ConfigError as exc:
        _err(f"config problem: {exc}")
        return 2
    except yaml.YAMLError as exc:
        _err(f"the config file is not valid YAML: {exc}")
        return 2
    except KeyboardInterrupt:
        _out("")
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
