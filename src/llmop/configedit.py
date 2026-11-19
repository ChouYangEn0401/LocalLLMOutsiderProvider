"""Comment-preserving edits of ``config.yaml``.

Copied from ``cli.py`` (the "YAML text editing" section) so that other modules (the
local console, later tasks) can edit the config without importing the CLI. ``cli.py``
keeps its own copy for now; the integrator may switch it over to this module.

Differences from the copy in ``cli.py``: errors are :class:`~llmop.config.ConfigError`
instead of ``CliError``, and :func:`_save_edit` logs its warning instead of printing it.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Callable

import yaml

from .config import Config, ConfigError, parse_config

log = logging.getLogger(__name__)


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


_MODEL_RE = re.compile(r"^(model\s*:\s*)(\"[^\"]*\"|'[^']*'|[^#\s]*)(\s*(?:#.*)?)$")


def _text_set_model(text: str, model: str) -> str | None:
    """Set the top-level ``model:`` line (appended when there is none)."""
    lines, nl, bom = _split_lines(text)
    for k, line in enumerate(lines):
        m = _MODEL_RE.match(line)
        if m:
            lines[k] = m.group(1) + _yaml_scalar(model) + m.group(3)
            return _join_lines(lines, nl, bom)
    while lines and not lines[-1].strip():
        lines.pop()
    lines += [f"model: {_yaml_scalar(model)}", ""]
    return _join_lines(lines, nl, bom)


def edit_config_text(
    text: str, text_edit: Callable[[str], str | None], raw_edit: Callable[[dict], dict]
) -> tuple[str, bool]:
    """Apply an edit; returns ``(new_text, comments_kept)``."""
    old_raw = yaml.safe_load(text) or {}
    if not isinstance(old_raw, dict):
        raise ConfigError("the config file's top level must be a mapping")
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


def _raw_set_model(raw: dict, model: str) -> dict:
    raw["model"] = model
    return raw


def set_llm_backend(text: str, btype: str, base_url: str, model: str | None = None) -> tuple[str, bool]:
    """Point the first backend at ``btype``/``base_url`` and optionally set the top-level
    ``model``; comments are kept when possible. Returns ``(new_text, comments_kept)``.
    The backend's ``name`` follows the type only while it was just the old type's name."""
    raw = yaml.safe_load(text) or {}
    first = (raw.get("backends") or [None])[0] if isinstance(raw, dict) else None
    values = {"type": btype, "base_url": base_url}
    if isinstance(first, dict) and first.get("name") == first.get("type") and first.get("name"):
        values = {"name": btype, **values}
    text, kept = edit_config_text(text, lambda t: _text_set_first_backend(t, values),
                                  lambda r: _raw_set_first_backend(r, values))
    if model is not None:
        text, kept2 = edit_config_text(text, lambda t: _text_set_model(t, model),
                                       lambda r: _raw_set_model(r, model))
        kept = kept and kept2
    return text, kept


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
        raise ConfigError(f"the result would be an invalid config ({exc}); nothing was changed") from exc


def _save_edit(path: Path, text: str, kept: bool) -> None:
    _write_text(path, text)
    if not kept:
        log.warning("could not edit %s in place; it was rewritten and its comments were removed", path.name)
