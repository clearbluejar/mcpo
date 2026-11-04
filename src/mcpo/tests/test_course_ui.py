from fastapi import FastAPI
from fastapi.testclient import TestClient
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
    assert servers == [
        {
            "name": "test-server",
            "path": "",
            "tools": [{"name": "add", "summary": "Add two numbers."}],
        }
    ]


def test_swagger_defaults_applied():
    app = make_app()
    assert app.swagger_ui_parameters["tryItOutEnabled"] is True
    assert "tryItOutEnabled" in TestClient(app).get("/docs").text
