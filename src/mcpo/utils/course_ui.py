"""Course UI: a landing page, a live tool-call trace, and friendlier Swagger defaults.

Kept in its own module so the fork rebases cleanly onto upstream mcpo. Everything
here hangs off four hooks: ``enable()`` on the main app, ``apply_swagger_defaults()``
on each sub-app, ``on_tools_loaded()`` after each server's tools are mounted, and
``TraceMiddleware`` wrapping the whole ASGI stack.

Anything added for students stays out of what the model sees. OpenWebUI hands each
operation's ``description`` to the model as the tool description, so labels, token
estimates and docstring warnings go in ``x-`` OpenAPI extensions (Swagger shows them,
OpenWebUI ignores them), and the ``/_mcp`` routes are left out of the schema so they
never turn into tools.
"""

import json
import time
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, List

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.routing import APIRoute
from starlette.routing import Mount

ENABLED = False

# Applied to every FastAPI app mcpo builds. Try-it-out on by default saves a click per
# tool, request duration shows how slow a decompile is, and the filter box matters
# once a server exposes dozens of tools.
SWAGGER_UI_PARAMETERS = {
    "tryItOutEnabled": True,
    "displayRequestDuration": True,
    "filter": True,
    "defaultModelsExpandDepth": -1,
    "docExpansion": "list",
    "showExtensions": True,
}

TRACE_LIMIT = 200
PREVIEW_BYTES = 4096

_trace: Deque[Dict[str, Any]] = deque(maxlen=TRACE_LIMIT)
_next_id = 0


def apply_swagger_defaults(app: FastAPI) -> None:
    if ENABLED:
        app.swagger_ui_parameters = {
            **(app.swagger_ui_parameters or {}),
            **SWAGGER_UI_PARAMETERS,
        }


def _classify_client(user_agent: str) -> str:
    ua = user_agent.lower()
    if "mozilla" in ua:
        return "browser"
    for name in ("python-requests", "httpx", "aiohttp", "curl", "node"):
        if name in ua:
            return name
    return user_agent[:40] or "unknown"


def _preview(raw: bytes) -> Any:
    text = raw[:PREVIEW_BYTES].decode("utf-8", errors="replace")
    truncated = len(raw) > PREVIEW_BYTES
    if not truncated:
        try:
            return json.loads(text)
        except ValueError:
            pass
    return text + ("\n… (truncated)" if truncated else "")


def record_call(entry: Dict[str, Any]) -> None:
    global _next_id
    _next_id += 1
    _trace.append({"id": _next_id, **entry})


class TraceMiddleware:
    """Records every tool call (mcpo serves tools as POST) into an in-memory ring buffer.

    Pure ASGI rather than BaseHTTPMiddleware so the request body still reaches the
    tool handler untouched and streamed responses pass straight through.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if (
            scope["type"] != "http"
            or scope.get("method") != "POST"
            or "/_" in path
        ):
            await self.app(scope, receive, send)
            return

        request_chunks: List[bytes] = []
        response_chunks: List[bytes] = []
        status = {"code": 0}
        started = time.perf_counter()

        async def traced_receive():
            message = await receive()
            if message["type"] == "http.request":
                request_chunks.append(message.get("body", b""))
            return message

        async def traced_send(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            elif message["type"] == "http.response.body":
                if sum(len(c) for c in response_chunks) <= PREVIEW_BYTES:
                    response_chunks.append(message.get("body", b""))
            await send(message)

        try:
            await self.app(scope, traced_receive, traced_send)
        finally:
            headers = {k.decode(): v.decode() for k, v in scope.get("headers", [])}
            parts = [p for p in path.strip("/").split("/") if p]
            record_call(
                {
                    "time": time.time(),
                    "server": "/".join(parts[:-1]),
                    "tool": parts[-1] if parts else "",
                    "client": _classify_client(headers.get("user-agent", "")),
                    "arguments": _preview(b"".join(request_chunks)),
                    "status": status["code"] or 500,
                    "ms": round((time.perf_counter() - started) * 1000, 1),
                    "result": _preview(b"".join(response_chunks)),
                }
            )


def _wire(obj) -> Dict[str, Any]:
    """An mcp model as it travels on the wire: camelCase, no nulls."""
    return obj.model_dump(by_alias=True, exclude_none=True, mode="json")


def estimate_tokens(wire_tool: Dict[str, Any]) -> int:
    # Rough rule of thumb (about 4 characters per token). Close enough to compare tools.
    return max(1, round(len(json.dumps(wire_tool)) / 4))


def lint_tool(tool) -> List[str]:
    """Docstring problems that make it harder for a model to pick and call the tool."""
    warnings = []
    description = (tool.description or "").strip()
    if not description:
        warnings.append("No description: the model only has the tool name to go on.")
    elif len(description) < 25:
        warnings.append("Very short description: say what it returns and when to use it.")
    properties = (tool.input_schema or {}).get("properties", {})
    undocumented = [
        name
        for name, prop in properties.items()
        if not (isinstance(prop, dict) and prop.get("description"))
    ]
    if undocumented:
        warnings.append("Parameters without a description: " + ", ".join(undocumented))
    return warnings


def tool_labels(tool) -> List[str]:
    """Only hints the server set explicitly; MCP leaves unset hints undefined."""
    annotations = tool.annotations
    if not annotations:
        return []
    hints = [
        ("read_only_hint", "read-only"),
        ("destructive_hint", "destructive"),
        ("idempotent_hint", "idempotent"),
        ("open_world_hint", "open-world"),
    ]
    return [label for attr, label in hints if getattr(annotations, attr, None)]


def example_arguments(schema: Dict[str, Any]) -> Dict[str, Any]:
    """Placeholder values for the required parameters, for the curl example."""
    properties = schema.get("properties", {})
    placeholders = {
        "integer": 1,
        "number": 1.5,
        "boolean": True,
        "array": [],
        "object": {},
    }
    example = {}
    for name in schema.get("required", []):
        prop = properties.get(name, {})
        if "default" in prop:
            example[name] = prop["default"]
        elif prop.get("examples"):
            example[name] = prop["examples"][0]
        elif prop.get("enum"):
            example[name] = prop["enum"][0]
        else:
            example[name] = placeholders.get(prop.get("type"), "example")
    return example


async def _session(app: FastAPI):
    manager = getattr(app.state, "session_manager", None)
    if manager:
        session, _ = await manager.ensure_initialized()
        return session
    return app.state.session


def on_tools_loaded(app: FastAPI, tools) -> None:
    """Annotate the tool routes mcpo just built, and add read-only views of the server."""
    if not ENABLED:
        return

    info = {}
    for tool in tools:
        wire = _wire(tool)
        info[tool.name] = {
            "title": tool.title
            or getattr(tool.annotations, "title", None)
            or "",
            "labels": tool_labels(tool),
            "tokens": estimate_tokens(wire),
            "warnings": lint_tool(tool),
            "example": example_arguments(tool.input_schema or {}),
            "wire": wire,
        }
    app.state.course_tools = info

    for route in app.routes:
        entry = isinstance(route, APIRoute) and info.get(route.path.lstrip("/"))
        if not entry:
            continue
        if entry["title"]:
            route.summary = entry["title"]
        route.openapi_extra = {
            **(route.openapi_extra or {}),
            "x-mcp-annotations": entry["labels"] or ["none set"],
            "x-mcp-estimated-tokens": entry["tokens"],
            "x-docstring-check": entry["warnings"] or ["ok"],
        }
    app.openapi_schema = None
    app.description = (
        f"{app.description or ''}\n\n[Course home](/) · [Live tool calls](/_trace)"
    )

    if getattr(app.state, "course_routes", False):
        return
    app.state.course_routes = True

    @app.get("/_mcp/tools", include_in_schema=False)
    async def raw_tools():
        return JSONResponse(
            {"tools": [entry["wire"] for entry in app.state.course_tools.values()]}
        )

    @app.get("/_mcp/resources", include_in_schema=False)
    async def resources():
        payload: Dict[str, Any] = {"resources": [], "templates": []}
        try:
            session = await _session(app)
            payload["resources"] = [
                _wire(r) for r in (await session.list_resources()).resources
            ]
            payload["templates"] = [
                _wire(t)
                for t in (await session.list_resource_templates()).resource_templates
            ]
        except Exception as e:
            payload["note"] = f"{type(e).__name__}: {e}"
        return JSONResponse(payload)

    @app.get("/_mcp/resources/read", include_in_schema=False)
    async def read_resource(uri: str):
        try:
            session = await _session(app)
            return JSONResponse(_wire(await session.read_resource(uri)))
        except Exception as e:
            return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=502)

    @app.get("/_mcp/prompts", include_in_schema=False)
    async def prompts():
        try:
            session = await _session(app)
            return JSONResponse(
                {"prompts": [_wire(p) for p in (await session.list_prompts()).prompts]}
            )
        except Exception as e:
            return JSONResponse({"prompts": [], "note": f"{type(e).__name__}: {e}"})

    @app.post("/_mcp/prompts/{name}", include_in_schema=False)
    async def get_prompt(name: str, request: Request):
        body = await request.body()
        arguments = json.loads(body) if body else {}
        try:
            session = await _session(app)
            return JSONResponse(_wire(await session.get_prompt(name, arguments)))
        except Exception as e:
            return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=502)


def _collect_servers(app: FastAPI, prefix: str = "/") -> List[Dict[str, Any]]:
    """Walk the live route table so hot-reloaded servers show up without a restart."""

    def tools_of(sub_app: FastAPI) -> List[Dict[str, Any]]:
        details = getattr(sub_app.state, "course_tools", {})
        tools = []
        for route in sub_app.routes:
            if (
                isinstance(route, APIRoute)
                and "POST" in route.methods
                and route.include_in_schema
            ):
                name = route.path.lstrip("/")
                description = (route.description or "").strip()
                entry = details.get(name, {})
                tools.append(
                    {
                        "name": name,
                        "summary": description.splitlines()[0] if description else "",
                        "title": entry.get("title", ""),
                        "labels": entry.get("labels", []),
                        "tokens": entry.get("tokens"),
                        "warnings": entry.get("warnings", []),
                        "example": entry.get("example", {}),
                    }
                )
        return tools

    servers = []
    own_tools = tools_of(app)
    if own_tools:
        servers.append(
            {"name": app.title, "path": prefix.rstrip("/"), "tools": own_tools}
        )
    for route in app.routes:
        if isinstance(route, Mount) and isinstance(route.app, FastAPI):
            servers.append(
                {
                    "name": route.app.title,
                    "path": route.path,
                    "tools": tools_of(route.app),
                }
            )
    return servers


def enable(app: FastAPI, path_prefix: str = "/") -> None:
    """Register the landing page and trace routes, and wrap the app with tracing."""
    apply_swagger_defaults(app)
    app.add_middleware(TraceMiddleware)

    @app.get("/", include_in_schema=False)
    async def landing(request: Request):
        return HTMLResponse(LANDING_HTML)

    @app.get("/_servers", include_in_schema=False)
    async def servers(request: Request):
        return JSONResponse(
            {
                "base_url": str(request.base_url).rstrip("/"),
                "title": app.title,
                "servers": _collect_servers(app, path_prefix),
            }
        )

    @app.get("/_trace", include_in_schema=False)
    async def trace_page():
        return HTMLResponse(TRACE_HTML)

    @app.get("/_trace.json", include_in_schema=False)
    async def trace_json(after: int = 0):
        return JSONResponse([e for e in _trace if e["id"] > after])

    @app.post("/_trace/clear", include_in_schema=False)
    async def trace_clear():
        _trace.clear()
        return JSONResponse({"cleared": True})


_HERE = Path(__file__).parent
_STYLE = (_HERE / "course_ui.css").read_text()
LANDING_HTML = (_HERE / "course_ui_landing.html").read_text().replace("@@STYLE@@", _STYLE)
TRACE_HTML = (_HERE / "course_ui_trace.html").read_text().replace("@@STYLE@@", _STYLE)
