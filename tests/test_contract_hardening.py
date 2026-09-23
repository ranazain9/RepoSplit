from reposplit.agents.contract.generators import build_openapi, build_proto
from reposplit.core.schemas import EndpointContract, ParamSpec, ServiceContract


def test_build_openapi_synthesizes_typed_response_schema_and_status_codes() -> None:
    ep = EndpointContract(
        service="user_service",
        operation_id="register",
        method="POST",
        path="/api/v1/users",
        exposure="public",
        params=[ParamSpec(name="email", type_hint="str"), ParamSpec(name="full_name", type_hint="str")],
        body_keys=["email", "full_name"],
        response_fields=["id", "email", "full_name"],
        status_codes=[201, 400],
        source_symbol="routes.auth::register",
    )

    contract = ServiceContract(
        service="user_service",
        port=8001,
        base_path="/api/v1/user",
        endpoints=[ep],
    )

    doc = build_openapi(contract, ["X-User-Id", "X-Tenant-Id", "traceparent"])

    # Verify typed response schema in components.schemas
    assert "RegisterResponse" in doc["components"]["schemas"]
    schema = doc["components"]["schemas"]["RegisterResponse"]
    assert schema["type"] == "object"
    assert "id" in schema["properties"] and schema["properties"]["id"]["type"] == "integer"
    assert "email" in schema["properties"] and schema["properties"]["email"]["type"] == "string"
    assert "full_name" in schema["properties"] and schema["properties"]["full_name"]["type"] == "string"

    # Verify operation references the schema
    op = doc["paths"]["/api/v1/users"]["post"]
    assert "201" in op["responses"]
    assert op["responses"]["201"]["content"]["application/json"]["schema"]["$ref"] == "#/components/schemas/RegisterResponse"
    assert "400" in op["responses"]
    assert "4XX" in op["responses"]


def test_build_proto_generates_typed_response_fields() -> None:
    ep = EndpointContract(
        service="catalog_service",
        operation_id="get_product",
        method="GET",
        path="/api/v1/products/{id}",
        exposure="public",
        path_params=["id"],
        params=[ParamSpec(name="id", type_hint="int")],
        response_fields=["id", "sku", "name", "price", "stock"],
        status_codes=[200, 404],
        source_symbol="routes.catalog::get_product",
    )

    contract = ServiceContract(
        service="catalog_service",
        port=8002,
        base_path="/api/v1/catalog",
        endpoints=[ep],
    )

    proto = build_proto(contract)

    assert "service CatalogService" in proto
    assert "rpc GetProduct (GetProductRequest) returns (GetProductResponse);" in proto
    assert "message GetProductResponse {" in proto
    assert "string payload_json = 1;" in proto
    assert "int32 status_code = 2;" in proto
    assert "int64 id = 3;" in proto
    assert "string sku = 4;" in proto
    assert "string name = 5;" in proto
    assert "double price = 6;" in proto
    assert "int64 stock = 7;" in proto
