"""
The e2e stack's Mailpit (docker-compose.e2e.yml): it catches every email the stack sends and relays nothing.
Drivers use this to count and read what arrived. Inside the stack it is http://mailpit:8025.
"""
import json
import os
import urllib.parse
import urllib.request

API = os.environ.get("E2E_MAILPIT_API", "http://mailpit:8025/api/v1")


def _call(method: str, path: str):
    request = urllib.request.Request(f"{API}{path}", method=method)
    with urllib.request.urlopen(request, timeout=10) as response:      # noqa: S310 - the stack's own Mailpit
        body = response.read()
        is_json = "json" in (response.headers.get("Content-Type") or "")
    return json.loads(body) if body and is_json else None             # DELETE answers "ok" as plain text


def clear() -> None:
    _call("DELETE", "/messages")


def messages(query: str | None = None) -> list[dict]:
    """Every message (newest first), or those matching a Mailpit search such as 'to:"x@e2e.invalid"'."""
    if query:
        return (_call("GET", "/search?" + urllib.parse.urlencode({"query": query, "limit": 500})) or {}).get("messages") or []
    return (_call("GET", "/messages?limit=500") or {}).get("messages") or []


def count(query: str | None = None) -> int:
    return len(messages(query))


def recipients(message: dict) -> list[str]:
    return [t.get("Address") for t in message.get("To") or []]
