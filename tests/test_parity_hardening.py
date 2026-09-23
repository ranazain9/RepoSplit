"""Tests for Agent 6 (ParityAgent) hardening:
- Query param dispatch in DifferentialRunner for GET/HEAD
- Case-insensitive & expanded field masking in semantic_diff
- Unordered list sorting by entity ID in semantic_diff
- Error envelope normalization (detail vs error/message)
- Port allocation exclusion safety in local_stack
"""

from __future__ import annotations

from unittest.mock import MagicMock

from reposplit.agents.parity.local_stack import free_port
from reposplit.agents.parity.runner import DifferentialRunner, Targets
from reposplit.agents.parity.semantic_diff import DEFAULT_MASKS, diff, normalize
from reposplit.core.schemas import ParityCase


def test_runner_send_get_query_params() -> None:
    targets = Targets(monolith_url="http://127.0.0.1:5000", service_urls={"catalog": "http://127.0.0.1:8001"})
    runner = DifferentialRunner(targets, masks=DEFAULT_MASKS)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = [{"id": 1, "name": "Book"}]
    runner._http.request = MagicMock(return_value=mock_resp)

    # GET case with query parameters
    get_case = ParityCase(
        id="001_get_products",
        service="catalog",
        method="GET",
        path="/products",
        payload={"category": "books", "limit": 10},
    )
    m_status, m_body, _ = runner._send("http://127.0.0.1:5000", get_case)
    assert m_status == 200
    assert m_body == [{"id": 1, "name": "Book"}]

    # Verify httpx was called with params, not json body
    runner._http.request.assert_called_with(
        "GET",
        "http://127.0.0.1:5000/products",
        json=None,
        params={"category": "books", "limit": 10},
        headers={},
    )

    # POST case with json body
    post_case = ParityCase(
        id="002_create_product",
        service="catalog",
        method="POST",
        path="/products",
        payload={"name": "Widget", "price": 9.99},
    )
    runner._send("http://127.0.0.1:5000", post_case)
    runner._http.request.assert_called_with(
        "POST",
        "http://127.0.0.1:5000/products",
        json={"name": "Widget", "price": 9.99},
        params=None,
        headers={},
    )


def test_semantic_diff_expanded_and_camelcase_masks() -> None:
    data = {
        "id": 1,
        "createdAt": "2026-09-23T01:00:00Z",
        "accessToken": "secret-token-12345",
        "jwt": "header.payload.signature",
        "api_key": "live_key_abcdef",
        "etag": 'W/"123456"',
    }
    normalized = normalize(data)
    assert normalized["createdAt"] == "<NORMALIZED_CREATED_AT>"
    assert normalized["accessToken"] == "<NORMALIZED_ACCESS_TOKEN>"
    assert normalized["jwt"] == "<NORMALIZED_JWT>"
    assert normalized["api_key"] == "<NORMALIZED_API_KEY>"
    assert normalized["etag"] == "<NORMALIZED_ETAG>"
    assert normalized["id"] == 1


def test_semantic_diff_unordered_list_sorting() -> None:
    monolith = [
        {"id": 2, "name": "Keyboard", "price": 49.99},
        {"id": 1, "name": "Mouse", "price": 19.99},
        {"id": 3, "name": "Monitor", "price": 199.99},
    ]
    service = [
        {"id": 1, "name": "Mouse", "price": 19.99},
        {"id": 3, "name": "Monitor", "price": 199.99},
        {"id": 2, "name": "Keyboard", "price": 49.99},
    ]
    # Unordered database results should pass semantic diff
    differences = diff(normalize(monolith), normalize(service))
    assert differences == []


def test_semantic_diff_error_envelope_normalization() -> None:
    # Flask monolith returns {"error": "..."}, FastAPI returns {"detail": "..."}
    monolith_err = {"error": "Item not found"}
    fastapi_err = {"detail": "Item not found"}
    differences = diff(normalize(monolith_err), normalize(fastapi_err))
    assert differences == []


def test_local_stack_free_port_collision_safety() -> None:
    allocated: set[int] = set()
    ports = [free_port(allocated) for _ in range(10)]
    assert len(ports) == 10
    assert len(set(ports)) == 10
    assert len(allocated) == 10
