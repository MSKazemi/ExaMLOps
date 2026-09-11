"""A real-HTTP fake identity provider and policy decision point for the ADR 0120 tests.

Both run on 127.0.0.1 in a thread, so the code under test does genuine discovery, JWKS fetches,
token/device/introspection POSTs and AuthZEN/OPA calls over sockets — no mocking of httpx. The
fakes are deliberately strict (PKCE is actually checked, device codes actually go pending → ok,
refresh tokens actually rotate) so a client that cuts a corner fails here, not at a data center.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs

import jwt
from cryptography.hazmat.primitives.asymmetric import ec, rsa


def _b64(n: int) -> str:
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def rsa_jwk(key: rsa.RSAPrivateKey, kid: str, alg: str = "RS256") -> dict[str, Any]:
    nums = key.public_key().public_numbers()
    return {
        "kty": "RSA",
        "kid": kid,
        "use": "sig",
        "alg": alg,
        "n": _b64(nums.n),
        "e": _b64(nums.e),
    }


def ec_jwk(key: ec.EllipticCurvePrivateKey, kid: str) -> dict[str, Any]:
    nums = key.public_key().public_numbers()
    size = 32
    x = base64.urlsafe_b64encode(nums.x.to_bytes(size, "big")).decode().rstrip("=")
    y = base64.urlsafe_b64encode(nums.y.to_bytes(size, "big")).decode().rstrip("=")
    return {"kty": "EC", "crv": "P-256", "kid": kid, "use": "sig", "alg": "ES256", "x": x, "y": y}


class _Server:
    def __init__(self, handler_factory) -> None:
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_factory)
        self.port = self.httpd.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


class FakeIdP:
    """An OIDC provider: discovery, JWKS (rotatable), token, device, introspection."""

    def __init__(self, *, issuer_suffix: str = "") -> None:
        self.keys: dict[str, rsa.RSAPrivateKey] = {"k1": rsa.generate_private_key(65537, 2048)}
        self.ec_key = ec.generate_private_key(ec.SECP256R1())
        self.published = ["k1"]
        self.jwks_hits = 0
        self.codes: dict[str, dict[str, Any]] = {}
        self.devices: dict[str, dict[str, Any]] = {}
        self.refresh_tokens: dict[str, dict[str, Any]] = {}
        self.opaque: dict[str, dict[str, Any]] = {}
        self.introspect_calls: list[dict[str, str]] = []
        self.token_requests: list[dict[str, str]] = []
        self.clients = {"examlops-dashboard": "dash-secret", "exa-cli": None, "rs": "rs-secret"}
        self.issuer_suffix = issuer_suffix
        self.discovery_issuer_override: str | None = None
        idp = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # noqa: D401 — silence
                pass

            def _json(self, code: int, body: Any) -> None:
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _client(self, form: dict[str, str]) -> str | None:
                auth = self.headers.get("Authorization", "")
                if auth.startswith("Basic "):
                    cid, _, secret = base64.b64decode(auth[6:]).decode().partition(":")
                    return cid if idp.clients.get(cid) == secret else None
                cid = form.get("client_id", "")
                return cid if cid in idp.clients and idp.clients[cid] is None else None

            def do_GET(self):  # noqa: N802
                if self.path == idp.path("/.well-known/openid-configuration"):
                    return self._json(200, idp.discovery())
                if self.path == idp.path("/jwks"):
                    idp.jwks_hits += 1
                    return self._json(200, idp.jwks())
                return self._json(404, {"error": "not_found"})

            def do_POST(self):  # noqa: N802
                n = int(self.headers.get("Content-Length", "0"))
                form = {k: v[0] for k, v in parse_qs(self.rfile.read(n).decode()).items()}
                if self.path == idp.path("/token"):
                    idp.token_requests.append(form)
                    code, body = idp.token(form, self._client(form))
                    return self._json(code, body)
                if self.path == idp.path("/device"):
                    if form.get("client_id") not in idp.clients:
                        return self._json(401, {"error": "invalid_client"})
                    return self._json(200, idp.device(form))
                if self.path == idp.path("/introspect"):
                    idp.introspect_calls.append(form)
                    if self._client(form) is None:
                        return self._json(401, {"error": "invalid_client"})
                    return self._json(200, idp.opaque.get(form.get("token", ""), {"active": False}))
                return self._json(404, {"error": "not_found"})

        self.server = _Server(H)
        self.issuer = self.server.base + issuer_suffix

    # ── helpers ──
    def path(self, p: str) -> str:
        return self.issuer_suffix + p

    def url(self, p: str) -> str:
        return self.issuer + p

    def stop(self) -> None:
        self.server.stop()

    def discovery(self) -> dict[str, Any]:
        return {
            "issuer": self.discovery_issuer_override or self.issuer,
            "jwks_uri": self.url("/jwks"),
            "authorization_endpoint": self.url("/authorize"),
            "token_endpoint": self.url("/token"),
            "device_authorization_endpoint": self.url("/device"),
            "introspection_endpoint": self.url("/introspect"),
            "authorization_response_iss_parameter_supported": True,
        }

    def jwks(self) -> dict[str, Any]:
        keys = [rsa_jwk(self.keys[k], k) for k in self.published]
        keys.append(ec_jwk(self.ec_key, "ec1"))
        return {"keys": keys}

    def rotate(self, kid: str) -> None:
        self.keys[kid] = rsa.generate_private_key(65537, 2048)
        self.published = [kid]

    def mint(
        self,
        claims: dict[str, Any] | None = None,
        *,
        kid: str | None = None,
        alg: str = "RS256",
        headers: dict[str, Any] | None = None,
        **overrides: Any,
    ) -> str:
        now = int(time.time())
        body = {
            "iss": self.issuer,
            "aud": "examlops",
            "sub": "u-123",
            "preferred_username": "alice",
            "iat": now,
            "exp": now + 300,
            **(claims or {}),
            **overrides,
        }
        if alg == "ES256":
            return jwt.encode(
                body, self.ec_key, algorithm="ES256", headers={"kid": "ec1", **(headers or {})}
            )
        key_id = kid or self.published[0]
        return jwt.encode(
            body, self.keys[key_id], algorithm=alg, headers={"kid": key_id, **(headers or {})}
        )

    # ── flows ──
    def issue_code(
        self, *, challenge: str, nonce: str, redirect_uri: str, claims: dict[str, Any]
    ) -> str:
        code = secrets.token_urlsafe(16)
        self.codes[code] = {
            "challenge": challenge,
            "nonce": nonce,
            "redirect_uri": redirect_uri,
            "claims": claims,
        }
        return code

    def _tokens(
        self, client_id: str, claims: dict[str, Any], nonce: str | None = None
    ) -> dict[str, Any]:
        rt = secrets.token_urlsafe(16)
        self.refresh_tokens[rt] = {"client_id": client_id, "claims": claims}
        body: dict[str, Any] = {
            "access_token": self.mint(claims),
            "token_type": "Bearer",
            "expires_in": 300,
            "refresh_token": rt,
            "scope": "openid profile",
        }
        if nonce is not None:
            body["id_token"] = self.mint({**claims, "nonce": nonce}, aud=client_id)
        return body

    def token(self, form: dict[str, str], client: str | None) -> tuple[int, dict[str, Any]]:
        if client is None:
            return 401, {"error": "invalid_client"}
        grant = form.get("grant_type")
        if grant == "authorization_code":
            entry = self.codes.pop(form.get("code", ""), None)
            if entry is None:
                return 400, {"error": "invalid_grant"}
            verifier = form.get("code_verifier", "")
            digest = (
                base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
                .decode()
                .rstrip("=")
            )
            if digest != entry["challenge"] or form.get("redirect_uri") != entry["redirect_uri"]:
                return 400, {
                    "error": "invalid_grant",
                    "error_description": "PKCE or redirect mismatch",
                }
            return 200, self._tokens(client, entry["claims"], entry["nonce"])
        if grant == "urn:ietf:params:oauth:grant-type:device_code":
            dev = self.devices.get(form.get("device_code", ""))
            if dev is None:
                return 400, {"error": "invalid_grant"}
            if dev["state"] == "denied":
                return 400, {"error": "access_denied"}
            if dev["pending"] > 0:
                dev["pending"] -= 1
                return 400, {"error": dev.get("pending_error", "authorization_pending")}
            return 200, self._tokens(client, dev["claims"])
        if grant == "refresh_token":
            entry = self.refresh_tokens.pop(
                form.get("refresh_token", ""), None
            )  # rotation: one use
            if entry is None:
                return 400, {"error": "invalid_grant"}
            return 200, self._tokens(client, entry["claims"])
        return 400, {"error": "unsupported_grant_type"}

    def device(self, form: dict[str, str]) -> dict[str, Any]:
        code = secrets.token_urlsafe(16)
        self.devices[code] = {
            "state": "pending",
            "pending": self.device_pending,
            "claims": self.device_claims,
            "pending_error": self.device_pending_error,
        }
        return {
            "device_code": code,
            "user_code": "ABCD-EFGH",
            "verification_uri": self.url("/activate"),
            "verification_uri_complete": self.url("/activate?user_code=ABCD-EFGH"),
            "expires_in": 600,
            "interval": 1,
        }

    device_pending = 2
    device_pending_error = "authorization_pending"
    device_claims: dict[str, Any] = {"groups": ["examlops-operators"]}


class FakePdp:
    """An AuthZEN / OPA decision point driven by a Python policy function."""

    def __init__(self, policy=None) -> None:
        self.policy = policy or (lambda req: True)
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.fail_with: int | None = None
        self.bearer: str | None = None
        pdp = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):  # noqa: N802
                n = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(n) or b"{}")
                pdp.requests.append((self.path, body))
                pdp.bearer = self.headers.get("Authorization")
                if pdp.fail_with:
                    self.send_response(pdp.fail_with)
                    self.end_headers()
                    return
                if self.path == "/access/v1/evaluation":
                    allowed = bool(pdp.policy(body))
                    out: dict[str, Any] = {"decision": allowed}
                    if not allowed:
                        out["context"] = {"reason_user": {"en": "center policy says no"}}
                elif self.path.startswith("/v1/data/"):
                    verdict = pdp.policy(body["input"])
                    out = {} if verdict is None else {"result": verdict}
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                raw = json.dumps(out).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = _Server(H)
        self.url = self.server.base

    def stop(self) -> None:
        self.server.stop()
