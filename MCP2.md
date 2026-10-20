# mcp 2.x compatibility

`mcpo` 0.0.20 does not run on `mcp` 2.x. It declares `mcp>=1.17.0` with no upper
bound, so a fresh install resolves `mcp` 2.x and then fails at import. This branch
ports it.

Verified against `mcp` 2.3.0 and `fastapi` 0.156.3: the proxy starts, serves its
Swagger UI, and tool calls return correct results through the stdio path
(`mcpo --port N -- python server.py`).

## Changes

### 1. `streamable_http_client` replaces `streamablehttp_client`

`mcp.client.streamable_http.streamablehttp_client` was renamed, and no longer takes
`headers` / `auth` directly. Those move into an `httpx` client built by
`create_mcp_http_client(...)` and passed as `http_client=`.

```python
# mcp 1.x
streamablehttp_client(url=url, headers=headers, auth=auth)

# mcp 2.x
streamable_http_client(
    url=url,
    http_client=create_mcp_http_client(headers=headers, auth=auth),
)
```

### 2. `MCPError` replaces `McpError`

`mcp.shared.exceptions.McpError` is now `MCPError`.

### 3. camelCase model attributes are now snake_case

On `mcp` model objects:

| 1.x | 2.x |
|---|---|
| `tool.inputSchema` | `tool.input_schema` |
| `result.isError` | `result.is_error` |
| `content.mimeType` | `content.mime_type` |

Note this affects the **Python objects only**. The MCP wire format keeps camelCase,
so JSON payloads are unchanged.

### 4. `ssl_certfile` / `ssl_keyfile` removed from the `FastAPI()` call

Unrelated to `mcp`, but it blocks startup on current FastAPI. These are `uvicorn`
options, not FastAPI options; recent FastAPI rejects unknown keyword arguments:

```
TypeError: FastAPI.__init__() got an unexpected keyword argument 'ssl_certfile'
```

They are still passed to `uvicorn.Config(...)`, so TLS behaviour is unchanged.

Because `mcpo` declares `fastapi>=0.115.12` with no upper bound, this failure is
independent of the `mcp` version — 0.0.20 happens to work only with older FastAPI.

## Dependency changes

```toml
"mcp>=2.0.0,<3",
"mcp[cli]>=2.0.0,<3",
"fastapi>=0.115.12,<1",
```

The `mcp` bound is now explicit in both directions so the package cannot silently
resolve across a major again. The `fastapi` upper bound guards the next major.

## Compatibility

This branch targets `mcp` 2.x only. For `mcp` 1.x, use `mcpo` 0.0.20 from PyPI.
