from fastapi import FastAPI
from fastapi.testclient import TestClient
from mcp import types
from pydantic import BaseModel

from mcpo.utils import course_ui


class AddForm(BaseModel):
    a: int
    b: int


def make_app():
    course_ui._trace.clear()
    course_ui.ENABLED = True
    app = FastAPI(title="test-server")

    @app.post("/add", description="Add two numbers.\n\nMore detail.")
    async def add(form: AddForm):
        return {"result": form.a + form.b}

    course_ui.enable(app)
    return app


def test_trace_records_tool_calls():
    client = TestClient(make_app())
    assert client.post("/add", json={"a": 2, "b": 3}).json() == {"result": 5}
    client.post("/add", json={"a": "x"})

    calls = client.get("/_trace.json").json()
    assert [c["status"] for c in calls] == [200, 422]
    assert calls[0]["tool"] == "add"
    assert calls[0]["arguments"] == {"a": 2, "b": 3}
    assert calls[0]["result"] == {"result": 5}

    assert client.get(f"/_trace.json?after={calls[0]['id']}").json()[0]["status"] == 422


def test_trace_skips_internal_routes():
    client = TestClient(make_app())
    client.post("/add", json={"a": 1, "b": 1})
    client.post("/_trace/clear")
    assert client.get("/_trace.json").json() == []


def test_servers_lists_tools_only():
    client = TestClient(make_app())
    servers = client.get("/_servers").json()["servers"]
    assert [s["name"] for s in servers] == ["test-server"]
    assert [(t["name"], t["summary"]) for t in servers[0]["tools"]] == [
        ("add", "Add two numbers.")
    ]


def make_tool(**overrides):
    fields = {
        "name": "add",
        "description": "Add two numbers and return the sum.",
        "input_schema": {
            "type": "object",
            "properties": {
                "a": {"type": "integer", "description": "First addend"},
                "b": {"type": "integer"},
            },
            "required": ["a", "b"],
        },
        **overrides,
    }
    return types.Tool(**fields)


def test_lint_flags_thin_docstrings():
    assert course_ui.lint_tool(make_tool()) == [
        "Parameters without a description: b"
    ]
    warnings = course_ui.lint_tool(make_tool(description=None))
    assert warnings[0].startswith("No description")
    assert course_ui.lint_tool(make_tool(description="Adds."))[0].startswith(
        "Very short description"
    )


def test_labels_only_for_explicit_hints():
    assert course_ui.tool_labels(make_tool()) == []
    tool = make_tool(
        annotations=types.ToolAnnotations(read_only_hint=True, idempotent_hint=True)
    )
    assert course_ui.tool_labels(tool) == ["read-only", "idempotent"]


def test_example_arguments_fill_required_params():
    assert course_ui.example_arguments(make_tool().input_schema) == {"a": 1, "b": 1}


def test_tools_loaded_annotates_openapi_without_touching_description():
    app = make_app()
    tool = make_tool(
        title="Add Numbers",
        annotations=types.ToolAnnotations(read_only_hint=True),
    )
    course_ui.on_tools_loaded(app, [tool])
    client = TestClient(app)

    spec = client.get("/openapi.json").json()
    operation = spec["paths"]["/add"]["post"]
    assert operation["summary"] == "Add Numbers"
    assert operation["description"].startswith("Add two numbers.")
    assert operation["x-mcp-annotations"] == ["read-only"]
    assert operation["x-docstring-check"] == ["Parameters without a description: b"]
    assert not any(path.startswith("/_") for path in spec["paths"])

    raw = client.get("/_mcp/tools").json()["tools"][0]
    assert raw["inputSchema"]["required"] == ["a", "b"]
    assert raw["annotations"] == {"readOnlyHint": True}


def test_swagger_defaults_applied():
    app = make_app()
    assert app.swagger_ui_parameters["tryItOutEnabled"] is True
    assert "tryItOutEnabled" in TestClient(app).get("/docs").text
