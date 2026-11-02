from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterable


ROLE_LABELS = {
    "responsible": "Responsável",
    "creator": "Criador",
    "accomplice": "Participante",
    "auditor": "Observador",
}

ROLE_FIELDS = {
    "responsible": ("responsibleId", "RESPONSIBLE_ID", "responsible"),
    "creator": ("createdBy", "CREATED_BY", "creator"),
    "accomplice": ("accomplices", "ACCOMPLICES", "accomplicesData"),
    "auditor": ("auditors", "AUDITORS", "auditorsData"),
}

VERIFICATION_RANK = {"contradictory": 0, "unverifiable": 1, "confirmed": 2}


def _normalized(name: Any) -> str:
    return "".join(character for character in str(name).casefold() if character.isalnum())


def _extract_ids(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, int)) and not isinstance(value, bool):
        text = str(value).strip()
        return (text,) if text else ()
    if isinstance(value, (list, tuple, set)):
        output: list[str] = []
        for item in value:
            output.extend(_extract_ids(item))
        return tuple(dict.fromkeys(output))
    if isinstance(value, dict):
        for key in ("id", "ID"):
            if key in value:
                return _extract_ids(value[key])
        output: list[str] = []
        for key, item in value.items():
            if str(key).isdigit():
                output.append(str(key))
            output.extend(_extract_ids(item))
        return tuple(dict.fromkeys(output))
    return ()


def field_ids(raw: dict[str, Any], role: str) -> tuple[bool, tuple[str, ...], str]:
    normalized = {_normalized(key): (str(key), value) for key, value in raw.items()}
    for alias in ROLE_FIELDS[role]:
        found = normalized.get(_normalized(alias))
        if found is not None:
            source_name, value = found
            return True, _extract_ids(value), source_name
    return False, (), ROLE_FIELDS[role][0]


@dataclass(frozen=True)
class RoleEvidence:
    task_id: str
    user_id: str
    role: str
    verification: str
    evidence_field: str
    observed_user_ids: tuple[str, ...]
    reason: str

    def to_link(self, *, source_unit: int | None, query_filter: str | None) -> dict[str, Any]:
        value = asdict(self)
        value["observed_user_ids"] = list(self.observed_user_ids)
        value["source_unit"] = source_unit
        value["query_filter"] = query_filter
        return value


def verify_role(raw: dict[str, Any], user_id: str, role: str) -> RoleEvidence:
    task_id = str(raw.get("id", raw.get("ID", ""))).strip()
    present, observed, field_name = field_ids(raw, role)
    if not present:
        return RoleEvidence(
            task_id, user_id, role, "unverifiable", field_name, (),
            f"Campo {field_name} ausente no retorno; vínculo não verificável.",
        )
    if user_id in observed:
        return RoleEvidence(
            task_id, user_id, role, "confirmed", field_name, observed,
            f"Usuário confirmado no campo {field_name}.",
        )
    return RoleEvidence(
        task_id, user_id, role, "contradictory", field_name, observed,
        f"Filtro retornou a tarefa, mas o usuário não consta no campo {field_name}.",
    )


def confirmed_links_from_fields(raw: dict[str, Any], *, source_unit: int | None = None) -> list[dict[str, Any]]:
    task_id = str(raw.get("id", raw.get("ID", ""))).strip()
    links: list[dict[str, Any]] = []
    for role in ROLE_FIELDS:
        present, user_ids, field_name = field_ids(raw, role)
        if not present:
            continue
        for user_id in user_ids:
            links.append(RoleEvidence(
                task_id, user_id, role, "confirmed", field_name, user_ids,
                f"Usuário identificado diretamente no campo {field_name}.",
            ).to_link(source_unit=source_unit, query_filter=None))
    return links


def merge_links(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[tuple[str, str, str], dict[str, Any]] = {}
    for record in records:
        key = (str(record.get("task_id", "")), str(record.get("user_id", "")), str(record.get("role", "")))
        if not all(key):
            continue
        candidate = dict(record)
        candidate.setdefault("verification", "unverifiable")
        candidate.setdefault("reason", "Vínculo legado sem evidência persistida.")
        candidate_sources = list(candidate.get("source_units", []))
        if candidate.get("source_unit") is not None:
            candidate_sources.append(candidate["source_unit"])
        candidate_filters = list(candidate.get("query_filters", []))
        if candidate.get("query_filter"):
            candidate_filters.append(candidate["query_filter"])
        candidate["source_units"] = list(dict.fromkeys(candidate_sources))
        candidate["query_filters"] = list(dict.fromkeys(candidate_filters))
        current = merged.get(key)
        if current is None:
            merged[key] = candidate
            continue
        all_sources = list(dict.fromkeys([*current.get("source_units", []), *candidate["source_units"]]))
        all_filters = list(dict.fromkeys([*current.get("query_filters", []), *candidate["query_filters"]]))
        if VERIFICATION_RANK.get(candidate["verification"], -1) > VERIFICATION_RANK.get(current.get("verification"), -1):
            merged[key] = candidate
        merged[key]["source_units"] = all_sources
        merged[key]["query_filters"] = all_filters
    return list(merged.values())
