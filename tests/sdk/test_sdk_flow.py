"""整套流程：enroll → 釘選 → 加密聊天 → 明文聊天 → 連結更換（link_changed → set_link）→ 撤銷 → 401。"""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from e2e.harness import ALICE_PASSWORD, DEFAULT_REPLY, OLLAMA_MODEL
from llmop.sdk import Client, SdkError, add_plain, enroll, list_profiles
from llmop.sdk import profiles as store

PROMPT = "SDK 測試的秘密句子 7f3a"


def _text(completion: dict) -> str:
    return completion["choices"][0]["message"]["content"]


def _ask(client: Client, **kw) -> dict:
    return client.chat({"messages": [{"role": "user", "content": PROMPT}]}, **kw)


async def test_full_flow(make_stack):
    stack = await make_stack()
    fingerprint = stack.worker_keys[0]["key_id"]

    # --- enroll（API 連結指定成經過錄音轉送器的那條，才能檢查通道看不到明文） ---
    result = await asyncio.to_thread(
        enroll, stack.ui_url, "alice", ALICE_PASSWORD, worker_fingerprint=fingerprint, profile="home",
        api_link=stack.tunnel_url, key_name="sdk 測試", test=True)
    assert result.status == "ready"
    assert result.transport == "e2e"
    assert result.pinned == (fingerprint,)
    assert result.signing_key_created and result.sig_key_id
    assert result.test_reply == DEFAULT_REPLY
    assert "home" in list_profiles()

    profile = store.load("home")
    assert profile.encryption == "required" and profile.key_id
    assert profile.username == "alice"
    assert ALICE_PASSWORD not in profile.config_path.read_text(encoding="utf-8")  # 密碼不存檔

    # 伺服器上那把 key：必須加密、簽章公鑰＝本機的那把
    portal = await stack.portal.portal()
    rec = next(k for k in portal["keys"] if k["id"] == profile.key_id)
    assert rec["encryption"] == "required" and rec["sig_key_id"] == result.sig_key_id

    client = Client.from_profile("home")
    try:
        # --- 加密聊天：通道（錄音轉送器）上看不到提示與回答 ---
        reply = await asyncio.to_thread(_ask, client, model="any")
        assert _text(reply) == DEFAULT_REPLY
        wire = b"".join(stack.relay.blobs())
        assert PROMPT.encode("utf-8") not in wire
        assert DEFAULT_REPLY.encode("utf-8") not in wire

        models = await asyncio.to_thread(client.models)
        assert [(m.id, m.key_id) for m in models] == [(OLLAMA_MODEL, fingerprint)]

        me = await asyncio.to_thread(client.me)
        assert me["transport"] == "e2e" and me["account"] is None
        assert me["account_error"] == "encryption_required"
        assert stack.gate not in str(me)  # 秘密路徑不外露

        checks = await asyncio.to_thread(client.self_check)
        failed = [c for c in checks if not c[1]]
        assert not failed, failed
        names = [c[0] for c in checks]
        assert "明文被拒（必須加密的 key）" in names and "沒有簽章的請求被拒" in names

        # 指紋打錯：不釘選
        with pytest.raises(SdkError) as bad:
            await asyncio.to_thread(client.pin_worker, "A" * 22)
        assert bad.value.kind_hint == "trust"

        # 強制走明文：伺服器拒絕（這把 key 只能加密）
        with pytest.raises(SdkError) as plain_refused:
            await asyncio.to_thread(_ask, client, transport="plain")
        assert plain_refused.value.code == "encryption_required"

        # --- 明文：服務提供者發的 GUEST key ---
        _record, guest_key = await stack.console.create_guest_key("sdk guest", 50_000)
        await asyncio.to_thread(add_plain, stack.tunnel_url, guest_key, profile="guest")
        guest = Client.from_profile("guest")
        try:
            assert guest.transport == "plain"
            assert _text(await asyncio.to_thread(_ask, guest)) == DEFAULT_REPLY
            guest_me = await asyncio.to_thread(guest.me)
            assert guest_me["account"]["role"] == "guest"

            # --- 連結更換：舊連結 404 → link_changed；set_link 之後恢復 ---
            stack.state.api_link.rotate_path(reason="sdk test", grace_seconds=0)
            new_link = f"{stack.relay.url}/{stack.gate}"
            for c in (guest, client):
                with pytest.raises(SdkError) as changed:
                    await asyncio.to_thread(_ask, c)
                assert changed.value.kind_hint == "link_changed"
                assert "API 連結已更換" in changed.value.message
                assert await asyncio.to_thread(c.set_link, new_link) is True
                assert _text(await asyncio.to_thread(_ask, c)) == DEFAULT_REPLY
            assert store.load("guest").api_link == new_link
        finally:
            guest.close()

        # --- 撤銷：伺服器回 401 ---
        before = client.profile
        await asyncio.to_thread(client.revoke, ALICE_PASSWORD)
        assert client.profile.status == "revoked" and client.profile.api_key == ""
        with pytest.raises(SdkError) as local:
            await asyncio.to_thread(_ask, client)
        assert local.value.kind_hint == "auth"
        old = Client(replace(before, status="ready"))
        try:
            with pytest.raises(SdkError) as revoked:
                await asyncio.to_thread(_ask, old)
            assert revoked.value.status == 401 and revoked.value.kind_hint == "auth"
        finally:
            old.close()

        # --- forget：本機檔案都刪掉 ---
        removed = await asyncio.to_thread(client.forget)
        assert "signing_key.json" in removed and "config.json" in removed
        assert "home" not in list_profiles()
    finally:
        client.close()


async def test_enroll_uses_portal_link_and_stops_on_wrong_fingerprint(make_stack):
    stack = await make_stack()
    with pytest.raises(SdkError) as exc:
        await asyncio.to_thread(enroll, stack.ui_url, "alice", ALICE_PASSWORD,
                                worker_fingerprint="B" * 22, profile="typo")
    assert exc.value.kind_hint == "trust"
    profile = store.load("typo")
    assert profile.status == "incomplete" and not profile.pinned_workers
    assert profile.api_link == f"{stack.api_url}/{stack.gate}"  # 帳號網站顯示的連結

    # 指紋改對之後補釘選，設定檔就完成了
    with Client.from_profile("typo") as client:
        pin = await asyncio.to_thread(client.pin_worker, stack.worker_keys[0]["key_id"])
        assert pin.newly_pinned and OLLAMA_MODEL in pin.models
        assert client.profile.status == "ready" and client.transport == "e2e"
        assert _text(await asyncio.to_thread(_ask, client)) == DEFAULT_REPLY


async def test_enroll_wrong_password_is_auth(make_stack):
    stack = await make_stack()
    with pytest.raises(SdkError) as exc:
        await asyncio.to_thread(enroll, stack.ui_url, "alice", "wrong password", worker_fingerprint="C" * 22,
                                profile="nope")
    assert exc.value.kind_hint == "auth" and exc.value.status == 401
    assert "nope" not in list_profiles()
