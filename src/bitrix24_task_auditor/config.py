from __future__ import annotations

import ipaddress
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from dotenv import load_dotenv


class ConfigurationError(ValueError):
    """Raised when configuration is unsafe or invalid."""


ROLE_NAMES = ("responsible", "creator", "accomplice", "auditor")
FIELD_ID_RE = re.compile(r"^(UF_[A-Z0-9_]+|[A-Za-z][A-Za-z0-9_.]*)$", re.IGNORECASE)


@dataclass(frozen=True)
class BitrixConfig:
    webhook_env: str = "BITRIX24_WEBHOOK_URL"
    timeout_seconds: int = 30
    max_retries: int = 4
    allowed_hosts: tuple[str, ...] = ()


@dataclass(frozen=True)
class AuditConfig:
    timezone: str = "America/Sao_Paulo"
    include_closed: bool = True
    stale_after_hours: int = 168
    due_soon_hours: int = 24
    incremental_overlap_minutes: int = 5
    checkpoint_path: Path = Path(".state/checkpoint.json")
    spool_path: Path = Path(".state/tasks.ndjson")
    output_directory: Path = Path("output")


@dataclass(frozen=True)
class CustomFieldConfig:
    id: str
    label: str
    required_for: tuple[str, ...] = ()
    export: bool = True


@dataclass(frozen=True)
class AppConfig:
    bitrix: BitrixConfig
    audit: AuditConfig
    roles: dict[str, tuple[str, ...]] = field(default_factory=dict)
    custom_fields: tuple[CustomFieldConfig, ...] = ()


def load_project_environment(project_root: str | Path) -> Path:
    """Load the project-local .env without replacing process environment values."""
    root = Path(project_root).resolve()
    dotenv_path = root / ".env"
    if dotenv_path.is_file():
        load_dotenv(dotenv_path=dotenv_path, override=False, encoding="utf-8")
    return dotenv_path


def _positive_int(value: Any, name: str, *, minimum: int = 1, maximum: int = 10000) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ConfigurationError(f"{name} deve ser inteiro entre {minimum} e {maximum}")
    return value


def _boolean(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigurationError(f"{name} deve ser true ou false")
    return value


def _safe_path(root: Path, value: Any, name: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{name} deve ser um caminho relativo não vazio")
    candidate = Path(value)
    if candidate.is_absolute():
        raise ConfigurationError(f"{name} deve usar caminho relativo")
    resolved = (root / candidate).resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise ConfigurationError(f"{name} aponta para fora do diretório do projeto") from exc
    return resolved


def load_config(path: str | Path, *, project_root: str | Path | None = None) -> AppConfig:
    config_path = Path(path).resolve()
    root = Path(project_root or Path.cwd()).resolve()
    load_project_environment(root)
    try:
        raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigurationError(f"não foi possível ler a configuração: {exc}") from exc

    b = raw.get("bitrix", {})
    a = raw.get("audit", {})
    webhook_env = b.get("webhook_env", "BITRIX24_WEBHOOK_URL")
    if not isinstance(webhook_env, str) or not re.fullmatch(r"[A-Z_][A-Z0-9_]*", webhook_env):
        raise ConfigurationError("bitrix.webhook_env deve ser nome válido de variável de ambiente")
    allowed = b.get("allowed_hosts", [])
    if not isinstance(allowed, list) or not all(isinstance(x, str) and x.strip() for x in allowed):
        raise ConfigurationError("bitrix.allowed_hosts deve ser uma lista de hosts")
    bitrix = BitrixConfig(
        webhook_env=webhook_env,
        timeout_seconds=_positive_int(b.get("timeout_seconds", 30), "bitrix.timeout_seconds", maximum=300),
        max_retries=_positive_int(b.get("max_retries", 4), "bitrix.max_retries", minimum=0, maximum=10),
        allowed_hosts=tuple(x.lower().rstrip(".") for x in allowed),
    )
    timezone_name = a.get("timezone", "America/Sao_Paulo")
    if not isinstance(timezone_name, str):
        raise ConfigurationError("audit.timezone deve ser um nome de fuso horário")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_+.-]*(?:/[A-Za-z0-9_+.-]+)+", timezone_name):
        raise ConfigurationError("audit.timezone deve usar formato IANA, por exemplo America/Sao_Paulo")
    audit = AuditConfig(
        timezone=timezone_name,
        include_closed=_boolean(a.get("include_closed", True), "audit.include_closed"),
        stale_after_hours=_positive_int(a.get("stale_after_hours", 168), "audit.stale_after_hours"),
        due_soon_hours=_positive_int(a.get("due_soon_hours", 24), "audit.due_soon_hours"),
        incremental_overlap_minutes=_positive_int(
            a.get("incremental_overlap_minutes", 5), "audit.incremental_overlap_minutes", minimum=0, maximum=1440
        ),
        checkpoint_path=_safe_path(root, a.get("checkpoint_path", ".state/checkpoint.json"), "audit.checkpoint_path"),
        spool_path=_safe_path(root, a.get("spool_path", ".state/tasks.ndjson"), "audit.spool_path"),
        output_directory=_safe_path(root, a.get("output_directory", "output"), "audit.output_directory"),
    )

    roles_raw = raw.get("roles", {})
    if not isinstance(roles_raw, dict):
        raise ConfigurationError("roles deve ser uma tabela TOML")
    unknown_roles = set(roles_raw) - set(ROLE_NAMES)
    if unknown_roles:
        raise ConfigurationError(f"papéis desconhecidos: {', '.join(sorted(unknown_roles))}")
    roles: dict[str, tuple[str, ...]] = {}
    for name in ROLE_NAMES:
        values = roles_raw.get(name, [])
        if not isinstance(values, list) or any(not str(v).strip() for v in values):
            raise ConfigurationError(f"roles.{name} deve ser uma lista de IDs")
        roles[name] = tuple(dict.fromkeys(str(v).strip() for v in values))

    fields: list[CustomFieldConfig] = []
    seen: set[str] = set()
    raw_fields = raw.get("custom_fields", [])
    if not isinstance(raw_fields, list):
        raise ConfigurationError("custom_fields deve ser uma lista de tabelas")
    for index, item in enumerate(raw_fields):
        if not isinstance(item, dict):
            raise ConfigurationError(f"custom_fields[{index}] inválido")
        field_id = str(item.get("id", "")).strip()
        if not FIELD_ID_RE.fullmatch(field_id):
            raise ConfigurationError(f"ID de campo personalizado inválido: {field_id!r}")
        normalized = field_id.upper()
        if normalized in seen:
            raise ConfigurationError(f"campo personalizado duplicado: {field_id}")
        seen.add(normalized)
        required_for = item.get("required_for", [])
        if not isinstance(required_for, list) or set(required_for) - set(ROLE_NAMES):
            raise ConfigurationError(f"required_for inválido no campo {field_id}")
        fields.append(CustomFieldConfig(
            id=field_id,
            label=str(item.get("label") or field_id).strip(),
            required_for=tuple(required_for),
            export=_boolean(item.get("export", True), f"custom_fields[{index}].export"),
        ))
    return AppConfig(bitrix=bitrix, audit=audit, roles=roles, custom_fields=tuple(fields))


def webhook_from_environment(config: BitrixConfig) -> str:
    if not config.allowed_hosts:
        raise ConfigurationError(
            "bitrix.allowed_hosts deve conter o hostname explícito do portal antes de conexões reais"
        )
    value = os.environ.get(config.webhook_env, "").strip()
    if not value:
        raise ConfigurationError(f"variável {config.webhook_env} não definida")
    return validate_webhook(value, config.allowed_hosts)


def validate_webhook(value: str, allowed_hosts: tuple[str, ...] = ()) -> str:
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or not host or parsed.username or parsed.password:
        raise ConfigurationError("webhook deve ser uma URL HTTPS sem credenciais embutidas")
    if parsed.query or parsed.fragment:
        raise ConfigurationError("webhook não pode conter query string ou fragmento")
    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        address = None
    if address and (address.is_private or address.is_loopback or address.is_link_local or address.is_reserved):
        raise ConfigurationError("webhook não pode usar endereço IP privado ou reservado")
    if host == "localhost" or host.endswith(".localhost"):
        raise ConfigurationError("webhook localhost não é permitido")
    if allowed_hosts and host not in allowed_hosts:
        raise ConfigurationError("host do webhook não pertence a bitrix.allowed_hosts")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 3 or parts[0].lower() != "rest":
        raise ConfigurationError("webhook deve ter o formato https://host/rest/ID/TOKEN/")
    clean_path = "/" + "/".join(parts) + "/"
    return urlunsplit(("https", parsed.netloc, clean_path, "", ""))


def redact_secret(text: str, webhook: str | None) -> str:
    if not webhook:
        return text
    parsed = urlsplit(webhook)
    return text.replace(webhook, f"{parsed.scheme}://{parsed.netloc}/rest/***/***/")
