"""B2 — `exa gateway`: virtual-key admin + a test chat call (ADR 0010)."""

from __future__ import annotations

import json
import os
from typing import Any

import typer

from examlops.cli import _output

app = typer.Typer(
    help="Model gateway — virtual keys, routing, and cost",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)

_EXAMPLES = (
    "Examples:\n\n"
    "  exa gateway key issue --tenant acme --project chat --budget 50 --model gpt-judge\n\n"
    "  exa gateway key issue --tenant acme --project chat --rpm 60 --tpm 100000\n\n"
    "  exa gateway key list\n\n"
    "  exa gateway key revoke <key-hash>\n\n"
    "  exa gateway chat default --message 'hello there'\n\n"
    "  # A model registered with `exa serve llm start` is a route under its own name\n"
    "  exa gateway chat qwen-vl --message 'Summarise this alert' --key $EXA_KEY\n\n"
    "  exa gateway models --key $EXA_KEY\n\n"
    "  exa gateway routes\n\n"
    "  exa gateway reload"
)


def _actor() -> str:
    return os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "unknown"


# ── The live llm-gateway service (ADR 0151/0155): validate/status/providers ────
#
# Everything above this point is the in-process `examlops.gateway` library (keys, quotas, cache,
# a test chat via `GatewayClient`). These three commands are the first CLI surface over the
# *deployed service itself* — before this an operator had no way to check it short of `curl`ing
# raw endpoints, and no offline gate for a bad `gateway.yaml` before it reached production
# (ADR 0155 decision 2 names `exa gateway validate` as that gate; it did not exist until now).


def default_config_path():
    from examlops.gateway.config import default_config_path as _default

    return _default()


def _sync_client(*, timeout: float | Any = 10.0):
    """A synchronous HTTP client for the gateway service. Its own function so tests can replace
    it with one bound to an in-process fake app, never a real socket."""
    import httpx

    return httpx.Client(timeout=timeout)


def _gateway_url() -> str:
    """Resolve, then validate (ADR 0154) — an env var is exactly the SSRF-shaped input the egress
    check exists for, whether it was set by an operator's typo or a compromised script; the
    in-process `gateway_service_backend` gets the identical check for the identical reason."""
    from examlops.gateway.egress import validate_base_url

    url = os.getenv("EXAMLOPS_LLM_GATEWAY_URL") or os.getenv("AGENT_LLM_GATEWAY_URL") or ""
    return validate_base_url(url.rstrip("/") or "http://127.0.0.1:18020", locality="local")


def _admin_headers() -> dict[str, str]:
    token = os.getenv("LLM_GATEWAY_ADMIN_TOKEN", "")
    if not token:
        _output.error(
            "LLM_GATEWAY_ADMIN_TOKEN is not set — the admin API is disabled without it",
            hint="set it to the same value the gateway service was started with",
        )
    return {"Authorization": f"Bearer {token}"}


@app.command("validate", epilog=_EXAMPLES)
def validate_cfg(
    file: str = typer.Argument(
        None,
        help="Path to a gateway.yaml; defaults to EXAMLOPS_GATEWAY_CONFIG / the site config dir",
    ),
) -> None:
    """Validate a gateway.yaml offline (ADR 0155): every problem, with its path, no network call."""
    from examlops.gateway.config import ConfigError, load_config_file, validate_config

    path = (
        file
        or os.getenv("EXAMLOPS_GATEWAY_CONFIG")
        or (str(p) if (p := default_config_path()) else None)
    )
    if path is None:
        if _output.json_mode:
            _output.print_json({"valid": True, "errors": [], "source": "generated"})
        else:
            _output.ok(
                "no gateway.yaml configured — the service falls back to a generated config "
                "from the reachable Ollama (nothing to validate)"
            )
        return
    try:
        raw = load_config_file(path)
    except ConfigError as exc:
        errors = list(exc.errors)
        if _output.json_mode:
            _output.print_json({"valid": False, "errors": errors, "source": path})
            raise typer.Exit(1) from None
        _output.error("\n".join(errors), hint=f"could not load {path}")
    errors = validate_config(raw)
    if _output.json_mode:
        _output.print_json({"valid": not errors, "errors": errors, "source": path})
        if errors:
            raise typer.Exit(1)
        return
    if errors:
        _output.error("\n".join(errors), hint=f"{path} is invalid")
    _output.ok(f"{path} is valid")


@app.command("status", epilog=_EXAMPLES)
def status() -> None:
    """Is the llm-gateway service up, and which routes can it currently serve?"""
    from examlops.gateway.egress import EgressDenied

    try:
        url = _gateway_url()  # resolved once: the ADR 0154 check runs exactly one time per call
    except EgressDenied as exc:
        _output.error(str(exc))
    try:
        with _sync_client() as client:
            resp = client.get(f"{url}/ready")
    except Exception as exc:  # noqa: BLE001 - a network failure is an operator-facing message
        _output.error(f"llm-gateway is unreachable: {exc}", hint=f"tried {url}")
    try:
        data = resp.json()
    except ValueError:
        _output.error(f"llm-gateway answered with a non-JSON body (HTTP {resp.status_code})")
    if _output.json_mode:
        _output.print_json(data)
        if not data.get("ready"):
            raise typer.Exit(1)
        return
    ready = bool(data.get("ready"))
    routes = data.get("routes") or {}
    _output.print_table(
        f"llm-gateway — {url}",
        ["Route", "Healthy", "Required", "Deployments"],
        [
            [
                name,
                "yes" if r.get("healthy") else "no",
                "yes" if r.get("required") else "no",
                str(r.get("deployments", "?")),
            ]
            for name, r in routes.items()
        ]
        or [["(no routes)", "", "", ""]],
    )
    for w in data.get("warnings") or []:
        _output.warning(w)
    (_output.ok if ready else _output.warning)(f"ready: {ready}")
    if not ready:
        raise typer.Exit(1)


@app.command("providers", epilog=_EXAMPLES)
def providers() -> None:
    """Live provider health from the gateway's admin API — why a deployment is down, not just that it is."""
    from examlops.gateway.egress import EgressDenied

    headers = _admin_headers()  # checked (and may exit) before the URL is even resolved
    try:
        url = _gateway_url()
    except EgressDenied as exc:
        _output.error(str(exc))
    try:
        with _sync_client() as client:
            resp = client.get(f"{url}/admin/health", headers=headers)
    except Exception as exc:  # noqa: BLE001
        _output.error(f"llm-gateway is unreachable: {exc}", hint=f"tried {url}")
    if resp.status_code == 401:
        _output.error("the gateway rejected the admin token", hint="check LLM_GATEWAY_ADMIN_TOKEN")
    if resp.status_code >= 400:
        _output.error(f"llm-gateway admin API returned HTTP {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    if _output.json_mode:
        _output.print_json(data["providers"])
        return
    _output.print_table(
        "Providers",
        ["Name", "Type", "Locality", "Up", "Latency (ms)", "Resident models", "Detail"],
        [
            [
                name,
                p.get("type", ""),
                p.get("locality", ""),
                "yes" if p.get("ok") else "no",
                f"{p.get('latency_ms', 0):.1f}",
                ", ".join(p.get("resident") or []) or "-",
                (p.get("detail") or "")[:80],
            ]
            for name, p in data["providers"].items()
        ],
    )


@app.command("models", epilog=_EXAMPLES)
def models_cmd(
    key: str | None = typer.Option(
        None,
        "--key",
        help="Virtual key — omit for an unauthenticated call (only if the service allows it)",
    ),
) -> None:
    """Models the deployed llm-gateway can currently serve — live from `GET /v1/models`.

    Filtered by the service itself to what ``key`` may reach and to routes it can currently
    serve (a route whose every deployment has an open breaker is left out) — this is what the
    gateway would actually route a chat to right now, not the full configured catalog.
    """
    from examlops.gateway.egress import EgressDenied

    try:
        url = _gateway_url()  # resolved once: the ADR 0154 check runs exactly one time per call
    except EgressDenied as exc:
        _output.error(str(exc))
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    try:
        with _sync_client() as client:
            resp = client.get(f"{url}/v1/models", headers=headers)
    except Exception as exc:  # noqa: BLE001 - a network failure is an operator-facing message
        _output.error(f"llm-gateway is unreachable: {exc}", hint=f"tried {url}")
    if resp.status_code == 401:
        _output.error(
            "the gateway rejected the key",
            hint="pass --key, or check LLM_GATEWAY_AUTH on the service",
        )
    if resp.status_code >= 400:
        _output.error(f"llm-gateway returned HTTP {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    if _output.json_mode:
        _output.print_json(data)
        return
    rows = data.get("data") or []
    if not rows:
        _output.ok("No models are currently servable (or none are visible to this key).")
        return
    _output.print_table(
        f"Models — {url}",
        ["Model", "Provider"],
        [[m.get("id", ""), m.get("owned_by", "")] for m in rows],
    )


@app.command("routes", epilog=_EXAMPLES)
def routes_cmd() -> None:
    """The configured route table — every route, its strategy/deployments/fallbacks, and aliases.

    Distinct from `models` (what's *currently servable*, filtered by breaker state and a key) and
    `providers` (per-provider *health*): this is the full configured topology from `GET
    /admin/config`, the same source `exa gateway validate`'s offline check and a live reload both
    ultimately build from.
    """
    from examlops.gateway.egress import EgressDenied

    headers = _admin_headers()  # checked (and may exit) before the URL is even resolved
    try:
        url = _gateway_url()
    except EgressDenied as exc:
        _output.error(str(exc))
    try:
        with _sync_client() as client:
            resp = client.get(f"{url}/admin/config", headers=headers)
    except Exception as exc:  # noqa: BLE001
        _output.error(f"llm-gateway is unreachable: {exc}", hint=f"tried {url}")
    if resp.status_code == 401:
        _output.error("the gateway rejected the admin token", hint="check LLM_GATEWAY_ADMIN_TOKEN")
    if resp.status_code >= 400:
        _output.error(f"llm-gateway admin API returned HTTP {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    if _output.json_mode:
        _output.print_json(data)
        return
    routes = data.get("routes") or {}
    reverse_aliases: dict[str, list[str]] = {}
    for alias, target in (data.get("aliases") or {}).items():
        reverse_aliases.setdefault(target, []).append(alias)
    _output.print_table(
        f"Routes — {url} (source: {data.get('source', '?')})",
        ["Route", "Aliases", "Strategy", "Required", "Deployments", "Fallbacks"],
        [
            [
                name,
                ", ".join(reverse_aliases.get(name, [])) or "-",
                r.get("strategy", ""),
                "yes" if r.get("required") else "no",
                ", ".join(f"{d['provider']}/{d['model']}" for d in r.get("deployments", [])),
                ", ".join(r.get("fallbacks") or []) or "-",
            ]
            for name, r in routes.items()
        ]
        or [["(no routes)", "", "", "", "", ""]],
    )
    if err := data.get("last_reload_error"):
        _output.warning(f"last reload was rejected: {'; '.join(err)}")


@app.command("reload", epilog=_EXAMPLES)
def reload_cmd() -> None:
    """Reload the deployed llm-gateway's `gateway.yaml` — `POST /admin/reload`.

    ADR 0155 d3: a rejected config never bricks the gateway — the previous one keeps serving and
    this reports exactly why the new one was refused, same as the MCP `gateway_service_reload`
    tool this mirrors (an operator on the CLI should never have strictly less visibility than an
    agent calling the same admin endpoint).
    """
    from examlops.data.audit import write_audit_event
    from examlops.gateway.egress import EgressDenied

    headers = _admin_headers()  # checked (and may exit) before the URL is even resolved
    try:
        url = _gateway_url()
    except EgressDenied as exc:
        _output.error(str(exc))
    try:
        with _sync_client() as client:
            resp = client.post(f"{url}/admin/reload", headers=headers)
    except Exception as exc:  # noqa: BLE001
        _output.error(f"llm-gateway is unreachable: {exc}", hint=f"tried {url}")
    if resp.status_code == 401:
        _output.error("the gateway rejected the admin token", hint="check LLM_GATEWAY_ADMIN_TOKEN")
    if resp.status_code == 422:
        data = resp.json()
        errors = (data.get("error") or {}).get("errors") or []
        if _output.json_mode:
            _output.print_json({"reloaded": False, "errors": errors})
            raise typer.Exit(1)
        _output.error(
            "config rejected; the previous config keeps serving:\n  " + "\n  ".join(errors)
        )
    if resp.status_code >= 400:
        _output.error(f"llm-gateway admin API returned HTTP {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    write_audit_event(
        "exa-gateway", _actor(), "gateway_reloaded", url, {"routes": data.get("routes")}
    )
    if _output.json_mode:
        _output.print_json(data)
        return
    _output.ok(f"reloaded — {len(data.get('routes') or [])} route(s)")
    for w in data.get("warnings") or []:
        _output.warning(w)


key_app = typer.Typer(
    help="Virtual key administration",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)
app.add_typer(key_app, name="key")


@key_app.command("issue", epilog=_EXAMPLES)
def key_issue(
    tenant: str = typer.Option("default", "--tenant", help="Tenant the key belongs to"),
    project: str = typer.Option("default", "--project", help="Project the key belongs to"),
    model: list[str] = typer.Option(
        None, "--model", help="Allow-list model (repeatable; omit = all models)"
    ),
    budget: float | None = typer.Option(None, "--budget", help="Budget in USD (omit = unlimited)"),
    rpm: int | None = typer.Option(
        None, "--rpm", help="Requests-per-minute cap (BL-107; omit = unlimited)"
    ),
    tpm: int | None = typer.Option(
        None, "--tpm", help="Tokens-per-minute cap (BL-107; omit = unlimited)"
    ),
) -> None:
    """Issue a virtual key (printed once — only its hash is stored)."""
    from examlops.gateway import issue_virtual_key

    raw = issue_virtual_key(
        tenant,
        project,
        list(model) if model else None,
        budget,
        _actor(),
        rpm_limit=rpm,
        tpm_limit=tpm,
    )
    if _output.json_mode:
        _output.print_json(
            {
                "virtual_key": raw,
                "tenant": tenant,
                "project": project,
                "rpm_limit": rpm,
                "tpm_limit": tpm,
            }
        )
    else:
        _output.ok(f"Issued virtual key for {tenant}/{project} (save it now — shown once):")
        typer.echo(raw)


@key_app.command("list")
def key_list() -> None:
    """List virtual keys (hashes only)."""
    from examlops.data.gateway import list_virtual_keys

    keys = list_virtual_keys()
    if _output.json_mode:
        _output.print_json(keys)
        return
    if not keys:
        _output.ok("No virtual keys issued.")
        return
    rows = [
        [
            k["key_hash"][:16] + "…",
            f"{k['tenant']}/{k['project']}",
            ",".join(k["models"]) or "all",
            "∞" if k["budget_usd"] is None else f"${k['budget_usd']:.2f}",
            f"${k['spent_usd']:.4f}",
            "revoked" if k["revoked"] else "active",
        ]
        for k in keys
    ]
    _output.print_table(
        "Virtual Keys",
        ["Key (hash)", "Tenant/Project", "Models", "Budget", "Spent", "Status"],
        rows,
    )


@key_app.command("revoke")
def key_revoke(key_hash: str = typer.Argument(..., help="Key hash prefix or full hash")) -> None:
    """Revoke a virtual key by its stored hash."""
    from examlops.data.audit import write_audit_event
    from examlops.data.gateway import list_virtual_keys
    from examlops.data.governance import revoke_virtual_key

    matches = [k for k in list_virtual_keys() if k["key_hash"].startswith(key_hash)]
    if not matches:
        _output.error(f"No key matching hash '{key_hash}'")
    if len(matches) > 1:
        _output.error(f"Ambiguous hash '{key_hash}' matches {len(matches)} keys")
    kh = matches[0]["key_hash"]
    revoke_virtual_key(kh)
    write_audit_event("exa-gateway", _actor(), "virtual_key_revoked", kh, None)
    _output.ok(f"Revoked key {kh[:16]}…")


quota_app = typer.Typer(
    help="Per-tenant request quotas the serving gateway enforces (ADR 0123)",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)
app.add_typer(quota_app, name="quota")


@quota_app.command(
    "set",
    epilog="Examples:\n\n  exa gateway quota set acme 120\n\n  exa gateway quota set batch 0  # unlimited",
)
def quota_set(
    tenant: str = typer.Argument(..., help="Tenant name (the virtual key's tenant)"),
    rpm: int = typer.Argument(
        ..., min=0, help="Requests per minute; 0 = unlimited for this tenant"
    ),
) -> None:
    """Cap a tenant's requests per minute. Reaches the gateway in the next serving snapshot."""
    from examlops.data import serving_quotas

    result = serving_quotas.set_quota(tenant, rpm, updated_by=_actor())
    if _output.json_mode:
        _output.print_json({"tenant": tenant, "rpm": rpm, "result": result})
        return
    _output.ok(f"{tenant}: {result}, {rpm or 'unlimited'} requests/min (via the serving snapshot)")


@quota_app.command("list")
def quota_list() -> None:
    """List the per-tenant quotas (tenants without one use EXAMLOPS_GATEWAY_TENANT_RPM)."""
    from examlops.data import serving_quotas

    rows = serving_quotas.list_quotas()
    if _output.json_mode:
        _output.print_json(rows)
        return
    if not rows:
        _output.info("No per-tenant quotas set; every tenant uses the gateway default.")
        return
    for row in rows:
        _output.info(f"{row['tenant']}  rpm={row['rpm']}  by={row['updated_by'] or '-'}")


@quota_app.command("remove")
def quota_remove(tenant: str = typer.Argument(..., help="Tenant whose quota to drop")) -> None:
    """Drop a tenant's quota so it falls back to the gateway default."""
    from examlops.data import serving_quotas

    removed = serving_quotas.remove_quota(tenant, updated_by=_actor())
    if _output.json_mode:
        _output.print_json({"tenant": tenant, "result": "removed" if removed else "not_found"})
        return
    if not removed:
        _output.error(f"No quota set for tenant '{tenant}'")
    _output.ok(f"{tenant}: quota removed (gateway default applies)")


cache_app = typer.Typer(
    help="Semantic cache (B3) — hit-rate + measured savings",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)
app.add_typer(cache_app, name="cache")


@cache_app.command("stats")
def cache_stats_cmd(
    tenant: str | None = typer.Option(None, "--tenant", help="Filter to one tenant"),
) -> None:
    """Show semantic-cache hit-rate and token/cost savings (B3)."""
    from examlops.data.gateway import cache_stats

    stats = cache_stats(tenant)
    if _output.json_mode:
        _output.print_json(stats)
        return
    _output.print_table(
        "Semantic Cache" + (f" — {tenant}" if tenant else ""),
        ["Hits", "Misses", "Hit rate", "Tokens saved", "Cost saved"],
        [
            [
                str(stats["hits"]),
                str(stats["misses"]),
                f"{stats['hit_rate'] * 100:.1f}%",
                str(stats["tokens_saved"]),
                f"${stats['cost_saved']:.6f}",
            ]
        ],
    )


@app.command("chat", epilog=_EXAMPLES)
def chat(
    model: str = typer.Argument(
        "default", help="Route name: a registered LLM endpoint, or 'default' (echo)"
    ),
    message: str = typer.Option(..., "--message", help="User message"),
    key: str | None = typer.Option(None, "--key", help="Virtual key to authenticate with"),
    cache: bool = typer.Option(False, "--cache", help="Route through the B3 semantic cache"),
    stream: bool = typer.Option(
        False,
        "--stream",
        help="Stream tokens live over SSE from the deployed llm-gateway service, "
        "instead of one completed answer from the in-process client",
    ),
) -> None:
    """Send one chat message through the gateway, to a registered endpoint or the echo route."""
    if stream:
        _chat_stream(model, message, key)
        return
    from examlops.gateway import GatewayClient, GatewayError, build_default_router

    cache_lookup = cache_store = None
    if cache:
        from examlops.semantic_cache import SemanticCache, bind_to_gateway

        cache_lookup, cache_store = bind_to_gateway(SemanticCache())
    client = GatewayClient(
        build_default_router(), virtual_key=key, cache_lookup=cache_lookup, cache_store=cache_store
    )
    from examlops.structured import StructuredOutputError

    try:
        comp = client.chat(model, [{"role": "user", "content": message}])
    except (GatewayError, StructuredOutputError) as exc:
        # StructuredOutputError is not a GatewayError: a route default schema in structured.yaml
        # (ADR 0035 clause 3) can make this plain request a structured one, and an answer that
        # cannot be made valid must be a clean refusal, not a traceback.
        _output.error(f"{type(exc).__name__}: {exc}")
        return
    if _output.json_mode:
        payload: dict[str, object] = {
            "text": comp.text,
            "backend": comp.backend,
            "cost_usd": comp.cost_usd,
            "cached": comp.cached,
        }
        if comp.parsed is not None:
            payload["parsed"] = comp.parsed
        _output.print_json(payload)
        return
    tag = " (cached)" if comp.cached else ""
    _output.ok(f"[{comp.backend}] {comp.text}  (cost ${comp.cost_usd:.6f}){tag}")


def _chat_stream(model: str, message: str, key: str | None) -> None:
    """`--stream`: real SSE against the *deployed* service, not the in-process `GatewayClient` —
    that library has no streaming API at all (`GatewayClient.chat()` always returns one completed
    `Completion`; the only place a real token stream exists today is `POST /v1/chat/completions`
    on the running service). Plain text only — there is no sensible `--json` shape for a live
    stream of deltas, so `-o json`/`--json` is ignored here rather than silently buffering the
    whole reply just to wrap it in one JSON document, which would defeat the point of `--stream`.
    """
    from examlops.gateway.egress import EgressDenied

    try:
        url = _gateway_url()  # resolved once: the ADR 0154 check runs exactly one time per call
    except EgressDenied as exc:
        _output.error(str(exc))
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    body = {"model": model, "messages": [{"role": "user", "content": message}], "stream": True}
    # Fail fast on an unreachable host, but wait as long as a real cold model load can take (ADR
    # 0153 d7) — the same connect/read split every provider in this package uses, not the CLI's
    # usual flat 10s (fine for a quick admin read, wrong for a first token that may need to load
    # a model first).
    import httpx

    try:
        with (
            _sync_client(timeout=httpx.Timeout(300.0, connect=3.0)) as client,
            client.stream("POST", f"{url}/v1/chat/completions", json=body, headers=headers) as resp,
        ):
            if resp.status_code >= 400:
                resp.read()
                try:
                    err = resp.json().get("error", {})
                except ValueError:
                    err = {}
                _output.error(
                    err.get("message") or f"llm-gateway returned HTTP {resp.status_code}",
                    hint=f"code={err.get('code', '?')} request_id={err.get('request_id', '?')}",
                )
            printed_any = False
            for raw_line in resp.iter_lines():
                line = raw_line.strip()
                if not line or line.startswith(":") or not line.startswith("data:"):
                    continue
                payload = line[len("data:") :].strip()
                if payload == "[DONE]":
                    break
                try:
                    data = json.loads(payload)
                except ValueError:
                    continue  # a malformed keep-alive/comment line, not a fatal condition mid-stream
                if "error" in data:
                    if printed_any:
                        typer.echo()  # end the partial line before the error message
                    _output.error(
                        (data.get("error") or {}).get("message", "the stream ended in error")
                    )
                choices = data.get("choices") or []
                delta = (choices[0].get("delta") or {}) if choices else {}
                text = delta.get("content") or ""
                if text:
                    typer.echo(text, nl=False)
                    printed_any = True
            if printed_any:
                typer.echo()  # a trailing newline after the last streamed token
            elif not _output.json_mode:
                _output.warning("the stream produced no content")
    except Exception as exc:  # noqa: BLE001 - a network failure is an operator-facing message
        _output.error(f"llm-gateway is unreachable: {exc}", hint=f"tried {url}")


# ── B8: structured output + reasoning ops (ADR 0035) ──────────────────────────
schema_app = typer.Typer(no_args_is_help=True, help="Structured output — schema-constrained (B8)")
app.add_typer(schema_app, name="schema")

reasoning_app = typer.Typer(
    no_args_is_help=True, help="Reasoning ops — budget/accounting/trace (B8)"
)
app.add_typer(reasoning_app, name="reasoning")


@schema_app.command("test")
def schema_test(
    schema_file: str = typer.Argument(
        ...,
        help="Path to a JSON Schema file, or a registered schema name (exa gateway schema list)",
    ),
    object_file: str = typer.Argument(..., help="Path to a JSON object to validate"),
    repair: bool = typer.Option(True, "--repair/--no-repair", help="Attempt repair on invalid"),
) -> None:
    """Validate (and optionally repair) an object against a JSON Schema (R1/R8)."""
    import json as _json

    from examlops.structured import repair_object, validate_object
    from examlops.structured.policy import UnknownSchemaError, get_schema

    if os.path.isfile(schema_file):
        with open(schema_file) as fh:
            schema = _json.load(fh)
    else:
        try:
            schema = get_schema(schema_file)
        except UnknownSchemaError as exc:
            _output.error(f"{exc} — and no file named {schema_file!r}", exit_code=2)
    with open(object_file) as fh:
        obj = _json.load(fh)
    errors = validate_object(obj, schema)
    if not errors:
        _output.ok("Object is valid against the schema.")
        return
    if repair:
        fixed = repair_object(obj, schema)
        if not validate_object(fixed, schema):
            _output.warning("Invalid — repaired to a valid object:")
            _output.info(_json.dumps(fixed, indent=2))
            return
    _output.error(f"Invalid: {'; '.join(errors)}")
    raise typer.Exit(1)


@schema_app.command("list")
def schema_list() -> None:
    """Registered output schemas + per-route defaults from structured.yaml (ADR 0035 cl. 3)."""
    from examlops.structured.policy import (
        StructuredConfigError,
        config_path,
        list_schemas,
        load_config,
    )

    try:
        cfg = load_config()
    except StructuredConfigError as exc:
        _output.error(str(exc))
    schemas = list_schemas(cfg)
    routes = [{"route": r, **spec} for r, spec in sorted(cfg.routes.items())]
    if _output.json_mode:
        _output.print_json({"config": str(config_path()), "schemas": schemas, "routes": routes})
        return
    _output.print_table(
        "Output schemas",
        ["name", "source", "description"],
        [[r["name"], r["source"], r["description"]] for r in schemas],
    )
    if routes:
        _output.print_table(
            f"Route defaults ({config_path()})",
            ["route", "response_schema", "reasoning_budget"],
            [
                [r["route"], r.get("response_schema", "-"), r.get("reasoning_budget", "-")]
                for r in routes
            ],
        )
    else:
        _output.info(f"No route defaults ({config_path()} absent or empty).")


@schema_app.command("show")
def schema_show(
    name: str = typer.Argument(..., help="Schema name (see exa gateway schema list)"),
) -> None:
    """Print one registered output schema as JSON."""
    import json as _json

    from examlops.structured.policy import UnknownSchemaError, get_schema

    try:
        schema = get_schema(name)
    except UnknownSchemaError as exc:
        _output.error(str(exc), exit_code=2)
    if _output.json_mode:
        _output.print_json(schema)
        return
    _output.info(_json.dumps(schema, indent=2))


@reasoning_app.command("account")
def reasoning_account(
    model: str = typer.Argument(..., help="Model name"),
    reasoning_tokens: int = typer.Option(..., "--reasoning", help="Reasoning (thinking) tokens"),
    output_tokens: int = typer.Option(..., "--output", help="Output tokens"),
    reasoning_rate: float = typer.Option(0.0, "--reasoning-rate", help="$/reasoning token"),
    output_rate: float = typer.Option(0.0, "--output-rate", help="$/output token"),
    tenant: str = typer.Option("default", "--tenant", help="Tenant scope"),
) -> None:
    """Account reasoning vs output tokens/cost separately (R5)."""
    from examlops.structured import account_reasoning

    result = account_reasoning(
        model,
        reasoning_tokens,
        output_tokens,
        reasoning_rate=reasoning_rate,
        output_rate=output_rate,
        tenant=tenant,
    )
    if _output.json_mode:
        _output.print_json(result)
        return
    _output.ok(
        f"{model}: reasoning {result['reasoning_tokens']}tok (${result['reasoning_cost']}) + "
        f"output {result['output_tokens']}tok (${result['output_cost']}) = ${result['total_cost']}"
    )


@reasoning_app.command("budget")
def reasoning_budget(
    requested: int = typer.Argument(..., help="Requested thinking tokens"),
    max_thinking: int = typer.Option(..., "--max", help="Reasoning budget (max thinking tokens)"),
) -> None:
    """Show how a reasoning budget caps a request (R4)."""
    from examlops.structured import ReasoningBudget

    allowed, cut = ReasoningBudget(max_thinking).enforce(requested)
    if _output.json_mode:
        _output.print_json({"requested": requested, "allowed": allowed, "cut": cut})
        return
    if cut:
        _output.warning(f"Thinking cut off at {allowed} tokens (requested {requested}).")
    else:
        _output.ok(f"Within budget: {allowed} thinking tokens.")


@reasoning_app.command("stats")
def reasoning_stats(
    model: str = typer.Option(None, "--model", help="Filter by model"),
    tenant: str = typer.Option(None, "--tenant", help="Filter by tenant"),
) -> None:
    """Reasoning-vs-output token/cost split + structured-output outcomes."""
    from examlops.data.events import reasoning_usage_summary, structured_output_stats

    summary = reasoning_usage_summary(model, tenant)
    outcomes = structured_output_stats()
    if _output.json_mode:
        _output.print_json({"reasoning": summary, "structured_output": outcomes})
        return
    _output.print_record(
        {
            "reasoning_tokens": summary["reasoning_tokens"],
            "output_tokens": summary["output_tokens"],
            "reasoning_cost": f"${summary['reasoning_cost']:.6f}",
            "output_cost": f"${summary['output_cost']:.6f}",
            "structured_valid": outcomes.get("valid", 0),
            "structured_repaired": outcomes.get("repaired", 0),
            "structured_failed": outcomes.get("failed", 0),
        }
    )


@reasoning_app.command("set-budget")
def reasoning_set_budget(
    max_thinking: int = typer.Argument(..., min=0, help="Max thinking tokens per request"),
    model: str = typer.Option(None, "--model", help="Cap for this logical model"),
    project: str = typer.Option(None, "--project", help="Cap for this project's virtual keys"),
    key_hash: str = typer.Option(None, "--key-hash", help="Cap for one virtual key (its hash)"),
    tenant: str = typer.Option("default", "--tenant", help="Tenant scope"),
    remove: bool = typer.Option(False, "--remove", help="Delete the cap instead of setting it"),
) -> None:
    """Set (or --remove) a gateway reasoning budget; the tightest applicable cap wins (ADR 0035)."""
    from examlops.data import reasoning_budgets as store

    chosen = [(s, r) for s, r in (("model", model), ("project", project), ("key", key_hash)) if r]
    if len(chosen) != 1:
        _output.error("Pass exactly one of --model, --project, --key-hash.")
        raise typer.Exit(2)
    scope, ref = chosen[0]
    # ADR 0035 clause 3: a budget change is a governed mutation - D5 decides, and it is audited.
    from examlops.structured.policy import budget_change_decision

    decision = budget_change_decision(
        scope, ref, tenant=tenant, max_thinking_tokens=max_thinking, remove=remove
    )
    if not decision.allowed:
        _output.error(f"Refused by policy ({decision.effect}): {decision.reason}")
    if remove:
        result = "removed" if store.remove(scope, ref, tenant) else "not_found"
    else:
        result = store.put(scope, ref, max_thinking, tenant)
    from examlops.data.audit import audit_best_effort

    audit_best_effort(
        "exa-gateway",
        _actor(),
        "reasoning_budget_removed" if remove else "reasoning_budget_set",
        f"{scope}:{ref}",
        {"max_thinking_tokens": None if remove else max_thinking, "result": result},
        tenant=tenant,
    )
    if _output.json_mode:
        _output.print_json(
            {"scope": scope, "ref": ref, "tenant": tenant, "max": max_thinking, "result": result}
        )
        return
    _output.ok(f"{scope} {ref} ({tenant}): {result}, max {max_thinking} thinking tokens")


@reasoning_app.command("budgets")
def reasoning_budgets(
    tenant: str = typer.Option(None, "--tenant", help="Filter by tenant"),
    events: bool = typer.Option(False, "--events", help="Show recent budget outcomes instead"),
    outcome: str = typer.Option(
        None, "--outcome", help="With --events: within|exceeded|unknown|refused"
    ),
) -> None:
    """List configured reasoning budgets, or (--events) what the gateway observed against them."""
    from examlops.data import reasoning_budgets as store

    rows = store.list_events(outcome=outcome) if events else store.list_budgets(tenant)
    if _output.json_mode:
        _output.print_json(rows)
        return
    if not rows:
        _output.info("No reasoning budget events." if events else "No reasoning budgets set.")
        return
    for row in rows:
        _output.info("  ".join(f"{k}={v}" for k, v in row.items() if v is not None))
