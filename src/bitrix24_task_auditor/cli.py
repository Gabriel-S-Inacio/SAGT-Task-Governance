from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from . import __version__
from .checkpoint import CheckpointError, CheckpointStore
from .client import BitrixClient, BitrixError
from .config import ConfigurationError, load_config, webhook_from_environment
from .models import Task, User
from .service import collect, write_reports


PREFIX_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bitrix24-task-auditor",
        description="SAGT — auditoria read-only e governança de vínculos de tarefas do Bitrix24",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--config", default="config/auditor.toml", help="arquivo TOML relativo à raiz do projeto")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("validate-config", help="valida a configuração sem acessar a rede")
    reset = sub.add_parser("reset-checkpoint", help="remove somente o checkpoint e spool configurados")
    reset.add_argument("--confirm", action="store_true", help="confirma a remoção")
    run = sub.add_parser("run", help="coleta, audita e exporta (requer confirmação explícita)")
    run.add_argument("--confirm-read-only-collection", action="store_true")
    run.add_argument("--incremental", action="store_true", help="usa a marca temporal do último processamento")
    run.add_argument("--output-prefix", default=None)
    offline = sub.add_parser("audit-file", help="audita um JSON local sem acessar a rede")
    offline.add_argument("input", help="JSON relativo contendo tasks e opcionalmente users")
    offline.add_argument("--output-prefix", default=None)
    return parser


def _relative_project_path(value: str, root: Path) -> Path:
    path = Path(value)
    if path.is_absolute():
        raise ConfigurationError("o caminho deve ser relativo à raiz do projeto")
    resolved = (root / path).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ConfigurationError("o caminho aponta para fora da raiz do projeto") from exc
    return resolved


def _prefix(value: str | None) -> str:
    candidate = value or datetime.now().strftime("auditoria_%Y%m%d_%H%M%S")
    if not PREFIX_RE.fullmatch(candidate):
        raise ConfigurationError("prefixo de saída inválido; use letras, números, '_' ou '-'")
    return candidate


def _offline_records(path: Path, field_ids: tuple[str, ...]) -> tuple[list[Task], list[User]]:
    try:
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError(f"não foi possível ler o JSON local: {exc}") from exc
    if isinstance(raw, list):
        raw = {"tasks": raw, "users": []}
    if not isinstance(raw, dict) or not isinstance(raw.get("tasks"), list) or not isinstance(raw.get("users", []), list):
        raise ConfigurationError("JSON local deve conter listas 'tasks' e, opcionalmente, 'users'")
    if any(not isinstance(item, dict) for item in [*raw["tasks"], *raw.get("users", [])]):
        raise ConfigurationError("tasks/users deve conter apenas objetos")
    return (
        [Task.from_api(item, field_ids) for item in raw["tasks"]],
        [User.from_api(item) for item in raw.get("users", [])],
    )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = Path.cwd().resolve()
    try:
        config_path = _relative_project_path(args.config, root)
        config = load_config(config_path, project_root=root)
        store = CheckpointStore(config.audit.checkpoint_path, config.audit.spool_path)
        if args.command == "validate-config":
            print(f"Configuração válida: {config_path.relative_to(root)}")
            print("Nenhuma consulta de rede foi executada.")
            return 0
        if args.command == "reset-checkpoint":
            if not args.confirm:
                raise ConfigurationError("use --confirm para remover checkpoint e spool")
            store.reset()
            print("Checkpoint e spool removidos.")
            return 0
        if args.command == "audit-file":
            input_path = _relative_project_path(args.input, root)
            tasks, users = _offline_records(input_path, tuple(item.id for item in config.custom_fields))
            json_path, xlsx_path, _ = write_reports(
                config, tasks, users, prefix=_prefix(args.output_prefix), source=f"local:{input_path.relative_to(root)}"
            )
            print(f"JSON: {json_path.relative_to(root)}")
            print(f"Excel: {xlsx_path.relative_to(root)}")
            print("Nenhuma consulta de rede foi executada.")
            return 0
        if args.command == "run":
            if not args.confirm_read_only_collection:
                raise ConfigurationError("a coleta não foi iniciada; use --confirm-read-only-collection após revisar a configuração")
            webhook = webhook_from_environment(config.bitrix)
            client = BitrixClient(webhook, config.bitrix)
            tasks, users = collect(client, config, store, incremental=args.incremental)
            json_path, xlsx_path, watermark = write_reports(
                config, tasks, users, prefix=_prefix(args.output_prefix), source="bitrix24"
            )
            store.complete(watermark)
            print(f"Tarefas processadas: {len(tasks)}")
            print(f"JSON: {json_path.relative_to(root)}")
            print(f"Excel: {xlsx_path.relative_to(root)}")
            return 0
        raise AssertionError("comando não tratado")
    except (ConfigurationError, CheckpointError, BitrixError) as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
