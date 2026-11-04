"""Course UI: a landing page, a live tool-call trace, and friendlier Swagger defaults.

Kept in its own module so the fork rebases cleanly onto upstream mcpo. Everything
here hangs off three hooks: ``enable()`` on the main app, ``apply_swagger_defaults()``
on each sub-app, and ``TraceMiddleware`` wrapping the whole ASGI stack.
"""

import json
import time
from collections import deque
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


def _collect_servers(app: FastAPI, prefix: str = "/") -> List[Dict[str, Any]]:
    """Walk the live route table so hot-reloaded servers show up without a restart."""

    def tools_of(sub_app: FastAPI) -> List[Dict[str, str]]:
        tools = []
        for route in sub_app.routes:
            if (
                isinstance(route, APIRoute)
                and "POST" in route.methods
                and route.include_in_schema
            ):
                description = (route.description or "").strip()
                tools.append(
                    {
                        "name": route.path.lstrip("/"),
                        "summary": description.splitlines()[0] if description else "",
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


_STYLE = """
:root { --bg:#f8fafc; --card:#fff; --ink:#0f172a; --muted:#64748b; --line:#e2e8f0;
        --accent:#4f46e5; --ok:#15803d; --err:#b91c1c; --code:#f1f5f9; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#0b1120; --card:#111827; --ink:#e5e7eb; --muted:#94a3b8; --line:#1f2937;
          --accent:#818cf8; --ok:#4ade80; --err:#f87171; --code:#1e293b; } }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--ink);
       font:15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
main { max-width:960px; margin:0 auto; padding:32px 16px 64px; }
h1 { font-size:26px; margin:0 0 4px; } h2 { font-size:17px; margin:0 0 8px; }
.muted { color:var(--muted); } a { color:var(--accent); }
nav { display:flex; gap:16px; flex-wrap:wrap; margin:12px 0 24px; font-weight:600; }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px;
        padding:16px 18px; margin:0 0 16px; }
code, pre { font:13px/1.45 ui-monospace, SFMono-Regular, Menlo, monospace; }
pre { background:var(--code); padding:10px 12px; border-radius:8px; overflow-x:auto;
      margin:6px 0 0; white-space:pre-wrap; word-break:break-word; }
button { font:inherit; font-size:13px; padding:4px 10px; border-radius:6px; cursor:pointer;
         border:1px solid var(--line); background:var(--card); color:var(--ink); }
footer { margin-top:32px; font-size:13px; color:var(--muted); }
"""

LANDING_HTML = (
    """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MCP Tool Proxy</title><style>"""
    + _STYLE
    + """
ul.tools { list-style:none; padding:0; margin:8px 0 0; }
ul.tools li { padding:6px 0; border-top:1px solid var(--line); }
ul.tools code { font-weight:600; }
.row { display:flex; gap:8px; align-items:center; flex-wrap:wrap; }
</style></head><body><main>
<h1 id="title">MCP Tool Proxy</h1>
<div class="muted">Your MCP server's tools, exposed as a REST API by mcpo.</div>
<nav><a href="docs">Swagger UI</a><a href="_trace">Live Tool Calls</a><a href="openapi.json">OpenAPI Spec</a></nav>

<div class="card">
  <h2>Connect OpenWebUI</h2>
  <div class="muted">Settings → Tools → add this URL as a tool server.</div>
  <div class="row"><pre id="connect" style="flex:1"></pre><button data-copy="connect">Copy</button></div>
</div>

<div id="servers"></div>

<div class="card">
  <h2>Call a Tool From Code</h2>
  <div class="row"><pre id="curl" style="flex:1"></pre><button data-copy="curl">Copy</button></div>
</div>

<footer>Clearseclabs · Agentic RE course build of mcpo</footer>
</main><script>
function el(tag, text, cls) { const e = document.createElement(tag);
  if (text !== undefined) e.textContent = text; if (cls) e.className = cls; return e; }
document.querySelectorAll("button[data-copy]").forEach(b => b.onclick = () => {
  navigator.clipboard.writeText(document.getElementById(b.dataset.copy).textContent);
  b.textContent = "Copied"; setTimeout(() => b.textContent = "Copy", 1200); });
fetch("_servers").then(r => r.json()).then(data => {
  document.getElementById("title").textContent = data.title;
  document.title = data.title;
  const root = document.getElementById("servers");
  const urls = [];
  let example = null;
  for (const s of data.servers) {
    const url = data.base_url + s.path;
    urls.push(url);
    const card = el("div", undefined, "card");
    const head = el("div", undefined, "row");
    head.append(el("h2", s.name), el("span", s.tools.length + " tools", "muted"));
    const docs = el("a", "Open in Swagger →"); docs.href = (s.path ? s.path.replace(/^\\//, "") + "/" : "") + "docs";
    head.append(docs);
    card.append(head);
    const ul = el("ul", undefined, "tools");
    for (const t of s.tools) {
      const li = el("li"); li.append(el("code", t.name));
      if (t.summary) li.append(el("span", " — " + t.summary, "muted"));
      ul.append(li);
      if (!example) example = url + "/" + t.name;
    }
    card.append(ul);
    root.append(card);
  }
  document.getElementById("connect").textContent = urls.join("\\n") || data.base_url;
  document.getElementById("curl").textContent = example
    ? "curl -s -X POST " + example + " \\\\\\n  -H 'Content-Type: application/json' \\\\\\n  -d '{}'"
    : "No tools found yet. Is the MCP server running?";
});
</script></body></html>"""
)

TRACE_HTML = (
    """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Live Tool Calls</title><style>"""
    + _STYLE
    + """
.call { border-left:4px solid var(--ok); }
.call.err { border-left-color:var(--err); }
.head { display:flex; gap:12px; flex-wrap:wrap; align-items:baseline; }
.head .tool { font-weight:700; font-family:ui-monospace, Menlo, monospace; }
.badge { font-size:12px; padding:1px 8px; border-radius:999px; background:var(--code); }
.status { font-weight:700; color:var(--ok); } .err .status { color:var(--err); }
details { margin-top:8px; } summary { cursor:pointer; color:var(--muted); font-size:13px; }
.label { font-size:12px; text-transform:uppercase; letter-spacing:.04em; color:var(--muted); margin-top:8px; }
#empty { text-align:center; padding:40px 0; }
</style></head><body><main>
<h1>Live Tool Calls</h1>
<div class="muted">Every tool call through this proxy, newest first: what the caller sent and what came back.
Point OpenWebUI at this proxy and watch which tools the model picks.</div>
<nav><a href="./">Home</a><a href="docs">Swagger UI</a><span style="flex:1"></span>
<button id="pause">Pause</button><button id="clear">Clear</button></nav>
<div id="calls"><div id="empty" class="muted">No tool calls yet. Try one in Swagger or from OpenWebUI.</div></div>
</main><script>
let last = 0, paused = false;
const calls = document.getElementById("calls");
function el(tag, text, cls) { const e = document.createElement(tag);
  if (text !== undefined) e.textContent = text; if (cls) e.className = cls; return e; }
function pretty(v) { return typeof v === "string" ? v : JSON.stringify(v, null, 2); }
function render(c) {
  const ok = c.status < 400;
  const card = el("div", undefined, "card call" + (ok ? "" : " err"));
  const head = el("div", undefined, "head");
  head.append(
    el("span", new Date(c.time * 1000).toLocaleTimeString(), "muted"),
    el("span", (c.server ? c.server + " / " : "") + c.tool, "tool"),
    el("span", c.client, "badge"),
    el("span", c.status, "status"),
    el("span", c.ms + " ms", "muted"));
  card.append(head);
  card.append(el("div", "Arguments", "label"), el("pre", pretty(c.arguments)));
  const d = el("details"); if (!ok) d.open = true;
  d.append(el("summary", ok ? "Result" : "Error"), el("pre", pretty(c.result)));
  card.append(d);
  return card;
}
async function poll() {
  if (!paused) {
    try {
      const fresh = await (await fetch("_trace.json?after=" + last)).json();
      if (fresh.length) document.getElementById("empty")?.remove();
      for (const c of fresh) { calls.prepend(render(c)); last = Math.max(last, c.id); }
    } catch (e) {}
  }
  setTimeout(poll, 1500);
}
document.getElementById("pause").onclick = e => {
  paused = !paused; e.target.textContent = paused ? "Resume" : "Pause"; };
document.getElementById("clear").onclick = async () => {
  await fetch("_trace/clear", { method: "POST" });
  calls.replaceChildren(el("div", "Cleared.", "muted")); };
poll();
</script></body></html>"""
)
