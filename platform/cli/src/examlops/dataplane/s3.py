"""S3 for the dataplane through pyarrow's native filesystem (ADR 0130 §5, §7).

Why pyarrow and not s3fs: s3fs needs aiobotocore, and every aiobotocore release caps botocore well
below the latest (aiobotocore 3.9.1: ``botocore>=1.43.66,<1.43.76``), while the workspace requires
``boto3>=1.43.88`` — which needs ``botocore>=1.43.88``. An extra that pulls in s3fs can therefore
never be locked together with the workspace root, and because aiobotocore always trails botocore
the conflict is structural, not transient. pyarrow is already a hard dependency of every dataplane
extra and its PyPI wheels ship ``pyarrow.fs.S3FileSystem``; fsspec's ``ArrowFSWrapper`` turns it
into a normal fsspec filesystem. So S3 costs zero extra dependencies.

``url_to_fs`` is a drop-in for ``fsspec.core.url_to_fs``: ``s3://`` is served here (translating the
s3fs-style options the callers already pass), every other scheme goes to fsspec unchanged.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from fsspec import AbstractFileSystem

# MinIO and most S3-compatible stores ignore the region but the AWS SDK still signs with one; a
# fixed default also stops the SDK from asking the EC2 metadata endpoint which region it is in.
_DEFAULT_REGION = "us-east-1"
_OPTIONS = frozenset({"key", "secret", "endpoint_url", "region", "anon", "client_kwargs"})
_CLIENT_KWARGS = frozenset({"endpoint_url", "region_name"})


def _arrow_s3(**kwargs: Any) -> Any:
    """Build a ``pyarrow.fs.S3FileSystem`` (a seam: tests swap it for a local filesystem)."""
    try:
        from pyarrow.fs import S3FileSystem
    except ImportError as exc:
        raise ImportError(
            "s3:// needs a pyarrow build with S3 support (pyarrow.fs.S3FileSystem); the PyPI "
            "wheels include it, a source build without ARROW_S3 or conda's pyarrow-core does not"
        ) from exc
    return S3FileSystem(**kwargs)


def parse_endpoint(endpoint_url: str) -> tuple[str, str, int | None]:
    """``" http://minio:9000 "`` -> ``("http", "minio", 9000)``; the port is ``None`` when absent.

    One validation shared by :func:`s3_filesystem` and the files connector's egress guard. Never
    echoes the URL: it may carry credentials, and error text crosses the service boundary.
    """
    parts = urlsplit(endpoint_url.strip())
    if parts.scheme not in ("http", "https"):
        raise ValueError("S3 endpoint_url must start with http:// or https://")
    if "@" in parts.netloc:
        raise ValueError("S3 endpoint_url must not carry credentials; use the key and secret")
    if not parts.hostname:
        raise ValueError("S3 endpoint_url has no host")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ValueError("S3 endpoint_url must be scheme://host[:port], with no path or query")
    try:
        port = parts.port
    except ValueError:  # "minio:abc", "minio:99999" — urlsplit's own text names the bad port
        raise ValueError("S3 endpoint_url has an invalid port (expected 1-65535)") from None
    if port == 0:
        raise ValueError("S3 endpoint_url has an invalid port (expected 1-65535)")
    return parts.scheme, parts.hostname, port


def _netloc(host: str, port: int | None) -> str:
    bracketed = f"[{host}]" if ":" in host else host  # an IPv6 literal
    return f"{bracketed}:{port}" if port else bracketed


def s3_filesystem(
    *,
    key: str | None,
    secret: str | None,
    endpoint_url: str | None,
    anonymous: bool,
    region: str | None = None,
) -> AbstractFileSystem:
    """An fsspec filesystem over ``pyarrow.fs.S3FileSystem``. Constructing it opens no socket.

    Credentials are explicit: a key *and* a secret, or neither (half a pair is refused). With
    neither, ``anonymous`` decides — every caller must choose:

    - ``True`` for a dataplane **source**: it reads anonymously and never borrows the service's own
      identity from the AWS default chain (env vars, ``~/.aws``, IRSA, the EC2 metadata endpoint);
    - ``False`` for the operator-trusted **platform store**: the default chain may serve it.

    ``anonymous=True`` together with explicit credentials is a contradiction and is refused. The
    region defaults to ``AWS_REGION``, then ``AWS_DEFAULT_REGION``, then ``us-east-1``.
    """
    from fsspec.implementations.arrow import ArrowFSWrapper

    key, secret = key or None, secret or None
    if (key is None) != (secret is None):
        raise ValueError("S3 credentials need both an access key and a secret key, or neither")
    if anonymous and key is not None:
        raise ValueError("S3 anonymous access cannot be combined with an access key and secret")
    kwargs: dict[str, Any] = {
        "region": region
        or os.getenv("AWS_REGION")
        or os.getenv("AWS_DEFAULT_REGION")
        or _DEFAULT_REGION
    }
    if key is None:
        kwargs["anonymous"] = anonymous
    else:
        kwargs["access_key"], kwargs["secret_key"] = key, secret
    if endpoint_url:
        scheme, host, port = parse_endpoint(endpoint_url)
        kwargs["endpoint_override"], kwargs["scheme"] = _netloc(host, port), scheme
    # fsspec's instance cache is a plain dict keyed on the wrapped filesystem's repr, which is
    # unique per pyarrow object: cached, every call would pin an S3 client for the process's life.
    return ArrowFSWrapper(_arrow_s3(**kwargs), skip_instance_cache=True)


def url_to_fs(url: str, **storage_options: Any) -> tuple[AbstractFileSystem, str]:
    """``fsspec.core.url_to_fs`` with ``s3://`` served by :func:`s3_filesystem`.

    For S3 the returned path is ``bucket/prefix`` (no scheme, no trailing slash). The accepted
    options are the s3fs-style ones the dataplane passes — ``key``, ``secret``, ``endpoint_url``,
    ``region``, ``anon`` and ``client_kwargs={"endpoint_url", "region_name"}``; anything else is
    refused rather than silently dropped, since an ignored ``token`` or ``use_ssl`` would change
    who or how the request runs. ``anon`` unset means anonymous exactly when no credentials are
    given (the safe default); the platform store passes ``anon=False`` explicitly.
    """
    if urlsplit(url).scheme != "s3":
        import fsspec.core

        fs, path = fsspec.core.url_to_fs(url, **storage_options)
        return fs, path
    unknown = sorted(set(storage_options) - _OPTIONS)
    client_kwargs = dict(storage_options.get("client_kwargs") or {})
    unknown += sorted(f"client_kwargs.{k}" for k in set(client_kwargs) - _CLIENT_KWARGS)
    if unknown:
        raise ValueError(f"unsupported S3 storage options: {', '.join(unknown)}")
    if "://" not in url:
        raise ValueError("an S3 URL must be written s3://<bucket>[/<prefix>]")
    path = url.split("://", 1)[1].rstrip("/")
    if not path.split("/", 1)[0]:
        raise ValueError("an s3:// URL needs a bucket: s3://<bucket>[/<prefix>]")
    key, secret = storage_options.get("key"), storage_options.get("secret")
    anon = storage_options.get("anon")
    fs = s3_filesystem(
        key=key,
        secret=secret,
        endpoint_url=client_kwargs.get("endpoint_url") or storage_options.get("endpoint_url"),
        anonymous=not (key or secret) if anon is None else bool(anon),
        region=storage_options.get("region") or client_kwargs.get("region_name"),
    )
    return fs, path
