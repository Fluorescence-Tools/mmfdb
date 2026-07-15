"""Runtime configuration for the standalone MMFDB package."""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from ipaddress import ip_address
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml


@dataclass(frozen=True)
class RuntimeConfig:
    """Runtime paths and identity defaults used by MMFDB.

    Parameters
    ----------
    settings_dir : Path or None
        Base settings directory. Defaults to ``$MMFDB_SETTINGS_DIR`` or
        ``~/.chisurf``.
    database_path : Path or None
        Explicit MMFDB SQLite path. Defaults to ``settings_dir/flr/sample_management.db``.
    database_url : str or None
        Explicit SQL database URL. This takes precedence over ``database_path``
        and ``MMFDB_DATABASE_PATH``. PostgreSQL URLs require the optional
        ``mmfdb[postgres]`` dependencies and a provisioned current schema.
    source_database_path : Path or None
        Optional curated source database path copied into the user database path.
    object_store_root : Path or None
        Explicit object-store root. Defaults to ``settings_dir/objects``.
    object_store_backend : str or None
        Blob backend name (``"local"`` or ``"s3"``). Defaults to
        ``$MMFDB_OBJECT_STORE_BACKEND`` or ``"local"``.
    s3_bucket : str or None
        S3 bucket used by the ``s3`` backend.
    s3_prefix : str or None
        Optional key prefix within the S3 bucket.
    s3_endpoint_url : str or None
        Optional S3-compatible endpoint URL. Authentication is intentionally
        delegated to the standard AWS environment/configuration chain.
    s3_region : str or None
        Optional AWS region passed to the S3 client.
    default_user_id : str or None
        Default local user id. When unset, ``$MMFDB_DEFAULT_USER_ID`` or
        ``user_default`` is used.

    """

    settings_dir: Path | None = None
    database_path: Path | None = None
    database_url: str | None = None
    source_database_path: Path | None = None
    object_store_root: Path | None = None
    object_store_backend: str | None = None
    s3_bucket: str | None = None
    s3_prefix: str | None = None
    s3_endpoint_url: str | None = None
    s3_region: str | None = None
    default_user_id: str | None = None


class ConfigError(ValueError):
    """Raised when an MMFDB YAML configuration is invalid or incomplete."""


@dataclass(frozen=True)
class ServerConfig:
    """Standalone HTTP server settings."""

    host: str = "127.0.0.1"
    port: int = 8080


@dataclass(frozen=True)
class DatabaseConfig:
    """Database target selected by a deployment."""

    path: Path | None = None
    url: str | None = None


@dataclass(frozen=True)
class ObjectStoreConfig:
    """Content-addressed object storage settings."""

    backend: str = "local"
    root: Path | None = None
    s3_bucket: str | None = None
    s3_prefix: str = ""
    s3_endpoint_url: str | None = None
    s3_region: str | None = None


@dataclass(frozen=True)
class AuthConfig:
    """Authentication provider settings."""

    provider: str = "local"
    ldap: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class AdminBootstrapConfig:
    """One-shot first-administrator bootstrap settings.

    The password is excluded from representations so config objects can be
    logged without disclosing credentials.
    """

    user: str | None = None
    password: str | None = field(default=None, repr=False)
    allow_weak_bootstrap: bool = False


@dataclass(frozen=True)
class ClientConfig:
    """Connection hints shared with embedding applications such as ChiSurf."""

    mode: str = "embedded"
    base_url: str = "http://127.0.0.1:8080"
    username: str = "admin"
    allow_insecure_http: bool = False


@dataclass(frozen=True)
class DeploymentConfig:
    """Validated, host-neutral MMFDB YAML configuration."""

    version: int = 1
    mode: str = "embedded"
    server: ServerConfig = field(default_factory=ServerConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    object_store: ObjectStoreConfig = field(default_factory=ObjectStoreConfig)
    auth: AuthConfig = field(default_factory=AuthConfig)
    admin: AdminBootstrapConfig = field(default_factory=AdminBootstrapConfig)
    client: ClientConfig = field(default_factory=ClientConfig)
    source_path: Path | None = field(default=None, repr=False, compare=False)


_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _interpolate_env(value: Any) -> Any:
    """Expand ``${NAME}``/``${NAME:-default}`` in YAML string values."""
    if isinstance(value, dict):
        return {key: _interpolate_env(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_interpolate_env(item) for item in value]
    if not isinstance(value, str):
        return value

    def replace_match(match: re.Match[str]) -> str:
        name, fallback = match.groups()
        resolved = os.environ.get(name)
        if resolved is not None and resolved != "":
            return resolved
        if fallback is not None:
            return fallback
        raise ConfigError(f"Environment variable {name} referenced by configuration is not set")

    return _ENV_PATTERN.sub(replace_match, value)


def _section(raw: dict[str, Any], name: str, allowed: set[str]) -> dict[str, Any]:
    value = raw.get(name, {})
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{name} must be a mapping")
    unknown = set(value) - allowed
    if unknown:
        raise ConfigError(f"Unknown {name} configuration keys: {', '.join(sorted(unknown))}")
    return value


def _config_path(value: Any, *, base_dir: Path, field_name: str) -> Path | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ConfigError(f"{field_name} must be a filesystem path")
    path = Path(os.path.expanduser(os.path.expandvars(value)))
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve()


def _read_yaml(path: str | os.PathLike[str] | None) -> tuple[Path, dict[str, Any]]:
    selected = Path(path or os.environ.get("MMFDB_CONFIG", "mmfdb.yaml")).expanduser()
    try:
        loaded = yaml.safe_load(selected.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"MMFDB configuration not found: {selected}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid MMFDB YAML in {selected}: {exc}") from exc
    if loaded is None:
        loaded = {}
    if not isinstance(loaded, dict):
        raise ConfigError("MMFDB configuration root must be a mapping")
    allowed_root = {
        "version", "mode", "server", "database", "object_store", "auth", "admin", "client"
    }
    unknown = set(loaded) - allowed_root
    if unknown:
        raise ConfigError(f"Unknown MMFDB configuration keys: {', '.join(sorted(unknown))}")
    if loaded.get("version", 1) != 1:
        raise ConfigError("Unsupported MMFDB configuration version; expected version: 1")
    return selected, loaded


def _validate_client_config(
    *,
    raw: dict[str, Any],
    deployment_mode: str,
    default_port: int,
) -> ClientConfig:
    client_raw = _section(
        raw, "client", {"mode", "base_url", "username", "allow_insecure_http"}
    )
    client_mode = client_raw.get(
        "mode", "remote" if deployment_mode == "standalone" else "embedded"
    )
    if client_mode not in {"embedded", "remote"}:
        raise ConfigError("client.mode must be 'embedded' or 'remote'")
    base_url = client_raw.get("base_url", f"http://127.0.0.1:{default_port}")
    username = client_raw.get("username", "admin")
    allow_insecure = client_raw.get("allow_insecure_http", False)
    if not isinstance(allow_insecure, bool):
        raise ConfigError("client.allow_insecure_http must be true or false")
    if not isinstance(base_url, str):
        raise ConfigError("client.base_url must be an HTTP(S) URL")
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ConfigError("client.base_url must be an HTTP(S) URL with a hostname")
    if parsed.username is not None or parsed.password is not None:
        raise ConfigError("client.base_url must not contain embedded credentials")
    if parsed.query or parsed.fragment:
        raise ConfigError("client.base_url must not contain a query string or fragment")
    hostname = parsed.hostname.lower()
    is_loopback = hostname == "localhost"
    try:
        is_loopback = is_loopback or ip_address(hostname).is_loopback
    except ValueError:
        pass
    if parsed.scheme == "http" and not is_loopback and not allow_insecure:
        raise ConfigError(
            "client.base_url must use HTTPS for a non-loopback host; set "
            "client.allow_insecure_http: true only for an isolated development network"
        )
    if not isinstance(username, str) or not username:
        raise ConfigError("client.username must be a non-empty string")
    return ClientConfig(
        mode=client_mode,
        base_url=base_url.rstrip("/"),
        username=username,
        allow_insecure_http=allow_insecure,
    )


def load_client_config(path: str | os.PathLike[str] | None = None) -> ClientConfig:
    """Load only safe client/server hints without resolving deployment secrets.

    This is the appropriate loader for ChiSurf and other clients reusing a
    deployment YAML: `${...}` values under admin, LDAP, database, or object
    storage are deliberately left untouched and therefore need not exist in a
    client process.
    """
    _, loaded = _read_yaml(path)
    mode = loaded.get("mode", "embedded")
    if mode not in {"embedded", "standalone"}:
        raise ConfigError("mode must be 'embedded' or 'standalone'")
    selected_raw = _interpolate_env(
        {"server": loaded.get("server", {}), "client": loaded.get("client", {})}
    )
    server_raw = _section(selected_raw, "server", {"host", "port"})
    port = server_raw.get("port", 8080)
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ConfigError("server.port must be an integer between 1 and 65535")
    return _validate_client_config(raw=selected_raw, deployment_mode=mode, default_port=port)


def load_deployment_config(path: str | os.PathLike[str] | None = None) -> DeploymentConfig:
    """Load and validate an MMFDB YAML configuration.

    Relative storage paths are resolved against the YAML file, never against a
    process-dependent working directory. Unknown keys fail closed so spelling
    mistakes cannot silently select insecure defaults.
    """
    selected, loaded = _read_yaml(path)
    raw = _interpolate_env(loaded)
    mode = raw.get("mode", "embedded")
    if mode not in {"embedded", "standalone"}:
        raise ConfigError("mode must be 'embedded' or 'standalone'")

    server_raw = _section(raw, "server", {"host", "port"})
    host = server_raw.get("host", "127.0.0.1")
    port = server_raw.get("port", 8080)
    if not isinstance(host, str) or not host.strip():
        raise ConfigError("server.host must be a non-empty string")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ConfigError("server.port must be an integer between 1 and 65535")

    database_raw = _section(raw, "database", {"path", "url"})
    database_path = _config_path(
        database_raw.get("path"), base_dir=selected.parent, field_name="database.path"
    )
    database_url = database_raw.get("url")
    if database_url is not None and (
        not isinstance(database_url, str) or not database_url.strip()
    ):
        raise ConfigError("database.url must be a non-empty URL")
    if database_path is not None and database_url:
        raise ConfigError("database may not define both path and url")
    if mode == "standalone" and database_path is None and not database_url:
        raise ConfigError("standalone mode requires exactly one database.path or database.url")
    if database_url:
        from mmfdb.store.sql_backend import DatabaseBackendError, parse_database_target

        if "://" not in database_url:
            raise ConfigError("database.url is invalid: expected a SQL URL with a scheme")
        try:
            parse_database_target(database_url)
        except DatabaseBackendError as exc:
            raise ConfigError(f"database.url is invalid: {exc}") from exc

    object_raw = _section(
        raw,
        "object_store",
        {"backend", "root", "s3_bucket", "s3_prefix", "s3_endpoint_url", "s3_region"},
    )
    backend = str(object_raw.get("backend", "local")).strip().lower()
    if backend not in {"local", "s3"}:
        raise ConfigError("object_store.backend must be 'local' or 's3'")
    object_root = _config_path(
        object_raw.get("root"), base_dir=selected.parent, field_name="object_store.root"
    )
    s3_bucket = object_raw.get("s3_bucket")
    if backend == "s3" and not s3_bucket:
        raise ConfigError("object_store.s3_bucket is required for the s3 backend")

    auth_raw = _section(raw, "auth", {"provider", "ldap"})
    provider = str(auth_raw.get("provider", "local")).strip().lower()
    if provider not in {"local", "ldap"}:
        raise ConfigError("auth.provider must be 'local' or 'ldap'")
    ldap = auth_raw.get("ldap") or {}
    if not isinstance(ldap, dict):
        raise ConfigError("auth.ldap must be a mapping")

    admin_raw = _section(raw, "admin", {"user", "password", "allow_weak_bootstrap"})
    admin_user = admin_raw.get("user")
    admin_password = admin_raw.get("password")
    allow_weak = admin_raw.get("allow_weak_bootstrap", False)
    if not isinstance(allow_weak, bool):
        raise ConfigError("admin.allow_weak_bootstrap must be true or false")
    if (admin_user is None) != (admin_password is None):
        raise ConfigError("admin.user and admin.password must be configured together")
    if admin_user is not None and not isinstance(admin_user, str):
        raise ConfigError("admin.user must be a string")
    if admin_password is not None and not isinstance(admin_password, str):
        raise ConfigError("admin.password must be a string")
    if isinstance(admin_user, str) and not admin_user.strip():
        raise ConfigError("admin.user must be a non-empty string")
    if isinstance(admin_password, str) and not admin_password:
        raise ConfigError("admin.password must not be empty")
    if admin_password is not None and not allow_weak:
        from mmfdb.admin.backend.password_services import evaluate_password

        if evaluate_password(admin_password)["score"] < 4:
            raise ConfigError(
                "admin.password is weak; use a stronger bootstrap secret or explicitly set "
                "admin.allow_weak_bootstrap: true for a local-only development deployment"
            )

    client_config = _validate_client_config(
        raw=raw, deployment_mode=mode, default_port=port
    )

    return DeploymentConfig(
        version=1,
        mode=mode,
        server=ServerConfig(host=host.strip(), port=port),
        database=DatabaseConfig(path=database_path, url=database_url),
        object_store=ObjectStoreConfig(
            backend=backend,
            root=object_root,
            s3_bucket=s3_bucket,
            s3_prefix=str(object_raw.get("s3_prefix", "")),
            s3_endpoint_url=object_raw.get("s3_endpoint_url"),
            s3_region=object_raw.get("s3_region"),
        ),
        auth=AuthConfig(provider=provider, ldap=dict(ldap)),
        admin=AdminBootstrapConfig(
            user=admin_user,
            password=admin_password,
            allow_weak_bootstrap=allow_weak,
        ),
        client=client_config,
        source_path=selected.resolve(),
    )


def apply_deployment_config(config: DeploymentConfig) -> RuntimeConfig:
    """Replace process runtime settings with values from a validated YAML config."""
    reset_runtime_config()
    runtime = configure_runtime(
        database_path=config.database.path,
        database_url=config.database.url,
        object_store_root=config.object_store.root,
        object_store_backend=config.object_store.backend,
        s3_bucket=config.object_store.s3_bucket,
        s3_prefix=config.object_store.s3_prefix,
        s3_endpoint_url=config.object_store.s3_endpoint_url,
        s3_region=config.object_store.s3_region,
        default_user_id=config.admin.user,
    )
    auth_mapping: dict[str, Any] = {"auth_provider": config.auth.provider}
    if config.auth.ldap:
        auth_mapping["ldap"] = dict(config.auth.ldap)
    set_auth_config_resolver(lambda: auth_mapping)
    return runtime


_CONFIG = RuntimeConfig()

# Optional host-supplied callable resolving the default user id live. A host
# application (e.g. ChiSurf) whose own settings are the source of truth injects
# this so runtime changes propagate without MMFDB importing the host. MMFDB stays
# standalone: the resolver is host-agnostic and purely optional.
_DEFAULT_USER_ID_RESOLVER: Callable[[], str | None] | None = None


def set_default_user_id_resolver(resolver: Callable[[], str | None] | None) -> None:
    """Register (or clear) a live resolver for the default user id.

    Parameters
    ----------
    resolver : callable returning str or None, or None
        Called by :func:`configured_default_user_id` when no explicit
        ``default_user_id`` override is set on the runtime config. Pass ``None``
        to clear a previously registered resolver.

    """
    global _DEFAULT_USER_ID_RESOLVER
    _DEFAULT_USER_ID_RESOLVER = resolver


# Optional host-supplied callable resolving the active authentication config
# (provider selection + per-provider settings such as the LDAP block). Mirrors
# the default-user-id resolver so a host application can supply live auth config
# without MMFDB importing the host. See :func:`configured_auth_config`.
_AUTH_CONFIG_RESOLVER: Callable[[], dict | None] | None = None


def set_auth_config_resolver(resolver: Callable[[], dict | None] | None) -> None:
    """Register (or clear) a live resolver for the authentication config.

    The resolver returns a dict like ``{"auth_provider": "ldap", "ldap": {...}}``
    or ``None`` for the default (local). Pass ``None`` to clear it.
    """
    global _AUTH_CONFIG_RESOLVER
    _AUTH_CONFIG_RESOLVER = resolver


def _ldap_env_config() -> dict:
    """Build an LDAP config block from ``MMFDB_LDAP_*`` environment variables."""
    import json as _json

    def _split(name: str) -> list[str]:
        raw = os.environ.get(name, "")
        return [p for p in (s.strip() for s in raw.split(",")) if p]

    cfg: dict = {
        "host": os.environ.get("MMFDB_LDAP_HOST"),
        "base_dn": os.environ.get("MMFDB_LDAP_BASE_DN"),
        "bind_dn": os.environ.get("MMFDB_LDAP_BIND_DN"),
        "bind_password": os.environ.get("MMFDB_LDAP_BIND_PASSWORD"),
    }
    if os.environ.get("MMFDB_LDAP_PORT"):
        cfg["port"] = int(os.environ["MMFDB_LDAP_PORT"])
    if os.environ.get("MMFDB_LDAP_USE_SSL"):
        cfg["use_ssl"] = os.environ["MMFDB_LDAP_USE_SSL"].lower() in ("1", "true", "yes")
    for key, env in (
        ("user_filter", "MMFDB_LDAP_USER_FILTER"),
        ("uid_attr", "MMFDB_LDAP_UID_ATTR"),
        ("mail_attr", "MMFDB_LDAP_MAIL_ATTR"),
        ("name_attr", "MMFDB_LDAP_NAME_ATTR"),
        ("memberof_attr", "MMFDB_LDAP_MEMBEROF_ATTR"),
    ):
        if os.environ.get(env):
            cfg[key] = os.environ[env]
    if os.environ.get("MMFDB_LDAP_GROUP_MAP"):
        try:
            cfg["group_map"] = _json.loads(os.environ["MMFDB_LDAP_GROUP_MAP"])
        except ValueError:
            pass
    if _split("MMFDB_LDAP_ADMIN_GROUPS"):
        cfg["admin_groups"] = _split("MMFDB_LDAP_ADMIN_GROUPS")
    return {k: v for k, v in cfg.items() if v is not None}


def configured_auth_config() -> dict | None:
    """Return the active auth config: host resolver → ``MMFDB_AUTH_PROVIDER`` env → None.

    ``None`` means the default ``local`` provider. A returned dict carries at
    least ``auth_provider`` and, for LDAP, an ``ldap`` block.
    """
    if _AUTH_CONFIG_RESOLVER is not None:
        try:
            resolved = _AUTH_CONFIG_RESOLVER()
        except Exception:
            resolved = None
        if resolved:
            return resolved
    provider = os.environ.get("MMFDB_AUTH_PROVIDER")
    if provider:
        config: dict = {"auth_provider": provider}
        if provider.lower() == "ldap":
            config["ldap"] = _ldap_env_config()
        return config
    return None


def configure_runtime(**kwargs: object) -> RuntimeConfig:
    """Update the process-local MMFDB runtime configuration.

    Parameters
    ----------
    **kwargs
        Fields of :class:`RuntimeConfig` to update. Path-like values are
        normalized to :class:`~pathlib.Path`.

    Returns
    -------
    RuntimeConfig
        The updated runtime configuration.

    """
    global _CONFIG
    normalized: dict[str, object] = {}
    path_fields = {
        "settings_dir",
        "database_path",
        "source_database_path",
        "object_store_root",
    }
    for key, value in kwargs.items():
        if key in path_fields and value is not None:
            if not isinstance(value, (str, os.PathLike)):
                raise TypeError(f"{key} must be a path-like value")
            normalized[key] = Path(value).expanduser()
        elif value is not None:
            normalized[key] = value
    _CONFIG = replace(_CONFIG, **normalized)  # type: ignore[arg-type]
    return _CONFIG


def reset_runtime_config() -> RuntimeConfig:
    """Reset process-local runtime configuration and host-supplied resolvers."""
    global _AUTH_CONFIG_RESOLVER, _CONFIG, _DEFAULT_USER_ID_RESOLVER
    _CONFIG = RuntimeConfig()
    _DEFAULT_USER_ID_RESOLVER = None
    _AUTH_CONFIG_RESOLVER = None
    return _CONFIG


def get_runtime_config() -> RuntimeConfig:
    """Return the process-local runtime configuration."""
    return _CONFIG


def configured_settings_dir() -> Path:
    """Return the configured settings directory."""
    if _CONFIG.settings_dir is not None:
        return _CONFIG.settings_dir
    env_value = os.environ.get("MMFDB_SETTINGS_DIR")
    if env_value:
        return Path(os.path.expandvars(os.path.expanduser(env_value)))
    return Path.home() / ".chisurf"


def configured_database_path() -> Path | None:
    """Return an explicitly configured database path, if any."""
    if _CONFIG.database_path is not None:
        return _CONFIG.database_path
    env_value = os.environ.get("MMFDB_DATABASE_PATH")
    if env_value:
        return Path(os.path.expandvars(os.path.expanduser(env_value)))
    return None


def configured_database_url() -> str | None:
    """Return the explicitly configured server SQL URL, if any.

    URLs remain strings: treating them as :class:`Path` values can silently
    turn ``postgresql://...`` into a local SQLite filename.
    """
    if _CONFIG.database_url:
        return _CONFIG.database_url
    value = os.environ.get("MMFDB_DATABASE_URL")
    return value.strip() if value and value.strip() else None


def configured_source_database_path() -> Path | None:
    """Return an explicitly configured source database path, if any."""
    if _CONFIG.source_database_path is not None:
        return _CONFIG.source_database_path
    env_value = os.environ.get("MMFDB_SOURCE_DATABASE_PATH")
    if env_value:
        return Path(os.path.expandvars(os.path.expanduser(env_value)))
    return None


def configured_object_store_root() -> Path | None:
    """Return an explicitly configured object-store root, if any."""
    if _CONFIG.object_store_root is not None:
        return _CONFIG.object_store_root
    env_value = os.environ.get("MMFDB_OBJECT_STORE_ROOT")
    if env_value:
        return Path(os.path.expandvars(os.path.expanduser(env_value)))
    return None


def configured_object_store_backend() -> str:
    """Return the configured blob backend name (``local`` by default)."""
    return (
        (_CONFIG.object_store_backend or os.environ.get("MMFDB_OBJECT_STORE_BACKEND") or "local")
        .strip()
        .lower()
    )


def configured_s3_bucket() -> str | None:
    """Return the S3 bucket without ever handling credential material."""
    return _CONFIG.s3_bucket or os.environ.get("MMFDB_S3_BUCKET")


def configured_s3_prefix() -> str:
    """Return the optional S3 object-key prefix."""
    return (_CONFIG.s3_prefix or os.environ.get("MMFDB_S3_PREFIX") or "").strip()


def configured_s3_endpoint_url() -> str | None:
    """Return an optional AWS/S3-compatible endpoint URL."""
    return _CONFIG.s3_endpoint_url or os.environ.get("MMFDB_S3_ENDPOINT_URL")


def configured_s3_region() -> str | None:
    """Return the optional S3 region name."""
    return _CONFIG.s3_region or os.environ.get("MMFDB_S3_REGION")


def configured_default_user_id() -> str:
    """Return the configured default user id.

    Resolution order: an explicit ``default_user_id`` on the runtime config, then
    a host-registered live resolver (see :func:`set_default_user_id_resolver`),
    then ``$MMFDB_DEFAULT_USER_ID``, then ``"user_default"``.
    """
    if _CONFIG.default_user_id:
        return _CONFIG.default_user_id
    if _DEFAULT_USER_ID_RESOLVER is not None:
        try:
            resolved = _DEFAULT_USER_ID_RESOLVER()
        except Exception:
            resolved = None
        if resolved:
            return resolved
    env_value = os.environ.get("MMFDB_DEFAULT_USER_ID")
    return env_value or "user_default"
