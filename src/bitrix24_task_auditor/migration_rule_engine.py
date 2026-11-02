from __future__ import annotations

import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from .migration_importer import ImportedAudit, ImportedTask
from .migration_models import MigrationPlan, MigrationValidationError, PlannedOperation, PreviewRow, utc_now
from .migration_rules import MigrationRule, RULE_OPERATIONS, validate_rule


def _condition_values(task: ImportedTask, rule: MigrationRule) -> list[str]:
    if rule.condition_type == "NONE":
        return []
    if rule.condition_type == "CREATED_BY":
        return [task.created_by] if task.created_by else []
    return list(task.properties.get(rule.condition_field_id or "", ()))


def _task_role_value(task: ImportedTask, role: str) -> Any:
    if role == "responsible":
        return task.responsible_id
    if role == "accomplice":
        return list(task.accomplices)
    if role == "auditor":
        return list(task.auditors)
    raise MigrationValidationError(f"Papel de regra não suportado: {role}.")


def _compose_field(task: ImportedTask, field: str, bindings: list[dict[str, Any]]) -> tuple[Any, str]:
    if field == "RESPONSIBLE_ID":
        targets = {item["target_user_id"] for item in bindings}
        if len(targets) != 1:
            return None, "Regras propõem responsáveis diferentes para a mesma tarefa."
        return next(iter(targets)), ""

    role = "accomplice" if field == "ACCOMPLICES" else "auditor"
    current = list(_task_role_value(task, role))
    replacements: dict[str, str] = {}
    additions: list[str] = []
    add_sources: set[str] = set()
    for binding in bindings:
        if binding["operation_kind"] == "REPLACE":
            previous = replacements.get(binding["source_user_id"])
            if previous and previous != binding["target_user_id"]:
                return None, f"Regras propõem destinos diferentes para o usuário {binding['source_user_id']} em {field}."
            replacements[binding["source_user_id"]] = binding["target_user_id"]
        else:
            add_sources.add(binding["source_user_id"])
            if binding["target_user_id"] not in additions:
                additions.append(binding["target_user_id"])
    overlap = add_sources.intersection(replacements)
    if overlap:
        return None, f"Uma regra mantém e outra remove o mesmo usuário em {field}: {', '.join(sorted(overlap))}."
    output: list[str] = []
    for user_id in current:
        candidate = replacements.get(user_id, user_id)
        if candidate not in output:
            output.append(candidate)
    for target in additions:
        if target not in output:
            output.append(target)
    return output, ""


def create_rules_plan(audit: ImportedAudit, rules: list[MigrationRule] | tuple[MigrationRule, ...]) -> MigrationPlan:
    selected = [validate_rule(rule) for rule in rules]
    if not selected:
        raise MigrationValidationError("Selecione ao menos uma regra ativa.")
    if any(not rule.active for rule in selected):
        raise MigrationValidationError("Regras inativas não podem compor uma migração.")
    if len({rule.rule_id for rule in selected}) != len(selected):
        raise MigrationValidationError("A mesma regra foi selecionada mais de uma vez.")
    for rule in selected:
        if rule.condition_type in {"GROUP_ID", "CUSTOM_FIELD"}:
            field_id = rule.condition_field_id or ""
            if field_id not in audit.property_fields:
                raise MigrationValidationError(
                    f"A regra {rule.name} exige {field_id}, mas a planilha não mapeia esse campo."
                )

    previews: list[PreviewRow] = []
    candidates: dict[tuple[str, str], list[dict[str, Any]]] = {}
    matched_rules: set[str] = set()
    for rule in selected:
        spec = RULE_OPERATIONS[rule.operation]
        links = sorted(
            (item for item in audit.links if item.user_id == rule.source_user_id and item.role == spec["role"]),
            key=lambda item: int(item.task_id),
        )
        for link in links:
            task = audit.tasks.get(link.task_id)
            if task is None:
                continue
            values = _condition_values(task, rule)
            state, reason = "ELEGIVEL", ""
            if audit.blockers:
                state, reason = "BLOQUEADO", "Arquivo de auditoria incompleto ou inconsistente."
            elif link.verification != "confirmed":
                state, reason = "DIVERGENTE", "O vínculo exigido pela regra não foi confirmado na planilha."
            elif rule.condition_type != "NONE":
                if len(values) != 1:
                    state, reason = "DIVERGENTE", "A condição está ausente ou possui múltiplos valores na planilha."
                elif values[0] != rule.condition_value:
                    state = "IGNORADO"
                    reason = f"Condição não atendida: valor auditado {values[0]}."
            binding = {
                "rule_id": rule.rule_id,
                "rule_version": rule.version,
                "rule_name": rule.name,
                "source_user_id": rule.source_user_id,
                "target_user_id": rule.target_user_id,
                "operation": rule.operation,
                "operation_kind": spec["kind"],
                "role": spec["role"],
                "field": spec["field"],
                "condition_type": rule.condition_type,
                "condition_field_id": rule.condition_field_id,
                "condition_value": rule.condition_value,
                "audited_condition_value": values,
            }
            before = _task_role_value(task, spec["role"])
            preview = PreviewRow(
                task_id=task.task_id,
                title=task.title,
                role=spec["role"],
                current_user=rule.source_user_id,
                target_user=rule.target_user_id,
                previous_value=before,
                proposed_value=before,
                eligibility=state,
                blocking_reason=reason,
                operation_type=spec["kind"] + "_USER",
                source_disposition="MANTIDO" if spec["kind"] == "ADD" else "REMOVIDO",
                audited_value=before,
                rule_id=rule.rule_id,
                rule_name=rule.name,
                condition_field_id=rule.condition_field_id or "",
                condition_value=rule.condition_value,
                audited_owner_value=values,
                role_disposition="MANTIDO_NO_PAPEL" if spec["kind"] == "ADD" else "REMOVIDO_DO_PAPEL",
                current_accomplices=list(task.accomplices),
                current_auditors=list(task.auditors),
                current_creator=task.created_by,
                source_other_roles=[
                    candidate for candidate, present in (
                        ("creator", task.created_by == rule.source_user_id),
                        ("accomplice", rule.source_user_id in task.accomplices),
                        ("auditor", rule.source_user_id in task.auditors),
                    ) if present and candidate != spec["role"]
                ],
                preservation_policy="Somente os campos aprovados podem mudar; participantes, observadores e criador serão comparados após a escrita.",
                side_effect_correction_authorized=False,
            )
            previews.append(preview)
            if state == "ELEGIVEL":
                matched_rules.add(rule.rule_id)
                candidates.setdefault((task.task_id, spec["field"]), []).append(binding)

    warnings = list(audit.warnings)
    for rule in selected:
        if rule.rule_id not in matched_rules:
            warnings.append(f"A regra {rule.name} não encontrou vínculos elegíveis na planilha.")
    preview_by_rule_task = {(row.task_id, row.rule_id): index for index, row in enumerate(previews)}
    task_fields: dict[str, dict[str, tuple[Any, list[dict[str, Any]]]]] = {}
    for (task_id, field), bindings in candidates.items():
        task = audit.tasks[task_id]
        proposed, conflict = _compose_field(task, field, bindings)
        if conflict:
            for binding in bindings:
                index = preview_by_rule_task[(task_id, binding["rule_id"])]
                row = previews[index]
                previews[index] = PreviewRow(**{
                    **asdict(row), "eligibility": "CONFLITO", "blocking_reason": conflict,
                })
            continue
        for binding in bindings:
            index = preview_by_rule_task[(task_id, binding["rule_id"])]
            row = previews[index]
            previews[index] = PreviewRow(**{**asdict(row), "proposed_value": proposed})
        task_fields.setdefault(task_id, {})[field] = (proposed, bindings)

    operations: list[PlannedOperation] = []
    for task_id in sorted(task_fields, key=int):
        task = audit.tasks[task_id]
        before: dict[str, Any] = {}
        patch: dict[str, Any] = {}
        bindings: list[dict[str, Any]] = []
        roles: list[str] = []
        for field, (proposed, field_bindings) in task_fields[task_id].items():
            role = field_bindings[0]["role"]
            before[field] = _task_role_value(task, role)
            patch[field] = proposed
            bindings.extend(field_bindings)
            if role not in roles:
                roles.append(role)
        operations.append(PlannedOperation(
            task_id=task_id,
            title=task.title,
            roles=roles,
            audited_changed_at=task.changed_at,
            before=before,
            patch=patch,
            operation_type="RULE_SET",
            rule_bindings=bindings,
            authorized_side_effect_corrections={},
        ))

    source_ids = {rule.source_user_id for rule in selected}
    plan = MigrationPlan(
        plan_id=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "_" + uuid.uuid4().hex[:8],
        created_at=utc_now(),
        spreadsheet_filename=audit.filename,
        spreadsheet_sha256=audit.sha256,
        audit_run_id=str(audit.metadata.get("run_id") or "") or None,
        source_user_id=next(iter(source_ids)) if len(source_ids) == 1 else None,
        target_user_id=None,
        selected_roles=sorted({RULE_OPERATIONS[rule.operation]["role"] for rule in selected}),
        task_ids=[item.task_id for item in operations],
        operations=operations,
        preview=previews,
        import_blockers=list(audit.blockers),
        import_warnings=list(dict.fromkeys(warnings)),
        audit_task_count=len(audit.tasks),
        audit_link_count=len(audit.links),
        matched_link_count=sum(row.eligibility != "IGNORADO" for row in previews),
        operation_type="RULE_SET",
        logic_version=4,
        migration_mode="RULE_SET",
        rule_snapshots=[rule.snapshot() for rule in selected],
    )
    plan.refresh_digest()
    return plan
