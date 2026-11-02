from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from .migration_importer import ImportedAudit, ImportedTask


ROLE_FIELDS = {
    "responsible": "RESPONSIBLE_ID",
    "accomplice": "ACCOMPLICES",
    "auditor": "AUDITORS",
    "creator": "CREATED_BY",
}
SUPPORTED_WRITE_ROLES = frozenset({"responsible", "accomplice", "auditor"})
ROLE_LABELS = {
    "responsible": "Responsável",
    "accomplice": "Participante",
    "auditor": "Observador",
    "creator": "Criador",
}
OPERATION_TYPES = frozenset({"REPLACE_USER", "ADD_USER"})
OPERATION_LABELS = {
    "REPLACE_USER": "Substituir usuário",
    "ADD_USER": "Adicionar usuário",
    "REMOVE_UNAUTHORIZED_AUDITOR": "Remover observador indevido comprovado",
}
MIGRATION_MODES = frozenset({"SIMPLE", "OWNER_RULES", "RULE_SET", "SIDE_EFFECT_CORRECTION"})


class MigrationValidationError(ValueError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_user_ids(source_user_id: str | int, target_user_id: str | int) -> tuple[str, str]:
    source = str(source_user_id).strip()
    target = str(target_user_id).strip()
    if not source.isdigit() or int(source) <= 0:
        raise MigrationValidationError("O ID do usuário de origem deve ser numérico e positivo.")
    if not target.isdigit() or int(target) <= 0:
        raise MigrationValidationError("O ID do usuário de destino deve ser numérico e positivo.")
    if int(source) == int(target):
        raise MigrationValidationError("Os usuários de origem e destino devem ser diferentes.")
    return str(int(source)), str(int(target))


def replace_member(values: tuple[str, ...], source: str, target: str) -> tuple[str, ...]:
    output: list[str] = []
    for item in values:
        replacement = target if item == source else item
        if replacement not in output:
            output.append(replacement)
    return tuple(output)


def role_value(task: ImportedTask, role: str) -> str | tuple[str, ...] | None:
    return {
        "responsible": task.responsible_id,
        "creator": task.created_by,
        "accomplice": task.accomplices,
        "auditor": task.auditors,
    }[role]


def proposed_value(task: ImportedTask, role: str, source: str, target: str, operation_type: str = "REPLACE_USER"):
    current = role_value(task, role)
    if operation_type == "ADD_USER":
        if role not in {"accomplice", "auditor"}:
            return current
        values = tuple(current or ())
        return values if target in values else (*values, target)
    if role in {"responsible", "creator"}:
        return target if current == source else current
    return replace_member(current or (), source, target)


@dataclass(frozen=True)
class PreviewRow:
    task_id: str
    title: str
    role: str
    current_user: str
    target_user: str
    previous_value: Any
    proposed_value: Any
    eligibility: str
    blocking_reason: str = ""
    operation_type: str = "REPLACE_USER"
    source_disposition: str = "REMOVIDO"
    audited_value: Any = None
    rule_id: str = ""
    owner_field_id: str = ""
    owner_label: str = ""
    audited_owner_value: Any = None
    current_owner_value: Any = None
    rule_name: str = ""
    condition_field_id: str = ""
    condition_value: Any = None
    current_condition_value: Any = None
    role_disposition: str = ""
    current_accomplices: list[str] = field(default_factory=list)
    current_auditors: list[str] = field(default_factory=list)
    current_creator: str | None = None
    source_other_roles: list[str] = field(default_factory=list)
    preservation_policy: str = ""
    side_effect_correction_authorized: bool = False


@dataclass(frozen=True)
class OwnerRule:
    rule_id: str
    owner_label: str
    owner_id: str
    target_user_id: str


@dataclass
class PlannedOperation:
    task_id: str
    title: str
    roles: list[str]
    audited_changed_at: str | None
    before: dict[str, Any]
    patch: dict[str, Any]
    eligibility: str = "ELEGIVEL"
    blocking_reason: str = ""
    result: str = "PENDENTE"
    operation_type: str = "REPLACE_USER"
    target_user_id: str | None = None
    rule_id: str = ""
    owner_field_id: str = ""
    owner_label: str = ""
    owner_id: str = ""
    rule_bindings: list[dict[str, Any]] = field(default_factory=list)
    integrity_before: dict[str, Any] = field(default_factory=dict)
    authorized_side_effect_corrections: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class MigrationPlan:
    plan_id: str
    created_at: str
    spreadsheet_filename: str
    spreadsheet_sha256: str
    audit_run_id: str | None
    source_user_id: str | None
    target_user_id: str | None
    selected_roles: list[str]
    task_ids: list[str]
    operations: list[PlannedOperation]
    preview: list[PreviewRow]
    import_blockers: list[str] = field(default_factory=list)
    import_warnings: list[str] = field(default_factory=list)
    audit_task_count: int | None = None
    audit_link_count: int | None = None
    matched_link_count: int | None = None
    preflight_at: str | None = None
    target_user: dict[str, Any] | None = None
    digest: str = ""
    operation_type: str = "REPLACE_USER"
    logic_version: int = 1
    migration_mode: str = "SIMPLE"
    owner_field_id: str | None = None
    owner_field_label: str | None = None
    owner_rules: list[OwnerRule] = field(default_factory=list)
    target_users: dict[str, dict[str, Any]] = field(default_factory=dict)
    rule_task_limit: int | None = None
    rule_snapshots: list[dict[str, Any]] = field(default_factory=list)
    side_effect_policy: dict[str, Any] = field(default_factory=dict)
    correction_evidence: dict[str, Any] = field(default_factory=dict)

    def binding(self) -> dict[str, Any]:
        return {
            "spreadsheet_sha256": self.spreadsheet_sha256,
            "source_user_id": self.source_user_id,
            "target_user_id": self.target_user_id,
            "migration_mode": self.migration_mode,
            "owner_field_id": self.owner_field_id,
            "owner_field_label": self.owner_field_label,
            "owner_rules": [asdict(item) for item in self.owner_rules],
            "rule_task_limit": self.rule_task_limit,
            "rule_snapshots": self.rule_snapshots,
            "side_effect_policy": self.side_effect_policy,
            "correction_evidence": self.correction_evidence,
            "operation_type": self.operation_type,
            "logic_version": self.logic_version,
            "selected_roles": self.selected_roles,
            "task_ids": self.task_ids,
            "operations": [asdict(item) for item in self.operations],
            "preview": [asdict(item) for item in self.preview],
        }

    def refresh_digest(self) -> str:
        self.digest = canonical_digest(self.binding())
        return self.digest

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "MigrationPlan":
        raw = dict(value)
        raw["operations"] = [PlannedOperation(**item) for item in raw.get("operations", [])]
        raw["preview"] = [PreviewRow(**item) for item in raw.get("preview", [])]
        raw["owner_rules"] = [OwnerRule(**item) for item in raw.get("owner_rules", [])]
        return cls(**raw)


def create_plan(
    audit: ImportedAudit,
    source_user_id: str | int,
    target_user_id: str | int,
    selected_roles: list[str] | tuple[str, ...],
    operation_type: str = "REPLACE_USER",
    *,
    allow_responsible_auditor_cleanup: bool = False,
) -> MigrationPlan:
    source, target = validate_user_ids(source_user_id, target_user_id)
    roles = list(dict.fromkeys(str(item).strip().casefold() for item in selected_roles))
    operation_type = str(operation_type).strip().upper()
    if operation_type not in OPERATION_TYPES:
        raise MigrationValidationError("Selecione uma operação válida: substituir ou adicionar usuário.")
    if not roles or set(roles) - set(ROLE_FIELDS):
        raise MigrationValidationError("Selecione ao menos um papel de vínculo reconhecido.")
    if operation_type == "ADD_USER" and set(roles) - {"accomplice", "auditor"}:
        raise MigrationValidationError("Adicionar usuário é suportado somente para participante e observador.")
    preview: list[PreviewRow] = []
    by_task: dict[str, list[str]] = {}
    for link in audit.links:
        if link.user_id != source or link.role not in roles:
            continue
        task = audit.tasks.get(link.task_id)
        if task is None:
            continue
        state = "ELEGIVEL"
        reason = ""
        if audit.blockers:
            state, reason = "BLOQUEADO", "Arquivo de auditoria incompleto ou inconsistente."
        elif link.verification != "confirmed":
            state, reason = "DIVERGENTE", "O vínculo não foi confirmado pela auditoria."
        elif link.role == "creator":
            state, reason = "NAO_SUPORTADO", "CREATED_BY não possui edição documentada e suportada."
        before = role_value(task, link.role)
        proposed = proposed_value(task, link.role, source, target, operation_type)
        proposed_members = proposed if isinstance(proposed, tuple) else (proposed,) if proposed else ()
        preview.append(PreviewRow(
            task_id=task.task_id,
            title=task.title,
            role=link.role,
            current_user=source,
            target_user=target,
            previous_value=list(before) if isinstance(before, tuple) else before,
            proposed_value=list(proposed) if isinstance(proposed, tuple) else proposed,
            eligibility=state,
            blocking_reason=reason,
            operation_type=operation_type,
            source_disposition="MANTIDO" if source in proposed_members else "REMOVIDO",
            audited_value=list(before) if isinstance(before, tuple) else before,
            role_disposition="MANTIDO_NO_PAPEL" if operation_type == "ADD_USER" else "REMOVIDO_DO_PAPEL",
            current_accomplices=list(task.accomplices),
            current_auditors=list(task.auditors),
            current_creator=task.created_by,
            source_other_roles=[
                candidate for candidate, present in (
                    ("creator", task.created_by == source),
                    ("accomplice", source in task.accomplices),
                    ("auditor", source in task.auditors),
                ) if present and candidate != link.role
            ],
            preservation_policy="Somente os campos explicitamente aprovados podem mudar; os demais vínculos serão comparados após a escrita.",
            side_effect_correction_authorized=False,
        ))
        if state == "ELEGIVEL":
            by_task.setdefault(task.task_id, []).append(link.role)

    operations: list[PlannedOperation] = []
    for task_id, task_roles in sorted(by_task.items(), key=lambda item: int(item[0])):
        task = audit.tasks[task_id]
        before: dict[str, Any] = {}
        patch: dict[str, Any] = {}
        for role in dict.fromkeys(task_roles):
            field_name = ROLE_FIELDS[role]
            old = role_value(task, role)
            new = proposed_value(task, role, source, target, operation_type)
            before[field_name] = list(old) if isinstance(old, tuple) else old
            patch[field_name] = list(new) if isinstance(new, tuple) else new
        operations.append(PlannedOperation(
            task_id=task_id,
            title=task.title,
            roles=list(dict.fromkeys(task_roles)),
            audited_changed_at=task.changed_at,
            before=before,
            patch=patch,
            operation_type=operation_type,
            target_user_id=target,
            authorized_side_effect_corrections={},
        ))
    if not preview:
        raise MigrationValidationError("Nenhum vínculo do usuário de origem foi encontrado para os papéis selecionados.")
    plan = MigrationPlan(
        plan_id=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "_" + uuid.uuid4().hex[:8],
        created_at=utc_now(),
        spreadsheet_filename=audit.filename,
        spreadsheet_sha256=audit.sha256,
        audit_run_id=str(audit.metadata.get("run_id") or "") or None,
        source_user_id=source,
        target_user_id=target,
        selected_roles=roles,
        task_ids=sorted(by_task, key=int),
        operations=operations,
        preview=preview,
        import_blockers=list(audit.blockers),
        import_warnings=list(audit.warnings),
        audit_task_count=len(audit.tasks),
        audit_link_count=len(audit.links),
        matched_link_count=len(preview),
        operation_type=operation_type,
        logic_version=2,
        migration_mode="SIMPLE",
        side_effect_policy={"remove_new_previous_responsible_from_auditors": False},
    )
    plan.refresh_digest()
    return plan


def create_owner_plan(
    audit: ImportedAudit,
    source_user_id: str | int,
    owner_field_id: str,
    rules: list[OwnerRule] | tuple[OwnerRule, ...],
    *,
    max_tasks_per_rule: int = 5,
) -> MigrationPlan:
    source = _positive_id(source_user_id, "usuário de origem")
    field_id = str(owner_field_id or "").strip().upper()
    if field_id not in audit.property_fields:
        raise MigrationValidationError("O campo de proprietário não está mapeado de forma inequívoca na planilha.")
    normalized_rules: list[OwnerRule] = []
    seen_rule_ids: set[str] = set()
    owners: dict[str, OwnerRule] = {}
    for raw in rules:
        rule_id = str(raw.rule_id or "").strip().upper()
        label = str(raw.owner_label or "").strip()
        owner_id = _positive_id(raw.owner_id, f"proprietário da regra {label or rule_id}")
        target_id = _positive_id(raw.target_user_id, f"destinatário da regra {label or rule_id}")
        if target_id == source:
            raise MigrationValidationError(f"O destinatário da regra {label or rule_id} deve ser diferente da origem.")
        if not rule_id or rule_id in seen_rule_ids:
            raise MigrationValidationError("As regras condicionais devem possuir identificadores únicos.")
        rule = OwnerRule(rule_id, label or rule_id, owner_id, target_id)
        previous = owners.get(owner_id)
        if previous and previous.target_user_id != target_id:
            raise MigrationValidationError(
                f"Conflito: o proprietário {owner_id} aponta para destinatários diferentes nas regras "
                f"{previous.owner_label} e {rule.owner_label}."
            )
        if previous:
            raise MigrationValidationError(f"Conflito: o proprietário {owner_id} foi configurado em mais de uma regra.")
        owners[owner_id] = rule
        seen_rule_ids.add(rule_id)
        normalized_rules.append(rule)
    if not normalized_rules:
        raise MigrationValidationError("Configure ao menos uma regra condicional.")
    if not isinstance(max_tasks_per_rule, int) or not 1 <= max_tasks_per_rule <= 5:
        raise MigrationValidationError("O limite de homologação deve ficar entre 1 e 5 tarefas por regra.")

    preview: list[PreviewRow] = []
    operations: list[PlannedOperation] = []
    selected_by_rule: dict[str, int] = {}
    responsible_links = sorted(
        (link for link in audit.links if link.user_id == source and link.role == "responsible"),
        key=lambda item: int(item.task_id),
    )
    for link in responsible_links:
        task = audit.tasks.get(link.task_id)
        if task is None:
            continue
        owner_values = list(task.properties.get(field_id, ()))
        state, reason = "IGNORADO", ""
        rule = None
        if audit.blockers:
            state, reason = "BLOQUEADO", "Arquivo de auditoria incompleto ou inconsistente."
        elif link.verification != "confirmed":
            state, reason = "DIVERGENTE", "O vínculo de responsável não foi confirmado pela auditoria."
        elif len(owner_values) != 1:
            state = "DIVERGENTE"
            reason = "A propriedade está ausente ou contém mais de um identificador na planilha."
        else:
            rule = owners.get(owner_values[0])
            if rule is None:
                reason = f"O proprietário {owner_values[0]} não corresponde a nenhuma regra configurada."
            elif selected_by_rule.get(rule.rule_id, 0) >= max_tasks_per_rule:
                state = "IGNORADO"
                reason = f"Limite de homologação de {max_tasks_per_rule} tarefa(s) atingido para esta regra."
            else:
                state = "ELEGIVEL"
                selected_by_rule[rule.rule_id] = selected_by_rule.get(rule.rule_id, 0) + 1
        target = rule.target_user_id if rule else ""
        preview.append(PreviewRow(
            task_id=task.task_id,
            title=task.title,
            role="responsible",
            current_user=source,
            target_user=target,
            previous_value=task.responsible_id,
            proposed_value=target or task.responsible_id,
            eligibility=state,
            blocking_reason=reason,
            operation_type="REPLACE_USER",
            source_disposition="REMOVIDO" if rule else "MANTIDO",
            audited_value=task.responsible_id,
            rule_id=rule.rule_id if rule else "",
            owner_field_id=field_id,
            owner_label=rule.owner_label if rule else "",
            audited_owner_value=owner_values,
        ))
        if state == "ELEGIVEL" and rule:
            operations.append(PlannedOperation(
                task_id=task.task_id,
                title=task.title,
                roles=["responsible"],
                audited_changed_at=task.changed_at,
                before={"RESPONSIBLE_ID": source},
                patch={"RESPONSIBLE_ID": rule.target_user_id},
                operation_type="REPLACE_USER",
                target_user_id=rule.target_user_id,
                rule_id=rule.rule_id,
                owner_field_id=field_id,
                owner_label=rule.owner_label,
                owner_id=rule.owner_id,
            ))
    if not preview:
        raise MigrationValidationError("Nenhuma tarefa da planilha confirma o usuário de origem como responsável.")
    task_ids = sorted((item.task_id for item in operations), key=int)
    plan = MigrationPlan(
        plan_id=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "_" + uuid.uuid4().hex[:8],
        created_at=utc_now(),
        spreadsheet_filename=audit.filename,
        spreadsheet_sha256=audit.sha256,
        audit_run_id=str(audit.metadata.get("run_id") or "") or None,
        source_user_id=source,
        target_user_id=None,
        selected_roles=["responsible"],
        task_ids=task_ids,
        operations=operations,
        preview=preview,
        import_blockers=list(audit.blockers),
        import_warnings=list(audit.warnings),
        audit_task_count=len(audit.tasks),
        audit_link_count=len(audit.links),
        matched_link_count=len(preview),
        operation_type="REPLACE_USER",
        logic_version=3,
        migration_mode="OWNER_RULES",
        owner_field_id=field_id,
        owner_field_label=audit.property_fields[field_id],
        owner_rules=normalized_rules,
        rule_task_limit=max_tasks_per_rule,
    )
    plan.refresh_digest()
    return plan


def _positive_id(value: str | int, label: str) -> str:
    text = str(value or "").strip()
    if not text.isdigit() or int(text) <= 0:
        raise MigrationValidationError(f"O ID de {label} deve ser numérico e positivo.")
    return str(int(text))
