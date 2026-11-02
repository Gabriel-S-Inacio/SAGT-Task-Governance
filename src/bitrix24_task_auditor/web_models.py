from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable

from .config import FIELD_ID_RE, ROLE_NAMES


class QueryValidationError(ValueError):
    pass


STANDARD_FIELDS: dict[str, str] = {
    "ID": "ID", "TITLE": "Título", "STATUS": "Status", "PRIORITY": "Prioridade",
    "RESPONSIBLE_ID": "Responsável", "CREATED_BY": "Criador", "ACCOMPLICES": "Participantes",
    "AUDITORS": "Observadores", "CREATED_DATE": "Data de criação", "CHANGED_DATE": "Última alteração",
    "DEADLINE": "Prazo", "CLOSED_DATE": "Data de conclusão", "GROUP_ID": "Grupo/projeto",
    "DESCRIPTION": "Descrição", "TAGS": "Tags",
}
CORE_FIELDS = (
    "ID", "TITLE", "STATUS", "PRIORITY", "RESPONSIBLE_ID", "CREATED_BY", "ACCOMPLICES",
    "AUDITORS", "CREATED_DATE", "CHANGED_DATE", "DEADLINE", "CLOSED_DATE", "GROUP_ID",
)
ROLE_FILTERS = {"responsible": "RESPONSIBLE_ID", "creator": "CREATED_BY", "accomplice": "ACCOMPLICE", "auditor": "AUDITOR"}
STATUS_LABELS = {"1": "Nova", "2": "Pendente", "3": "Em andamento", "4": "Aguardando controle", "5": "Concluída", "6": "Adiada", "7": "Recusada"}
PRIORITY_LABELS = {"0": "Baixa", "1": "Normal", "2": "Alta"}
REPORT_NAME_RE = re.compile(r"^[A-Za-z0-9À-ÿ][A-Za-z0-9À-ÿ_.-]{0,79}$")


def _unique_strings(values: Iterable[Any]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))


@dataclass(frozen=True)
class QuerySpec:
    user_ids: tuple[str, ...] = ()
    roles: tuple[str, ...] = ()
    group_ids: tuple[str, ...] = ()
    statuses: tuple[str, ...] = ()
    priorities: tuple[str, ...] = ()
    deadline_from: str | None = None
    deadline_to: str | None = None
    standard_fields: tuple[str, ...] = CORE_FIELDS
    custom_fields: tuple[str, ...] = ()
    report_name: str = "auditoria"
    output_directory: str = "output"

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "QuerySpec":
        if not isinstance(raw, dict):
            raise QueryValidationError("modelo de auditoria inválido")
        return cls(
            user_ids=_unique_strings(raw.get("user_ids", [])), roles=_unique_strings(raw.get("roles", [])),
            group_ids=_unique_strings(raw.get("group_ids", [])), statuses=_unique_strings(raw.get("statuses", [])),
            priorities=_unique_strings(raw.get("priorities", [])), deadline_from=raw.get("deadline_from") or None,
            deadline_to=raw.get("deadline_to") or None,
            standard_fields=_unique_strings(raw.get("standard_fields", CORE_FIELDS)),
            custom_fields=_unique_strings(raw.get("custom_fields", [])),
            report_name=str(raw.get("report_name", "auditoria")).strip(),
            output_directory=str(raw.get("output_directory", "output")).strip(),
        )

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        for key in ("user_ids", "roles", "group_ids", "statuses", "priorities", "standard_fields", "custom_fields"):
            value[key] = list(value[key])
        return value

    def validate(self, *, confirmed_custom_fields: Iterable[str] = (), project_root: Path | None = None) -> None:
        if any(not item.isdigit() or int(item) <= 0 for item in self.user_ids):
            raise QueryValidationError("IDs de colaboradores devem ser números inteiros positivos")
        if len(self.user_ids) > 100:
            raise QueryValidationError("selecione no máximo 100 colaboradores por auditoria")
        if any(not item.isdigit() or int(item) <= 0 for item in self.group_ids):
            raise QueryValidationError("IDs de grupos/projetos devem ser números inteiros positivos")
        if set(self.roles) - set(ROLE_NAMES):
            raise QueryValidationError("a seleção contém um papel não suportado")
        if self.user_ids and not self.roles:
            raise QueryValidationError("selecione ao menos um papel para os colaboradores informados")
        if set(self.statuses) - set(STATUS_LABELS):
            raise QueryValidationError("a seleção contém um status não suportado")
        if set(self.priorities) - set(PRIORITY_LABELS):
            raise QueryValidationError("a seleção contém uma prioridade não suportada")
        unknown = set(self.standard_fields) - set(STANDARD_FIELDS)
        if unknown:
            raise QueryValidationError(f"campos padrão não permitidos: {', '.join(sorted(unknown))}")
        confirmed = {item.upper() for item in confirmed_custom_fields}
        for field_id in self.custom_fields:
            if not FIELD_ID_RE.fullmatch(field_id) or field_id.upper() not in confirmed:
                raise QueryValidationError(f"campo personalizado não confirmado: {field_id}")
        start, end = self._date(self.deadline_from, "prazo inicial"), self._date(self.deadline_to, "prazo final")
        if start and end and start > end:
            raise QueryValidationError("o prazo inicial deve ser anterior ao prazo final")
        if not REPORT_NAME_RE.fullmatch(self.report_name):
            raise QueryValidationError("nome do relatório inválido; use letras, números, ponto, '_' ou '-'")
        output = Path(self.output_directory)
        if output.is_absolute() or not self.output_directory or ".." in output.parts:
            raise QueryValidationError("a pasta de saída deve ser relativa e permanecer dentro do projeto")
        if project_root:
            try:
                (project_root / output).resolve().relative_to(project_root.resolve())
            except ValueError as exc:
                raise QueryValidationError("a pasta de saída aponta para fora do projeto") from exc

    @staticmethod
    def _date(value: str | None, label: str) -> date | None:
        if value is None:
            return None
        try:
            return date.fromisoformat(value)
        except (TypeError, ValueError) as exc:
            raise QueryValidationError(f"{label} deve estar no formato AAAA-MM-DD") from exc


@dataclass(frozen=True)
class QueryUnit:
    user_id: str | None
    role: str | None

    @property
    def label(self) -> str:
        return f"Usuário {self.user_id} · {self.role}" if self.user_id and self.role else "Consulta independente de usuário"


def build_query_units(spec: QuerySpec) -> list[QueryUnit]:
    return [QueryUnit(None, None)] if not spec.user_ids else [QueryUnit(user, role) for user in spec.user_ids for role in spec.roles]


def build_task_params(spec: QuerySpec, unit: QueryUnit) -> dict[str, Any]:
    filters: dict[str, Any] = {}
    if unit.user_id and unit.role:
        filters[ROLE_FILTERS[unit.role]] = unit.user_id
    if spec.group_ids:
        filters["GROUP_ID"] = list(spec.group_ids)
    if spec.statuses:
        filters["STATUS"] = list(spec.statuses)
    if spec.priorities:
        filters["PRIORITY"] = list(spec.priorities)
    if spec.deadline_from:
        filters[">=DEADLINE"] = f"{spec.deadline_from}T00:00:00"
    if spec.deadline_to:
        filters["<=DEADLINE"] = f"{spec.deadline_to}T23:59:59"
    select = list(dict.fromkeys([*CORE_FIELDS, *spec.standard_fields, *spec.custom_fields]))
    return {"order": {"ID": "ASC"}, "filter": filters, "select": select}


def query_summary(spec: QuerySpec) -> dict[str, Any]:
    return {
        "usuarios": list(spec.user_ids) or ["Todos no escopo dos filtros"],
        "papeis": list(spec.roles) or ["Não aplicável"],
        "grupos": list(spec.group_ids) or ["Todos"],
        "status": [STATUS_LABELS[item] for item in spec.statuses] or ["Todos"],
        "prioridades": [PRIORITY_LABELS[item] for item in spec.priorities] or ["Todas"],
        "prazo": {"de": spec.deadline_from, "ate": spec.deadline_to},
        "campos": [STANDARD_FIELDS[item] for item in spec.standard_fields],
        "campos_personalizados": list(spec.custom_fields),
        "consultas_planejadas": len(build_query_units(spec)), "paginacao": "Automática até ausência de next",
        "pasta_saida": spec.output_directory, "relatorio": spec.report_name,
    }
