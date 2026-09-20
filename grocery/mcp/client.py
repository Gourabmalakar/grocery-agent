"""
A small Model Context Protocol client, spoken directly over HTTP.

WHY NOT THE OFFICIAL SDK. `pip install mcp` resolves to pydantic 2.13.5 and
starlette 1.6.0; this repo pins pydantic==2.10.3 alongside fastapi==0.115.6
(which pins starlette 0.41.3). Taking the SDK means taking that upgrade
across the process that holds the money, to gain three JSON-RPC methods we
can write in a page. Checked with `pip install --dry-run --report`: 17 new
packages. So this speaks the protocol directly on `requests`, which is
already a dependency, and adds zero pins.

That trade would be wrong if we needed the protocol's breadth. We need
exactly: initialize, notifications/initialized, tools/list, tools/call.

TRANSPORT. Streamable HTTP (MCP 2025-06-18): every request is a JSON-RPC
POST, and the server answers with EITHER application/json OR an SSE stream
carrying the same JSON-RPC envelope in a `data:` line. Both shapes are
handled in _post_rpc; a server that switches between them (as load or
payload size changes) must not break us. The Mcp-Session-Id header returned
by initialize has to be echoed on every later call or the server treats each
request as a new, uninitialised session.

THIS CLIENT NEVER DECIDES WHAT MAY BE CALLED. Tool allowlisting is the
caller's job and lives with the caller's security context — see
grocery/adapters/zepto_mcp.py. A transport that also enforced policy would
put the policy somewhere a future transport swap could quietly drop it.
"""
from __future__ import annotations

import json
from typing import Any, Callable

import requests

PROTOCOL_VERSION = "2025-06-18"
_TIMEOUT_SECONDS = 45


class McpError(RuntimeError):
    """A JSON-RPC error, or a transport-level failure talking to the server."""


class McpAuthRequired(McpError):
    """HTTP 401. The server wants a bearer token we don't have (or ours has
    expired). Callers catch this to trigger a refresh — never to prompt for
    a credential themselves."""

    def __init__(self, message: str, *, www_authenticate: str = "") -> None:
        super().__init__(message)
        self.www_authenticate = www_authenticate


class McpClient:
    def __init__(
        self,
        endpoint: str,
        *,
        client_name: str = "grocery-agent",
        client_version: str = "0.1",
        token_provider: Callable[[], str | None] | None = None,
    ) -> None:
        self.endpoint = endpoint
        self._client_name = client_name
        self._client_version = client_version
        # A callable, not a string: the token can be refreshed between calls
        # and we must not cache a stale one for the life of the client.
        self._token_provider = token_provider or (lambda: None)
        self._session_id: str | None = None
        self._next_id = 0
        self._server_info: dict[str, Any] = {}

    # ---- transport -----------------------------------------------------

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
        }
        token = self._token_provider()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        return headers

    @staticmethod
    def _parse_body(response: requests.Response) -> dict[str, Any] | None:
        """Accepts either a plain JSON body or an SSE stream, because the
        spec lets the server choose per response."""
        content_type = response.headers.get("Content-Type", "")
        text = response.text
        if "text/event-stream" in content_type:
            for line in text.splitlines():
                if line.startswith("data:"):
                    payload = line[len("data:"):].strip()
                    if not payload:
                        continue
                    try:
                        message = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    # Skip server-initiated notifications; we want the reply.
                    if "id" in message:
                        return message
            return None
        if not text.strip():
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise McpError(f"server sent a non-JSON body: {text[:200]!r}") from exc

    def _post_rpc(self, method: str, params: dict[str, Any] | None, *, notification: bool = False) -> Any:
        body: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            body["params"] = params
        if not notification:
            self._next_id += 1
            body["id"] = self._next_id

        try:
            response = requests.post(
                self.endpoint, headers=self._headers(), json=body, timeout=_TIMEOUT_SECONDS
            )
        except requests.RequestException as exc:
            raise McpError(f"could not reach {self.endpoint}: {exc}") from exc

        if response.status_code == 401:
            raise McpAuthRequired(
                f"{self.endpoint} requires authorization",
                www_authenticate=response.headers.get("WWW-Authenticate", ""),
            )

        session_id = response.headers.get("Mcp-Session-Id")
        if session_id:
            self._session_id = session_id

        if notification:
            return None

        if response.status_code >= 400:
            raise McpError(f"HTTP {response.status_code} from {self.endpoint}: {response.text[:300]}")

        message = self._parse_body(response)
        if message is None:
            raise McpError(f"empty response to {method}")
        if "error" in message:
            err = message["error"]
            raise McpError(f"{method} failed: [{err.get('code')}] {err.get('message')}")
        return message.get("result")

    # ---- protocol ------------------------------------------------------

    def connect(self) -> dict[str, Any]:
        result = self._post_rpc(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": self._client_name, "version": self._client_version},
            },
        )
        self._server_info = result or {}
        # Required by the spec: the server may reject real calls until it
        # has seen this, and it is a notification, so there is no reply.
        self._post_rpc("notifications/initialized", None, notification=True)
        return self._server_info

    def list_tools(self) -> list[dict[str, Any]]:
        """Every tool the server is willing to expose to THIS token. Worth
        recording verbatim: it is both an audit fact (what the surface was
        on the day we called it) and, when the grant is read-scoped, the
        evidence that the write tools are genuinely not on offer."""
        result = self._post_rpc("tools/list", {}) or {}
        return list(result.get("tools", []))

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = self._post_rpc("tools/call", {"name": name, "arguments": arguments}) or {}
        if result.get("isError"):
            raise McpError(f"tool {name} reported an error: {_text_of(result)[:300]}")
        return result


def _text_of(tool_result: dict[str, Any]) -> str:
    return "\n".join(
        block.get("text", "")
        for block in tool_result.get("content", [])
        if block.get("type") == "text"
    )


def payload_of(tool_result: dict[str, Any]) -> Any:
    """The tool's data, preferring structuredContent when the server sends
    it and falling back to parsing the text block as JSON.

    NB: the text block is a *transport* for the payload, not prose to be
    interpreted. Whatever comes back is merchant-controlled data (CLAUDE.md
    rule 2) — it is parsed into fields and never read as instructions.
    """
    if "structuredContent" in tool_result:
        return tool_result["structuredContent"]
    text = _text_of(tool_result)
    if not text.strip():
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text
