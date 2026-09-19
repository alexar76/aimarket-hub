"""A2A 1.0 discovery facade: conformance-shaped responses and abuse boundaries."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aimarket_hub.api import create_app
from aimarket_hub.config import HubConfig
from aimarket_hub.database import HubDatabase
from aimarket_hub.signing import Signer


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("AIFACTORY_PROD", raising=False)
    monkeypatch.delenv("AIMARKET_SKIP_SEED", raising=False)
    config = HubConfig()
    config.hub_name = "Independent AI Hub"
    config.hub_url = "https://independent.example/hub"
    config.db_path = str(tmp_path / "hub.db")
    config.signing_key_path = str(tmp_path / "hub-key")
    db = HubDatabase(config.db_path)
    app = create_app(config=config, db=db, signer=Signer(config.signing_key_path))
    with TestClient(app) as test_client:
        yield test_client


def _request(*, request_id=1, method="SendMessage", params=None):
    if params is None:
        params = {
            "message": {
                "messageId": "msg-1",
                "role": "ROLE_USER",
                "parts": [{"text": "Find translation tools"}],
            }
        }
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}


def _post(client, payload):
    return client.post("/a2a", json=payload, headers={"A2A-Version": "1.0"})


class TestAgentCard:
    def test_card_declares_only_the_live_jsonrpc_search_skill(self, client):
        response = client.get("/.well-known/agent-card.json")
        assert response.status_code == 200
        card = response.json()
        assert card["supportedInterfaces"] == [{
            "url": "https://independent.example/hub/a2a",
            "protocolBinding": "JSONRPC",
            "protocolVersion": "1.0",
        }]
        assert card["capabilities"] == {
            "streaming": False,
            "pushNotifications": False,
            "extendedAgentCard": False,
        }
        assert [skill["id"] for skill in card["skills"]] == ["marketplace-search"]
        assert response.headers["cache-control"] == "public, max-age=300"
        assert response.headers["etag"].startswith('"')

    def test_card_supports_head_and_conditional_get(self, client):
        first = client.get("/.well-known/agent-card.json")
        head = client.head("/.well-known/agent-card.json")
        cached = client.get(
            "/.well-known/agent-card.json",
            headers={"If-None-Match": first.headers["etag"]},
        )
        assert head.status_code == 200
        assert head.content == b""
        assert cached.status_code == 304
        assert cached.content == b""

    def test_native_manifest_binds_a2a_urls_inside_its_signature(self, client):
        manifest = client.get("/.well-known/ai-market.json").json()
        assert manifest["a2a_endpoint"] == "https://independent.example/hub/a2a"
        assert manifest["a2a_agent_card_url"].endswith("/.well-known/agent-card.json")
        assert manifest["signature"]["algorithm"] == "ed25519"


class TestA2AJsonRpc:
    def test_text_query_returns_a_direct_message_with_structured_offers(self, client):
        response = _post(client, _request())
        assert response.status_code == 200
        assert response.headers["a2a-version"] == "1.0"
        body = response.json()
        assert body["jsonrpc"] == "2.0"
        assert body["id"] == 1
        message = body["result"]["message"]
        assert message["role"] == "ROLE_AGENT"
        assert message["contextId"]
        assert message["parts"][0]["mediaType"] == "text/plain"
        data = message["parts"][1]["data"]
        assert data["query"] == "Find translation tools"
        assert isinstance(data["matches"], list)
        assert data["search"]["query_stays_local"] is True
        assert message["metadata"]["invokeEndpoint"].endswith("/ai-market/v2/invoke")

    def test_structured_query_maps_only_bounded_public_filters(self, client):
        payload = _request(params={
            "message": {
                "messageId": "msg-json",
                "contextId": "ctx-kept",
                "role": "ROLE_USER",
                "parts": [{"data": {
                    "intent": "security audit",
                    "budget": 0.1,
                    "maxLatencyMs": 5000,
                    "minTrust": 0.0,
                    "limit": 2,
                }}],
            }
        })
        body = _post(client, payload).json()
        assert body["result"]["message"]["contextId"] == "ctx-kept"
        data = body["result"]["message"]["parts"][1]["data"]
        assert data["query"] == "security audit"
        assert len(data["matches"]) <= 2

    @pytest.mark.parametrize(
        ("method", "code"),
        [
            ("GetTask", -32001),
            ("CancelTask", -32001),
            ("SendStreamingMessage", -32004),
            ("SubscribeToTask", -32004),
            ("GetExtendedAgentCard", -32004),
            ("CreateTaskPushNotificationConfig", -32003),
            ("UnknownMethod", -32601),
        ],
    )
    def test_unavailable_operations_fail_with_standard_codes(self, client, method, code):
        assert _post(client, _request(method=method, params={})).json()["error"]["code"] == code

    def test_list_tasks_is_honestly_empty_because_send_returns_messages(self, client):
        body = _post(client, _request(method="ListTasks", params={})).json()
        assert body["result"] == {"tasks": []}

    def test_version_is_mandatory_and_downgrade_fails_closed(self, client):
        missing = client.post("/a2a", json=_request())
        old = client.post("/a2a", json=_request(), headers={"A2A-Version": "0.3"})
        assert missing.json()["error"]["code"] == -32009
        assert old.json()["error"]["code"] == -32009

    def test_files_and_push_callbacks_are_not_silently_accepted(self, client):
        file_request = _request(params={
            "message": {
                "messageId": "msg-file",
                "role": "ROLE_USER",
                "parts": [{"url": "https://attacker.example/payload"}],
            }
        })
        assert _post(client, file_request).json()["error"]["code"] == -32005

        push_request = _request()
        push_request["params"]["configuration"] = {
            "taskPushNotificationConfig": {"url": "https://attacker.example/callback"}
        }
        assert _post(client, push_request).json()["error"]["code"] == -32003

    @pytest.mark.parametrize(
        "options",
        [
            {"intent": "x", "limit": 21},
            {"intent": "x", "budget": True},
            {"intent": "x", "minTrust": 1.1},
            {"intent": "x", "includeStale": True},
        ],
    )
    def test_search_options_are_finite_bounded_and_allowlisted(self, client, options):
        payload = _request(params={
            "message": {
                "messageId": "msg-bounds",
                "role": "ROLE_USER",
                "parts": [{"data": options}],
            }
        })
        assert _post(client, payload).json()["error"]["code"] == -32602

    def test_non_finite_json_number_is_rejected_even_if_a_lax_client_sends_it(self, client):
        # RFC 8259 forbids Infinity, while CPython's decoder accepts it by default. Keep
        # the application-level finite check: a hand-written client can still send this.
        raw = b'''{"jsonrpc":"2.0","id":1,"method":"SendMessage","params":{"message":{"messageId":"m","role":"ROLE_USER","parts":[{"data":{"intent":"x","budget":Infinity}}]}}}'''
        response = client.post(
            "/a2a", content=raw,
            headers={"Content-Type": "application/json", "A2A-Version": "1.0"},
        )
        assert response.json()["error"]["code"] == -32602

    def test_body_is_capped_before_json_processing(self, client):
        response = client.post(
            "/a2a",
            content=b"{" + b"x" * (128 * 1024) + b"}",
            headers={"Content-Type": "application/json", "A2A-Version": "1.0"},
        )
        assert response.status_code == 413
        assert response.json()["error"]["data"][0]["reason"] == "REQUEST_TOO_LARGE"
