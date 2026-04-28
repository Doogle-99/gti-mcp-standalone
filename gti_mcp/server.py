# Copyright 2025 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# Add lifespan support for startup/shutdown with strong typing
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from dataclasses import dataclass

import logging
import os
from contextvars import ContextVar
import vt
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response
from starlette.routing import Route, Mount
from starlette.requests import Request
from mcp.server.sse import SseServerTransport
from mcp.server.fastmcp import FastMCP, Context

logging.basicConfig(level=logging.ERROR)

# If True, creates a completely fresh transport for each request
# with no session tracking or state persistence between requests.
stateless = False
if os.getenv("STATELESS") == "1":
  stateless = True

# Context variable to store VT API key from request headers
vt_api_key_ctx: ContextVar[str | None] = ContextVar("vt_api_key", default=None)


def _vt_client_factory(ctx: Context, api_key: str = None) -> vt.Client:
    # Prioritize the passed argument
    if not api_key:
        api_key = vt_api_key_ctx.get()

    if not api_key:
        api_key = os.getenv("VT_APIKEY")
    
    # Try to get from context if not in env (placeholder for future ctx inspection)
    # if not api_key and ctx and hasattr(ctx, 'init_options'):
    #     api_key = ctx.init_options.get('vtApiKey')
    
    if not api_key:
        raise ValueError("VT API Key is required. Please provide it as an argument 'api_key' or set VT_APIKEY environment variable.")
    return vt.Client(api_key)

vt_client_factory = _vt_client_factory


@asynccontextmanager
async def vt_client(ctx: Context, api_key: str = None) -> AsyncIterator[vt.Client]:
  """Provides a vt.Client instance for the current request."""
  client = vt_client_factory(ctx, api_key)

  try:
    yield client
  finally:
    await client.close_async()

# Create a named server and specify dependencies for deployment and development
server = FastMCP(
    "Google Threat Intelligence MCP server",
    dependencies=["vt-py"],
    stateless_http=stateless)

# Load tools.
from gti_mcp.tools import *

# --- SSE and Auth Implementation ---

class BearerTokenAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # Allow OPTIONS for CORS (if needed) or health checks
        if request.method == "OPTIONS":
            return await call_next(request)
            
        # Allow unauthenticated access to the toolspec endpoint
        if request.url.path == "/toolspec":
            return await call_next(request)

        # Allow unauthenticated access to the tools/list and resources/list methods for MCP registry
        if request.method == "POST":
            try:
                body = await request.json()
                if body.get("method") in ("tools/list", "resources/list"):
                    return await call_next(request)
            except Exception:
                pass

        auth_token = os.getenv("MCP_AUTH_TOKEN")
        if not auth_token:
            # If no token configured, fail safe or allow? 
            # User requirement: "use a bearer token... hardcoded to this MCP server"
            # Assuming if env var is missing, we block everything to be safe.
            return JSONResponse({"error": "Server misconfigured: MCP_AUTH_TOKEN missing"}, status_code=500)

        # Support X-Mcp-Authorization to allow standard Authorization header to be used for Cloud Run IAM
        # or other upstream proxies.
        auth_header = request.headers.get("X-Mcp-Authorization")
        if not auth_header:
            auth_header = request.headers.get("Authorization")
        
        if not auth_header:
             return JSONResponse({"error": "Missing Authorization or X-Mcp-Authorization header"}, status_code=401)

        token = auth_header
        if auth_header.startswith("Bearer "):
             token = auth_header.split(" ")[1]
             
        if token != auth_token:
            return JSONResponse({"error": "Invalid token"}, status_code=403)

        # Extract VT API key if present in headers
        vt_api_key = request.headers.get("X-VT-ApiKey")
        if vt_api_key:
            vt_api_key_ctx.set(vt_api_key)

        return await call_next(request)

sse = SseServerTransport("/messages")

class ASGIResponse(Response):
    def __init__(self, app, **kwargs):
        super().__init__(**kwargs)
        self.app = app

    async def __call__(self, scope, receive, send):
        await self.app(scope, receive, send)

async def handle_sse(request: Request):
    # Access the underlying Server object from FastMCP
    mcp_server = getattr(server, "_mcp_server", None)
    if not mcp_server:
         raise RuntimeError("Could not find underlying MCP Server in FastMCP instance")

    async def asgi_handler(scope, receive, send):
        async with sse.connect_sse(scope, receive, send) as streams:
            await mcp_server.run(
                streams[0], streams[1], mcp_server.create_initialization_options()
            )
    
    return ASGIResponse(asgi_handler)

async def handle_toolspec(request: Request):
    mcp_tools = await server.list_tools()
    toolspec = []
    for tool in mcp_tools:
        toolspec.append({
            "name": tool.name,
            "description": tool.description,
            "inputSchema": tool.inputSchema
        })
    return JSONResponse(toolspec)

async def handle_mcp(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": -32700, "message": "Parse error"}
        }, status_code=400, media_type="application/json")

    if "jsonrpc" not in body or body["jsonrpc"] != "2.0" or "method" not in body:
        return JSONResponse({
            "jsonrpc": "2.0",
            "id": body.get("id"),
            "error": {"code": -32600, "message": "Invalid Request"}
        }, status_code=400, media_type="application/json")

    method = body["method"]
    rpc_id = body.get("id")
    params = body.get("params", {})

    logging.info(f"MCP request initialised for method: {method}")

    if method == "tools/list":
        mcp_tools = await server.list_tools()
        tools_list = []
        for tool in mcp_tools:
            tools_list.append({
                "name": tool.name,
                "description": tool.description,
                "inputSchema": tool.inputSchema
            })
        return JSONResponse({
            "jsonrpc": "2.0",
            "id": rpc_id,
            "result": {"tools": tools_list}
        }, media_type="application/json")

    elif method == "resources/list":
        return JSONResponse({
            "jsonrpc": "2.0",
            "id": rpc_id,
            "result": {"resources": []}
        }, media_type="application/json")

    elif method == "tools/call":
        tool_name = params.get("name")
        tool_args = params.get("arguments", {})

        if not tool_name:
            return JSONResponse({
                "jsonrpc": "2.0",
                "id": rpc_id,
                "error": {"code": -32602, "message": "Invalid params: 'name' is required"}
            }, status_code=400, media_type="application/json")

        try:
            api_key = tool_args.get("api_key")
            if not api_key:
                api_key = request.headers.get("X-VT-ApiKey")

            if api_key:
                vt_api_key_ctx.set(api_key)

            result = await server.call_tool(tool_name, arguments=tool_args)
            
            if hasattr(result, "content"):
                content_list = []
                for c in result.content:
                    if hasattr(c, "text"):
                        content_list.append({"type": "text", "text": c.text})
                    elif hasattr(c, "data"):
                        content_list.append({"type": "image", "data": c.data, "mimeType": c.mimeType})
                call_result = {"content": content_list}
            else:
                call_result = {"content": [{"type": "text", "text": str(result)}]}

            return JSONResponse({
                "jsonrpc": "2.0",
                "id": rpc_id,
                "result": call_result
            }, media_type="application/json")

        except Exception as e:
            return JSONResponse({
                "jsonrpc": "2.0",
                "id": rpc_id,
                "error": {"code": -32603, "message": f"Internal error: {str(e)}"}
            }, status_code=500, media_type="application/json")

    else:
        return JSONResponse({
            "jsonrpc": "2.0",
            "id": rpc_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"}
        }, status_code=404, media_type="application/json")

async def handle_messages(request: Request):
    return ASGIResponse(sse.handle_post_message)

# Create Starlette App
middleware = [
    Middleware(BearerTokenAuthMiddleware)
]

routes = [
    Route("/sse", handle_sse),
    Route("/messages", handle_messages, methods=["POST"]),
    Route("/toolspec", handle_toolspec, methods=["GET"]),
    Route("/", handle_mcp, methods=["POST"]),
    Route("/mcp", handle_mcp, methods=["POST"])
]

app = Starlette(debug=True, routes=routes, middleware=middleware)

# Run the server (Local stdio support kept for back-compat/debugging)
def main():
  server.run(transport='stdio')


if __name__ == '__main__':
  main()
