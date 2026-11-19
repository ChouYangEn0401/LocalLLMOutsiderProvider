"""紀錄裡不可以出現信封、封裝值、API 金鑰或權杖。"""

from __future__ import annotations

import logging


async def test_no_secrets_in_debug_log(make_gateway, caplog):
    caplog.set_level(logging.DEBUG)
    gw = await make_gateway(sync_wait_seconds=0.05)
    worker = await gw.connect_worker(silent=True)
    api_client = gw.new_api_client()
    envelope, opener = api_client.seal(worker.public)

    status, data = await gw.post(api_client, envelope)
    assert status == 202
    await worker.wait_received(1)
    await worker.answer(worker.received[0])
    status, data = await gw.poll(api_client, envelope.header.request_id)
    assert status == 200
    assert opener.open(data["sealed"])["ok"] is True

    # 也走一遍幾條錯誤路徑。
    assert (await gw.post(api_client, envelope))[0] == 409
    resp = await gw.client.post("/v1/chat/completions", json=envelope.to_json(),
                                headers={"Authorization": "Bearer " + api_client.api_key + "x"})
    assert resp.status == 401
    await worker.close()

    text = caplog.text
    assert envelope.header.request_id in text  # 確認紀錄確實有被收集
    for secret in (
        envelope.sealed, envelope.protected, envelope.sig, data["sealed"],
        api_client.api_key, worker.worker_token,
    ):
        assert secret not in text
