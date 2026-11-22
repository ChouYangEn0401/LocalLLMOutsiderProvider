"""SDK 的單元測試：錯誤分類（假的 HTTP）、設定檔、權限、命令列、import 不載入伺服器套件。不開任何埠。"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys

import httpx
import pytest

from llmop import crypto, paths
from llmop.client import cli as client_cli
from llmop.protocol import b64u_decode, b64u_encode
from llmop.sdk import AsyncClient, Client, SdkError, add_plain, list_profiles, set_link
from llmop.sdk import cli as sdk_cli
from llmop.sdk import profiles as store
from llmop.sdk.errors import KIND_HINTS, kind_for
from llmop.sdk.perms import check_private

LINK = "https://api.example.test/gate-0123456789"
KEY = "sk-llop-" + "x" * 40
CHAT_OK = {"id": "c1", "object": "chat.completion",
           "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}]}


def _client(handler, home) -> Client:
    add_plain(LINK, KEY, profile="t", home=home)
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return Client.from_profile("t", home=home, http=http)


def _err(status: int, code: str | None, *, headers=None, text: str | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if text is not None:
            return httpx.Response(status, text=text, headers=headers)
        return httpx.Response(status, json={"error": {"message": "m", "type": "x", "code": code}}, headers=headers)
    return handler


@pytest.mark.parametrize(("status", "code", "kind"), [
    (401, None, "auth"),
    (401, "key_expired", "auth"),
    (401, "key_revoked", "auth"),
    (401, "key_exhausted", "quota"),
    (429, "insufficient_quota", "quota"),
    (429, "queue_wait", "rate"),
    (429, "rate_limit_exceeded", "rate"),
    (403, "encryption_required", "bad_request"),
    (404, "model_not_found", "bad_request"),
    (400, "invalid_json", "bad_request"),
    (503, "queue_full", "server"),
    (500, "internal_error", "server"),
])
def test_plain_errors_are_classified(tmp_path, status, code, kind):
    with _client(_err(status, code, headers={"Retry-After": "12"}), tmp_path) as c:
        with pytest.raises(SdkError) as exc:
            c.chat({"messages": [{"role": "user", "content": "x"}]})
    assert exc.value.kind_hint == kind
    assert exc.value.status == status
    assert exc.value.retry_after == 12.0
    assert exc.value.code == (code or "invalid_api_key")


@pytest.mark.parametrize(("status", "text", "kind"), [
    (404, "Not Found", "link_changed"),
    (530, "<html>error 1033</html>", "offline"),
    (502, "<html>bad gateway</html>", "offline"),
    (524, "<html>a timeout occurred</html>", "timeout"),
])
def test_non_json_responses(tmp_path, status, text, kind):
    with _client(_err(status, None, text=text), tmp_path) as c:
        with pytest.raises(SdkError) as exc:
            c.chat({"messages": []})
    assert exc.value.kind_hint == kind
    if kind == "link_changed":
        assert exc.value.message.startswith("API 連結已更換，請向服務提供者拿新連結")


def test_network_errors(tmp_path):
    def refuse(request):
        raise httpx.ConnectError("refused")

    def slow(request):
        raise httpx.ReadTimeout("slow")

    with _client(refuse, tmp_path) as c, pytest.raises(SdkError) as exc:
        c.models()
    assert exc.value.kind_hint == "offline" and exc.value.status is None
    with _client(slow, tmp_path / "b") as c, pytest.raises(SdkError) as exc:
        c.models()
    assert exc.value.kind_hint == "timeout"


def test_kind_for_fallbacks():
    assert kind_for("bad_signature", 403) == "trust"
    assert kind_for("decrypt_failed", 400) == "trust"
    assert kind_for("", 418) == "bad_request"
    assert kind_for("", None) == "unknown"
    assert SdkError("x", kind_hint="nonsense").kind_hint == "unknown"
    assert set(KIND_HINTS) == {"quota", "auth", "rate", "link_changed", "offline", "timeout", "trust",
                               "bad_request", "server", "unknown"}


def test_plain_chat_sends_key_and_returns_completion(tmp_path):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=CHAT_OK)

    with _client(handler, tmp_path) as c:
        out = c.chat({"messages": [{"role": "user", "content": "x"}], "temperature": 0.1}, max_tokens=7)
    assert out == CHAT_OK
    assert seen["url"] == LINK + "/v1/chat/completions"
    assert seen["auth"] == f"Bearer {KEY}"
    assert seen["body"]["max_tokens"] == 7 and seen["body"]["stream"] is False
    assert seen["body"]["temperature"] == 0.1


def test_chat_rejects_stream_and_bad_tokens(tmp_path):
    with _client(_err(500, None), tmp_path) as c:
        with pytest.raises(SdkError, match="串流"):
            c.chat({"messages": [], "stream": True})
        with pytest.raises(SdkError):
            c.chat({"messages": []}, max_tokens=0)
        with pytest.raises(SdkError) as exc:
            c.chat({"messages": []}, transport="e2e")
        assert exc.value.kind_hint == "trust"


def test_async_client_wraps_sync(tmp_path):
    add_plain(LINK, KEY, profile="a", home=tmp_path)
    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=CHAT_OK)))

    async def go():
        async with AsyncClient.from_profile("a", home=tmp_path, http=http) as ac:
            assert ac.transport == "plain"
            return await ac.chat({"messages": []})

    assert asyncio.run(go()) == CHAT_OK


# --- 設定檔 ---------------------------------------------------------------


def test_profile_names_and_urls(tmp_path):
    for bad in ("", "../x", "a/b", "a.b", "-x", "x" * 65):
        with pytest.raises(SdkError):
            add_plain(LINK, KEY, profile=bad, home=tmp_path)
    with pytest.raises(SdkError):
        add_plain("http://api.example.test/g", KEY, profile="p", home=tmp_path)  # 非 loopback 的 http
    with pytest.raises(SdkError):
        add_plain(LINK + "?q=1", KEY, profile="p", home=tmp_path)
    with pytest.raises(SdkError):
        add_plain(LINK, "not-a-key", profile="p", home=tmp_path)
    add_plain("http://127.0.0.1:8782/g", KEY, profile="p", home=tmp_path)  # loopback 可以 http
    with pytest.raises(SdkError) as exc:
        add_plain(LINK, KEY, profile="p", home=tmp_path)
    assert exc.value.code == "profile_exists"


def test_profile_round_trip_and_permissions(tmp_path):
    p = add_plain(LINK, KEY, profile="work", home=tmp_path, max_tokens=512)
    assert list_profiles(home=tmp_path) == ["work"]
    loaded = store.load("work", home=tmp_path)
    assert (loaded.api_link, loaded.api_key, loaded.transport, loaded.default_max_tokens) == (LINK, KEY, "plain", 512)
    assert KEY not in repr(loaded)
    ok, note = check_private(p.config_path)
    assert ok, note
    set_link("https://api.example.test/new-gate", profile="work", home=tmp_path)
    assert store.load("work", home=tmp_path).api_link == "https://api.example.test/new-gate"
    assert check_private(p.config_path)[0]  # 原子替換後權限仍然收緊
    assert store.mask_link(LINK) == "https://api.example.test/…"


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL")
def test_windows_acl_has_only_current_user(tmp_path):
    p = add_plain(LINK, KEY, profile="acl", home=tmp_path)
    out = subprocess.run(["icacls", str(p.config_path)], capture_output=True).stdout.decode("mbcs", "replace")
    aces = [line for line in out.replace(str(p.config_path), "").splitlines() if ":(" in line]
    assert len(aces) == 1, out


def test_legacy_config_is_default_profile(tmp_path):
    worker_pub = b64u_encode(crypto.public_raw(crypto.generate_worker_key()))
    legacy = tmp_path / "client"
    legacy.mkdir(parents=True)
    paths.write_json(legacy / "config.json", {"gateway_url": LINK, "api_key": KEY, "pinned_workers": [worker_pub]},
                     secret=True)
    assert list_profiles(home=tmp_path) == ["default"]
    prof = store.load("default", home=tmp_path)
    assert prof.legacy and prof.pinned_workers[0].public_key == worker_pub
    assert prof.pinned_workers[0].key_id == crypto.worker_key_id(b64u_decode(worker_pub))
    assert prof.resolved_transport() == "plain"  # 沒有簽章金鑰
    set_link("https://api.example.test/rotated", profile="default", home=tmp_path)
    data = paths.read_json(legacy / "config.json")
    assert set(data) <= {"gateway_url", "api_key", "pinned_workers", "proxy_port", "default_max_tokens"}
    assert data["gateway_url"] == "https://api.example.test/rotated"
    from llmop.client.config import load_config  # 舊的代理照樣讀得懂
    assert load_config(legacy / "config.json").gateway_url == "https://api.example.test/rotated"


def test_add_plain_has_no_account_actions(tmp_path):
    with _client(_err(500, None), tmp_path) as c:
        with pytest.raises(SdkError, match="add-plain"):
            c.revoke("pw")
        removed = c.forget()
    assert removed == ["config.json"]
    assert list_profiles(home=tmp_path) == []


# --- 命令列 ----------------------------------------------------------------


def test_cli_add_plain_profiles_set_link_forget(sdk_home, monkeypatch, capsys):
    monkeypatch.setattr(sdk_cli.getpass, "getpass", lambda prompt="": KEY)
    assert client_cli.main(["add-plain", "--api-link", LINK, "--profile", "g"]) == 0
    assert KEY not in capsys.readouterr().out
    assert client_cli.main(["profiles"]) == 0
    out = capsys.readouterr().out
    assert "g\tplain\tready" in out and "gate-0123456789" not in out
    assert client_cli.main(["set-link", "--profile", "g", "--no-check", "https://api.example.test/other"]) == 0
    assert store.load("g").api_link == "https://api.example.test/other"
    assert client_cli.main(["set-link", "--profile", "g", "http://evil.example.test/x"]) == 1
    assert client_cli.main(["forget", "--profile", "g", "--yes"]) == 0
    assert list_profiles() == []
    assert client_cli.main(["pin", "--profile", "missing", "--fingerprint", "x"]) == 1
    assert "找不到設定檔" in capsys.readouterr().err


def test_cli_help_lists_sdk_commands(capsys):
    with pytest.raises(SystemExit):
        client_cli.main(["--help"])
    out = capsys.readouterr().out
    for cmd in sdk_cli.COMMANDS:
        assert cmd in out


def test_import_does_not_load_server_packages():
    code = ("import sys, llmop.sdk, llmop.sdk.cli; "
            "bad = {'fastapi', 'uvicorn', 'aiohttp', 'starlette', 'yaml'} & set(sys.modules); "
            "assert not bad, bad")
    subprocess.run([sys.executable, "-c", code], check=True)
