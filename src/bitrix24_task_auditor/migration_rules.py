from __future__ import annotations

import json
import re
import uuid
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from .migration_models import utc_now
from .migration_store import MigrationStore


RULE_OPERATIONS = {
    "REPLACE_RESPONSIBLE": {"label": "Substituir responsável", "role": "responsible", "field": "RESPONSIBLE_ID", "kind": "REPLACE"},
    "REPLACE_ACCOMPLICE": {"label": "Substituir participante", "role": "accomplice", "field": "ACCOMPLICES", "kind": "REPLACE"},
    "REPLACE_AUDITOR": {"label": "Substituir observador", "role": "auditor", "field": "AUDITORS", "kind": "REPLACE"},
    "ADD_ACCOMPLICE": {"label": "Adicionar participante", "role": "accomplice", "field": "ACCOMPLICES", "kind": "ADD"},
    "ADD_AUDITOR": {"label": "Adicionar observador", "role": "auditor", "field": "AUDITORS", "kind": "ADD"},
}
CONDITION_TYPES = {
    "NONE": "Sem condição adicional",
    "GROUP_ID": "Grupo/projeto (GROUP_ID)",
    "CREATED_BY": "Criador da tarefa (CREATED_BY)",
    "CUSTOM_FIELD": "Campo personalizado suportado",
}


class MigrationRuleError(ValueError):
    pass


def _positive_id(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not text.isdigit() or int(text) <= 0:
        raise MigrationRuleError(f"O ID de {label} deve ser numérico e positivo.")
    return str(int(text))


@dataclass(frozen=True)
class MigrationRule:
    rule_id: str
    version: int
    name: str
    description: str
    source_user_id: str
    operation: str
    target_user_id: str
    condition_type: str
    condition_field_id: str | None
    condition_value: str | None
    active: bool
    created_at: str
    updated_at: str
    cleanup_previous_responsible_auditor: bool = False

    def snapshot(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "MigrationRule":
        raw = dict(value)
        raw.setdefault("cleanup_previous_responsible_auditor", False)
        return cls(**raw)


def validate_rule(rule: MigrationRule) -> MigrationRule:
    name = str(rule.name or "").strip()
    if not name:
        raise MigrationRuleError("Informe o nome da regra.")
    operation = str(rule.operation or "").strip().upper()
    if operation not in RULE_OPERATIONS:
        raise MigrationRuleError("Selecione uma operação suportada.")
    source = _positive_id(rule.source_user_id, "usuário de origem")
    target = _positive_id(rule.target_user_id, "usuário de destino")
    if source == target:
        raise MigrationRuleError("Origem e destino devem ser diferentes.")
    condition_type = str(rule.condition_type or "NONE").strip().upper()
    if condition_type not in CONDITION_TYPES:
        raise MigrationRuleError("Selecione uma condição suportada.")
    field_id: str | None = None
    value: str | None = None
    if condition_type != "NONE":
        value = _positive_id(rule.condition_value, "valor da condição")
        if condition_type == "GROUP_ID":
            field_id = "GROUP_ID"
        elif condition_type == "CREATED_BY":
            field_id = "CREATED_BY"
        else:
            field_id = str(rule.condition_field_id or "").strip().upper()
            if not re.fullmatch(r"UF_[A-Z0-9_]+", field_id):
                raise MigrationRuleError("O campo personalizado deve possuir identificador UF_* explícito.")
    return replace(
        rule,
        name=name,
        description=str(rule.description or "").strip(),
        source_user_id=source,
        operation=operation,
        target_user_id=target,
        condition_type=condition_type,
        condition_field_id=field_id,
        condition_value=value,
        active=bool(rule.active),
        cleanup_previous_responsible_auditor=False,
    )


class MigrationRuleStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.directory = self.root / ".state" / "migration_rules"
        self.path = self.directory / "catalog.json"
        self.lock_path = self.directory / "catalog.lock"
        self._migration_store = MigrationStore(root)

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {"schema_version": 1, "rules": {}, "templates": {}, "usage": []}

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._empty()
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise MigrationRuleError(f"Não foi possível ler o catálogo de regras: {exc}") from exc
        if not isinstance(value, dict) or not isinstance(value.get("rules"), dict):
            raise MigrationRuleError("Catálogo de regras inválido.")
        value.setdefault("templates", {})
        value.setdefault("usage", [])
        return value

    def _write(self, value: dict[str, Any]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        MigrationStore._atomic_json(self.path, value)

    def list_rules(self, *, active_only: bool = False) -> list[MigrationRule]:
        output = []
        for entry in self._read()["rules"].values():
            current = entry.get("current") if isinstance(entry, dict) else None
            if not isinstance(current, dict):
                continue
            rule = MigrationRule.from_dict(current)
            if not active_only or rule.active:
                output.append(rule)
        return sorted(output, key=lambda item: (item.name.casefold(), item.rule_id))

    def get(self, rule_id: str) -> MigrationRule:
        entry = self._read()["rules"].get(rule_id)
        if not isinstance(entry, dict) or not isinstance(entry.get("current"), dict):
            raise MigrationRuleError("Regra não encontrada.")
        return MigrationRule.from_dict(entry["current"])

    def create(
        self,
        *,
        name: str,
        description: str,
        source_user_id: str,
        operation: str,
        target_user_id: str,
        condition_type: str = "NONE",
        condition_field_id: str | None = None,
        condition_value: str | None = None,
        active: bool = True,
        cleanup_previous_responsible_auditor: bool = False,
    ) -> MigrationRule:
        now = utc_now()
        rule = validate_rule(MigrationRule(
            rule_id="rule_" + uuid.uuid4().hex,
            version=1,
            name=name,
            description=description,
            source_user_id=source_user_id,
            operation=operation,
            target_user_id=target_user_id,
            condition_type=condition_type,
            condition_field_id=condition_field_id,
            condition_value=condition_value,
            active=active,
            created_at=now,
            updated_at=now,
            cleanup_previous_responsible_auditor=cleanup_previous_responsible_auditor,
        ))
        self.directory.mkdir(parents=True, exist_ok=True)
        with self._migration_store._file_lock(self.lock_path, timeout=10.0):
            catalog = self._read()
            catalog["rules"][rule.rule_id] = {"current": rule.snapshot(), "revisions": []}
            self._write(catalog)
        return rule

    def edit(self, rule_id: str, **changes: Any) -> MigrationRule:
        self.directory.mkdir(parents=True, exist_ok=True)
        with self._migration_store._file_lock(self.lock_path, timeout=10.0):
            catalog = self._read()
            entry = catalog["rules"].get(rule_id)
            if not isinstance(entry, dict) or not isinstance(entry.get("current"), dict):
                raise MigrationRuleError("Regra não encontrada.")
            current = MigrationRule.from_dict(entry["current"])
            allowed = {
                "name", "description", "source_user_id", "operation", "target_user_id",
                "condition_type", "condition_field_id", "condition_value", "active",
                "cleanup_previous_responsible_auditor",
            }
            unknown = set(changes) - allowed
            if unknown:
                raise MigrationRuleError(f"Campos de regra não editáveis: {', '.join(sorted(unknown))}.")
            updated = validate_rule(replace(
                current,
                **changes,
                version=current.version + 1,
                updated_at=utc_now(),
            ))
            revisions = entry.setdefault("revisions", [])
            revisions.append(current.snapshot())
            entry["current"] = updated.snapshot()
            self._write(catalog)
        return updated

    def duplicate(self, rule_id: str) -> MigrationRule:
        current = self.get(rule_id)
        return self.create(
            name=f"{current.name} (cópia)",
            description=current.description,
            source_user_id=current.source_user_id,
            operation=current.operation,
            target_user_id=current.target_user_id,
            condition_type=current.condition_type,
            condition_field_id=current.condition_field_id,
            condition_value=current.condition_value,
            active=current.active,
            cleanup_previous_responsible_auditor=current.cleanup_previous_responsible_auditor,
        )

    def set_active(self, rule_id: str, active: bool) -> MigrationRule:
        return self.edit(rule_id, active=active)

    def delete(self, rule_id: str) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        with self._migration_store._file_lock(self.lock_path, timeout=10.0):
            catalog = self._read()
            if rule_id not in catalog["rules"]:
                raise MigrationRuleError("Regra não encontrada.")
            if any(item.get("rule_id") == rule_id for item in catalog.get("usage", [])):
                raise MigrationRuleError("A regra já foi utilizada e não pode ser excluída; desative-a.")
            if any(rule_id in item.get("rule_ids", []) for item in catalog.get("templates", {}).values()):
                raise MigrationRuleError("A regra pertence a um modelo e não pode ser excluída.")
            del catalog["rules"][rule_id]
            self._write(catalog)

    def mark_used(self, rules: list[MigrationRule], plan_id: str) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        with self._migration_store._file_lock(self.lock_path, timeout=10.0):
            catalog = self._read()
            for rule in rules:
                marker = {"rule_id": rule.rule_id, "version": rule.version, "plan_id": plan_id}
                if any(all(item.get(key) == value for key, value in marker.items()) for item in catalog["usage"]):
                    continue
                catalog["usage"].append({**marker, "used_at": utc_now()})
            self._write(catalog)

    def save_template(self, name: str, rule_ids: list[str]) -> dict[str, Any]:
        label = str(name or "").strip()
        unique_ids = list(dict.fromkeys(rule_ids))
        if not label or not unique_ids:
            raise MigrationRuleError("Informe o nome do modelo e selecione ao menos uma regra.")
        self.directory.mkdir(parents=True, exist_ok=True)
        with self._migration_store._file_lock(self.lock_path, timeout=10.0):
            catalog = self._read()
            missing = [rule_id for rule_id in unique_ids if rule_id not in catalog["rules"]]
            if missing:
                raise MigrationRuleError("O modelo contém regras inexistentes.")
            template_id = "template_" + uuid.uuid4().hex
            template = {
                "template_id": template_id,
                "name": label,
                "rule_ids": unique_ids,
                "created_at": utc_now(),
            }
            catalog["templates"][template_id] = template
            self._write(catalog)
        return template

    def list_templates(self) -> list[dict[str, Any]]:
        return sorted(self._read().get("templates", {}).values(), key=lambda item: item.get("name", "").casefold())
