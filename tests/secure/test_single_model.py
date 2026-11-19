"""R-09（獨立複查）：config 設了 ``model`` 時，加密面只接受那一個模型。

標頭有簽章，閘道不能改寫 model，但看得到、可以拒絕；``/e2e/v1/models`` 也只列那一個。
"""

from __future__ import annotations

from dataclasses import replace


async def test_configured_model_is_enforced_on_the_encrypted_path(gateway):
    gateway.state.config = replace(gateway.state.config, model="m1")
    worker = await gateway.connect_worker(models=["m1", "m2"])
    api_client = gateway.new_api_client()

    envelope, _ = api_client.seal(worker.public, model="m2")
    status, data = await gateway.post(api_client, envelope)
    assert status == 404 and data["error"]["code"] == "model_unavailable", data
    assert "m1" in data["error"]["message"]
    assert gateway.usage_events(api_client.key_id) == []  # refused before admission

    envelope, _ = api_client.seal(worker.public, model="m1")
    status, data = await gateway.post(api_client, envelope)
    assert status == 200 and data["status"] == "done", data

    status, data = await gateway.get("/e2e/v1/models", api_client.headers)
    assert status == 200
    assert {entry["id"] for entry in data["data"]} == {"m1"}


async def test_without_a_configured_model_every_announced_model_is_offered(gateway):
    assert not gateway.state.config.model
    worker = await gateway.connect_worker(models=["m1", "m2"])
    api_client = gateway.new_api_client()
    envelope, _ = api_client.seal(worker.public, model="m2")
    status, data = await gateway.post(api_client, envelope)
    assert status == 200, data
    status, data = await gateway.get("/e2e/v1/models", api_client.headers)
    assert {entry["id"] for entry in data["data"]} == {"m1", "m2"}
