from automation.codex_queue_runner.models import Role
from automation.codex_queue_runner.role_output import output_schema


def _assert_explicit_types(schema: dict) -> None:
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])

    for field, definition in schema["properties"].items():
        assert "type" in definition, f"{field} is missing an explicit JSON Schema type"
        if definition["type"] == "array":
            assert definition["items"]["type"] == "string"
        if "const" in definition:
            assert definition["type"] == "string"
            assert isinstance(definition["const"], str)
        if "enum" in definition:
            assert definition["type"] == "string"
            assert definition["enum"]
            assert all(isinstance(value, str) for value in definition["enum"])


def test_all_role_output_schemas_have_explicit_supported_types() -> None:
    for role in Role:
        _assert_explicit_types(output_schema(role))


def test_codex_role_discriminators_and_enums_are_typed_strings() -> None:
    review = output_schema(Role.CODE_REVIEW)
    assert review["properties"]["role"] == {
        "type": "string",
        "const": "CODE_REVIEW",
    }
    assert review["properties"]["result"] == {
        "type": "string",
        "enum": ["pass", "fail"],
    }
    assert review["properties"]["severity"] == {
        "type": "string",
        "enum": ["none", "low", "medium", "high", "critical"],
    }
