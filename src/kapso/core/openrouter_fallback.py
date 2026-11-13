"""OpenRouter as Codex's fallback when the subscription is spent (fork patch, see SANOPTIS.md).

Off unless `hosts.codex.openrouter_fallback.enabled`. A Codex session that fails on its
subscription's quota or rate limit runs once more on OpenRouter; any other failure stays a
failure. Codex reaches a custom provider only through a base URL, headers and query
parameters, while OpenRouter's routing is a request-body field, so every request goes through
a loopback relay that sets `provider: {zdr: true, allow_fallbacks: false}` on its body. The
relay holds no credential: Codex sends the key named by `api_key_env` itself.
"""

import http.client
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List
from urllib.parse import urlsplit

from kapso.core.api_endpoint import validate_api_base_url

# Only Zero-Data-Retention endpoints, and no other provider when the chosen one is down
# (https://openrouter.ai/docs/features/provider-routing).
ZDR_ROUTING = {"zdr": True, "allow_fallbacks": False}
PROVIDER_ID = "kapso_openrouter_zdr"
FALLBACK_KEYS = frozenset({"enabled", "base_url", "api_key_env", "model", "timeout_seconds"})
# Codex's own wording for a spent subscription or a rate limit (codex-rs/protocol/src/error.rs:
# UsageLimitReached, QuotaExceeded, RateLimitExceeded, RetryLimit on a 429). POSIX ERE as well,
# so the update crew's shell launcher greps with the same pattern.
QUOTA_PATTERN = r"hit your usage limit|quota exceeded|rate limit exceeded|last status: 429|out of credits"
_QUOTA = re.compile(QUOTA_PATTERN, re.IGNORECASE)
_CHUNK_BYTES = 65536
_relays: Dict[str, ThreadingHTTPServer] = {}
_relays_lock = threading.Lock()


def fallback_settings(block: Any) -> Dict[str, Any]:
    """The validated block when the fallback is on; an empty dict when it is off or absent."""
    if block is None:
        return {}
    if not isinstance(block, dict) or set(block) != FALLBACK_KEYS:
        raise ValueError(f"openrouter_fallback needs exactly the keys {', '.join(sorted(FALLBACK_KEYS))}")
    if not isinstance(block["enabled"], bool):
        raise ValueError("openrouter_fallback.enabled must be true or false")
    if not block["enabled"]:
        return {}
    for key in ("api_key_env", "model"):
        if not isinstance(block[key], str) or not block[key].strip():
            raise ValueError(f"openrouter_fallback.{key} must be a non-empty string")
    if isinstance(block["timeout_seconds"], bool) or not isinstance(block["timeout_seconds"], (int, float)) \
            or block["timeout_seconds"] <= 0:
        raise ValueError("openrouter_fallback.timeout_seconds must be a positive number")
    return {**block, "base_url": validate_api_base_url(block["base_url"])}


def is_quota_error(text: str) -> bool:
    return bool(_QUOTA.search(text or ""))


def zdr_body(raw: bytes) -> bytes:
    """The request body with OpenRouter's routing forced to ZDR-only, no provider fallback."""
    body = json.loads(raw)
    if not isinstance(body, dict):
        raise ValueError("OpenRouter relay: the request body must be a JSON object")
    body["provider"] = dict(ZDR_ROUTING)
    return json.dumps(body).encode()


class _Relay(BaseHTTPRequestHandler):
    upstream = ""
    timeout_seconds = 0.0

    def do_POST(self) -> None:
        if self.headers.get("Content-Encoding") or "Content-Length" not in self.headers:
            self.send_error(400, "OpenRouter relay: needs an uncompressed body with a Content-Length")
            return
        raw = self.rfile.read(int(self.headers["Content-Length"]))
        parts = urlsplit(self.upstream)
        connection_class = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
        connection = connection_class(parts.hostname, parts.port, timeout=self.timeout_seconds)
        headers = {name: self.headers[name] for name in ("Authorization", "Content-Type", "Accept") if self.headers[name]}
        connection.request("POST", parts.path.rstrip("/") + self.path, body=zdr_body(raw), headers=headers)
        response = connection.getresponse()
        self.send_response(response.status)
        if response.getheader("Content-Type"):
            self.send_header("Content-Type", response.getheader("Content-Type"))
        self.end_headers()
        # HTTP/1.0: the body ends when the connection closes, so a stream passes through as it comes.
        chunk = response.read1(_CHUNK_BYTES)
        while chunk:
            self.wfile.write(chunk)
            self.wfile.flush()
            chunk = response.read1(_CHUNK_BYTES)
        connection.close()

    def log_message(self, format: str, *args: Any) -> None:
        pass


def relay_url(settings: Dict[str, Any]) -> str:
    """The loopback relay for ``settings['base_url']``, started once per process."""
    upstream = settings["base_url"]
    with _relays_lock:
        if upstream not in _relays:
            handler = type("OpenRouterRelay", (_Relay,), {
                "upstream": upstream, "timeout_seconds": float(settings["timeout_seconds"]),
            })
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            _relays[upstream] = server
        return f"http://127.0.0.1:{_relays[upstream].server_port}"


def codex_overrides(settings: Dict[str, Any]) -> List[str]:
    """`codex exec` options that send the session to OpenRouter through the relay."""
    prefix = f"model_providers.{PROVIDER_ID}"
    return [
        "-c", f'{prefix}.name="OpenRouter ZDR"',
        "-c", f"{prefix}.base_url={json.dumps(relay_url(settings))}",
        "-c", f"{prefix}.env_key={json.dumps(settings['api_key_env'])}",
        "-c", f'model_provider="{PROVIDER_ID}"',
    ]
