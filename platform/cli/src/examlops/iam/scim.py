"""SCIM 2.0 provisioning (RFC 7643 schema, RFC 7644 protocol) over the account directory (ADR 0132).

A center's IdP (Entra ID, Okta, Keycloak with a SCIM extension, midPoint, …) pushes the lifecycle of
the users it assigns to ExaMLOps: create on assignment, update on change, ``active: false`` or DELETE
on removal. This module is framework-free — it turns SCIM requests into
:mod:`examlops.iam.directory` operations and returns ``(status, body)`` — so any service can mount
it; the dashboard does, at ``/api/scim/v2``.

**Authentication and scope.** Each provider's trust entry names its own SCIM bearer
(``provisioning.token_ref``). The presented bearer selects the provider (constant-time compare
against every configured token) and every operation is confined to that provider's accounts: one
center can never read, create or deactivate another center's users.

**Supported surface** (what Entra ID and Okta send for user provisioning):
``/ServiceProviderConfig``, ``/ResourceTypes``, ``/Schemas``; ``/Users`` list with ``filter``
(``eq`` on ``userName``, ``externalId``, ``id``, ``emails[.value]``) and ``startIndex``/``count``
paging; ``POST``, ``GET``, ``PUT``, ``PATCH`` (``add``/``replace``/``remove``, with or without
``path``, case-insensitive ops, string booleans) and ``DELETE``. Groups are not provisioned here —
roles keep coming from the group claims in the center's tokens, so there is one source of truth for
authorization.
"""

from __future__ import annotations

import hmac
import re
from typing import Any

from examlops.iam import directory
from examlops.iam.config import IamConfig, ProviderConfig, load_config, resolve_secret_ref

USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
ENTERPRISE_SCHEMA = "urn:ietf:params:scim:schemas:extension:enterprise:2.0:User"
LIST_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
PATCH_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"
ERROR_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:Error"
CONTENT_TYPE = "application/scim+json"
MAX_PAGE = 200


class ScimError(Exception):
    """An RFC 7644 §3.12 error response."""

    def __init__(self, status: int, detail: str, scim_type: str | None = None) -> None:
        super().__init__(detail)
        self.status, self.detail, self.scim_type = status, detail, scim_type

    def body(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "schemas": [ERROR_SCHEMA],
            "status": str(self.status),
            "detail": self.detail,
        }
        if self.scim_type:
            out["scimType"] = self.scim_type
        return out


# ── authentication ───────────────────────────────────────────────────────────


def authenticate(authorization: str | None, config: IamConfig | None = None) -> ProviderConfig:
    """The provider whose SCIM bearer was presented, or :class:`ScimError` 401."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise ScimError(401, "missing bearer token")
    presented = authorization.split(None, 1)[1].strip()
    cfg = config if config is not None else load_config()
    matched: ProviderConfig | None = None
    for p in cfg.providers:  # compare against every provider: no early-exit timing signal
        expected = (
            resolve_secret_ref(p.provisioning.token_ref) if p.provisioning.token_ref else None
        )
        if expected and hmac.compare_digest(presented.encode(), expected.encode()):
            matched = p
    if matched is None:
        raise ScimError(401, "invalid bearer token")
    return matched


# ── resource mapping ─────────────────────────────────────────────────────────


def _bool(value: Any) -> bool:
    """SCIM booleans, including Entra ID's ``"False"`` / ``"True"`` strings."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
        return value.strip().lower() == "true"
    raise ScimError(400, f"expected a boolean, got {value!r}", "invalidValue")


def _primary_email(emails: Any) -> str | None:
    if not isinstance(emails, list) or not emails:
        return None
    ranked = sorted(
        (e for e in emails if isinstance(e, dict) and e.get("value")),
        key=lambda e: (
            not _bool(e.get("primary", False)) if "primary" in e else True,
            e.get("type") != "work",
        ),
    )
    return str(ranked[0]["value"]) if ranked else None


def to_resource(acc: directory.Account, base_url: str) -> dict[str, Any]:
    attrs = acc.attributes or {}
    res: dict[str, Any] = {
        "schemas": [USER_SCHEMA] + ([ENTERPRISE_SCHEMA] if ENTERPRISE_SCHEMA in attrs else []),
        "id": acc.id,
        "userName": acc.username,
        "active": acc.active,
        "meta": {
            "resourceType": "User",
            "created": acc.created_at,
            "lastModified": acc.updated_at,
            "location": f"{base_url.rstrip('/')}/Users/{acc.id}",
            "version": f'W/"{acc.version}"',
        },
    }
    if acc.external_id:
        res["externalId"] = acc.external_id
    if acc.display_name:
        res["displayName"] = acc.display_name
    if "name" in attrs:
        res["name"] = attrs["name"]
    emails = attrs.get("emails") or ([{"value": acc.email, "primary": True}] if acc.email else None)
    if emails:
        res["emails"] = emails
    if ENTERPRISE_SCHEMA in attrs:
        res[ENTERPRISE_SCHEMA] = attrs[ENTERPRISE_SCHEMA]
    return res


def _fields_from_resource(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ScimError(400, "body must be a SCIM User object", "invalidSyntax")
    user_name = payload.get("userName")
    if not isinstance(user_name, str) or not user_name.strip():
        raise ScimError(400, "userName is required", "invalidValue")
    attrs: dict[str, Any] = {}
    for key in ("name", "emails", "title", "preferredLanguage", "locale", "timezone"):
        if key in payload:
            attrs[key] = payload[key]
    if ENTERPRISE_SCHEMA in payload:
        attrs[ENTERPRISE_SCHEMA] = payload[ENTERPRISE_SCHEMA]
    return {
        "username": user_name.strip(),
        "external_id": payload.get("externalId") or None,
        "display_name": payload.get("displayName") or (payload.get("name") or {}).get("formatted"),
        "email": _primary_email(payload.get("emails")),
        "active": _bool(payload.get("active", True)),
        "attributes": attrs,
    }


# ── filters (RFC 7644 §3.4.2.2 — the `eq` subset IdPs use for provisioning) ───

_FILTER_RE = re.compile(r'^\s*([A-Za-z.:\[\]"\s]+?)\s+eq\s+"((?:[^"\\]|\\.)*)"\s*$', re.IGNORECASE)
_FILTER_COLUMNS = {
    "username": "username",
    "externalid": "external_id",
    "id": "id",
    "emails": "email",
    "emails.value": "email",
    'emails[type eq "work"].value': "email",
}


def parse_filter(expr: str | None) -> dict[str, str]:
    if not expr:
        return {}
    m = _FILTER_RE.match(expr)
    column = _FILTER_COLUMNS.get(m.group(1).strip().lower()) if m else None
    if not m or column is None:
        raise ScimError(
            400,
            'only `<attr> eq "value"` on userName, externalId, id or emails is supported',
            "invalidFilter",
        )
    return {column: m.group(2).replace('\\"', '"')}


# ── PATCH (RFC 7644 §3.5.2) ───────────────────────────────────────────────────


def _apply_path(
    changes: dict[str, Any], attrs: dict[str, Any], path: str, value: Any, op: str
) -> None:
    p = path.strip()
    low = p.lower()
    if low == "active":
        changes["active"] = False if op == "remove" else _bool(value)
    elif low == "username":
        if op == "remove" or not isinstance(value, str) or not value.strip():
            raise ScimError(400, "userName cannot be removed", "mutability")
        changes["username"] = value.strip()
    elif low == "externalid":
        changes["external_id"] = None if op == "remove" else (str(value) if value else None)
    elif low == "displayname":
        changes["display_name"] = None if op == "remove" else (str(value) if value else None)
    elif low.startswith("name"):
        name = dict(attrs.get("name") or {})
        if low == "name":
            name = {} if op == "remove" else dict(value or {})
        else:
            sub = p.split(".", 1)[1]
            if op == "remove":
                name.pop(sub, None)
            else:
                name[sub] = value
        attrs["name"] = name
        if name.get("formatted") and "display_name" not in changes:
            changes.setdefault("display_name", name.get("formatted"))
    elif low.startswith("emails"):
        if op == "remove":
            attrs["emails"] = []
            changes["email"] = None
        elif low == "emails":
            items = value if isinstance(value, list) else [value]
            attrs["emails"] = items
            changes["email"] = _primary_email(items)
        else:  # emails[type eq "work"].value — Entra ID's form
            attrs["emails"] = [{"value": value, "type": "work", "primary": True}]
            changes["email"] = str(value)
    elif low.startswith(ENTERPRISE_SCHEMA.lower()):
        ext = dict(attrs.get(ENTERPRISE_SCHEMA) or {})
        key = p[len(ENTERPRISE_SCHEMA) :].lstrip(":.")
        if op == "remove":
            ext.pop(key, None)
        else:
            ext[key] = value
        attrs[ENTERPRISE_SCHEMA] = ext
    else:
        raise ScimError(400, f"path {path!r} is not supported", "invalidPath")


def patch_changes(acc: directory.Account, body: dict[str, Any]) -> dict[str, Any]:
    """Translate a PatchOp request into directory field changes (validated, all-or-nothing)."""
    if not isinstance(body, dict) or PATCH_SCHEMA not in (body.get("schemas") or []):
        raise ScimError(400, "body must be a PatchOp message", "invalidSyntax")
    ops = body.get("Operations")
    if not isinstance(ops, list) or not ops:
        raise ScimError(400, "Operations must be a non-empty list", "invalidSyntax")
    changes: dict[str, Any] = {}
    attrs = dict(acc.attributes or {})
    for raw in ops:
        if not isinstance(raw, dict):
            raise ScimError(400, "each operation must be an object", "invalidSyntax")
        op = str(raw.get("op", "")).strip().lower()
        if op not in {"add", "replace", "remove"}:
            raise ScimError(400, f"unsupported op {raw.get('op')!r}", "invalidSyntax")
        path, value = raw.get("path"), raw.get("value")
        if path:
            _apply_path(changes, attrs, str(path), value, op)
        elif isinstance(value, dict):  # Okta's form: {"op":"replace","value":{"active":false}}
            for key, val in value.items():
                _apply_path(changes, attrs, str(key), val, op)
        else:
            raise ScimError(
                400, "an operation without a path needs an object value", "invalidValue"
            )
    changes["attributes"] = attrs
    return changes


# ── the protocol operations ───────────────────────────────────────────────────


def _owned(provider: ProviderConfig, account_id: str) -> directory.Account:
    acc = directory.get(account_id)
    if acc is None or acc.provider != provider.name:  # another center's id reads as unknown
        raise ScimError(404, f"User {account_id} not found")
    return acc


def list_users(
    provider: ProviderConfig, base_url: str, *, filter: str | None, start_index: int, count: int
) -> dict[str, Any]:
    filters = parse_filter(filter)
    start = max(1, start_index)
    size = max(0, min(count, MAX_PAGE))
    accounts, total = directory.list_accounts(
        provider.name, filters=filters, offset=start - 1, limit=size
    )
    return {
        "schemas": [LIST_SCHEMA],
        "totalResults": total,
        "startIndex": start,
        "itemsPerPage": len(accounts),
        "Resources": [to_resource(a, base_url) for a in accounts],
    }


def create_user(provider: ProviderConfig, base_url: str, payload: dict[str, Any]) -> dict[str, Any]:
    fields = _fields_from_resource(payload)
    try:
        acc = directory.provision(provider.name, actor=f"scim:{provider.name}", **fields)
    except directory.ConflictError as exc:
        raise ScimError(409, str(exc), "uniqueness") from exc
    return to_resource(acc, base_url)


def get_user(provider: ProviderConfig, base_url: str, account_id: str) -> dict[str, Any]:
    return to_resource(_owned(provider, account_id), base_url)


def replace_user(
    provider: ProviderConfig, base_url: str, account_id: str, payload: dict[str, Any]
) -> dict[str, Any]:
    acc = _owned(provider, account_id)
    fields = _fields_from_resource(payload)
    other = directory.find(provider.name, username=fields["username"], include_deleted=False)
    if other is not None and other.id != acc.id:
        raise ScimError(409, f"userName {fields['username']!r} is taken", "uniqueness")
    directory.update(acc, actor=f"scim:{provider.name}", **fields)
    return to_resource(acc, base_url)


def patch_user(
    provider: ProviderConfig, base_url: str, account_id: str, body: dict[str, Any]
) -> dict[str, Any]:
    acc = _owned(provider, account_id)
    changes = patch_changes(acc, body)
    new_name = changes.get("username")
    if new_name:
        other = directory.find(provider.name, username=new_name, include_deleted=False)
        if other is not None and other.id != acc.id:
            raise ScimError(409, f"userName {new_name!r} is taken", "uniqueness")
    directory.update(acc, actor=f"scim:{provider.name}", **changes)
    return to_resource(acc, base_url)


def delete_user(provider: ProviderConfig, account_id: str) -> None:
    directory.delete(_owned(provider, account_id), actor=f"scim:{provider.name}")


# ── discovery documents (RFC 7644 §4) ─────────────────────────────────────────


def service_provider_config(base_url: str) -> dict[str, Any]:
    return {
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"],
        "documentationUri": "docs/guides/identity-federation.md",
        "patch": {"supported": True},
        "bulk": {"supported": False, "maxOperations": 0, "maxPayloadSize": 0},
        "filter": {"supported": True, "maxResults": MAX_PAGE},
        "changePassword": {"supported": False},
        "sort": {"supported": False},
        "etag": {"supported": False},
        "authenticationSchemes": [
            {
                "type": "oauthbearertoken",
                "name": "Bearer token",
                "description": "Per-provider SCIM bearer (provisioning.token_ref in the trust file)",
                "primary": True,
            }
        ],
        "meta": {
            "resourceType": "ServiceProviderConfig",
            "location": f"{base_url}/ServiceProviderConfig",
        },
    }


def resource_types(base_url: str) -> dict[str, Any]:
    user = {
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ResourceType"],
        "id": "User",
        "name": "User",
        "endpoint": "/Users",
        "schema": USER_SCHEMA,
        "schemaExtensions": [{"schema": ENTERPRISE_SCHEMA, "required": False}],
        "meta": {"resourceType": "ResourceType", "location": f"{base_url}/ResourceTypes/User"},
    }
    return {
        "schemas": [LIST_SCHEMA],
        "totalResults": 1,
        "startIndex": 1,
        "itemsPerPage": 1,
        "Resources": [user],
    }


def schemas(base_url: str) -> dict[str, Any]:
    def attr(name: str, typ: str = "string", **kw: Any) -> dict[str, Any]:
        return {
            "name": name,
            "type": typ,
            "multiValued": kw.get("multi", False),
            "required": kw.get("required", False),
            "mutability": kw.get("mutability", "readWrite"),
            "returned": "default",
            "uniqueness": kw.get("uniqueness", "none"),
        }

    user = {
        "id": USER_SCHEMA,
        "name": "User",
        "description": "A federated ExaMLOps account",
        "attributes": [
            attr("userName", required=True, uniqueness="server"),
            attr("externalId"),
            attr("displayName"),
            attr("active", "boolean"),
            attr("name", "complex"),
            attr("emails", "complex", multi=True),
        ],
        "meta": {"resourceType": "Schema", "location": f"{base_url}/Schemas/{USER_SCHEMA}"},
    }
    return {
        "schemas": [LIST_SCHEMA],
        "totalResults": 1,
        "startIndex": 1,
        "itemsPerPage": 1,
        "Resources": [user],
    }
