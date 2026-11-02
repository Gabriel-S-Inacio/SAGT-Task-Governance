from __future__ import annotations

import os
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from openpyxl.utils.datetime import from_excel

from .exporters import export_json, export_xlsx
from .migration_client import AmbiguousWriteError, MigrationApiError, MigrationBitrixClient, sanitized_response
from .migration_models import MigrationPlan, PlannedOperation, PreviewRow, ROLE_FIELDS, SUPPORTED_WRITE_ROLES, utc_now
from .migration_store import MigrationStore, MigrationStoreError


TERMINAL_RESULTS = frozenset({
    "ATUALIZADO_VERIFICADO", "JA_ATENDIDO", "IGNORADO", "NAO_SUPORTADO", "DIVERGENTE",
    "ATUALIZADA_E_VERIFICADA", "CORRECAO_PENDENTE", "EFEITO_COLATERAL_DETECTADO",
    "FALHA_ATUALIZACAO", "FALHA_VERIFICACAO", "SEM_PERMISSAO", "NAO_ACESSIVEL",
    "RESULTADO_INCERTO", "PREVISTO_DRY_RUN", "OPERACAO_NAO_COMPROVADA",
})
LINKAGE_FIELDS = ("RESPONSIBLE_ID", "ACCOMPLICES", "AUDITORS", "CREATED_BY")


def _pick(raw: dict[str, Any], *names: str, default: Any = None) -> Any:
    normalized = {"".join(ch for ch in str(key).casefold() if ch.isalnum()): value for key, value in raw.items()}
    for name in names:
        key = "".join(ch for ch in name.casefold() if ch.isalnum())
        if key in normalized:
            return normalized[key]
    return default


def _id(value: Any) -> str | None:
    if isinstance(value, dict):
        value = value.get("id", value.get("ID"))
    text = str(value).strip() if value is not None else ""
    return str(int(text)) if text.isdigit() and int(text) > 0 else None


def _ids(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, dict):
        value = list(value.values())
    if not isinstance(value, (list, tuple, set)):
        value = [value]
    output: list[str] = []
    for item in value:
        candidate = _id(item)
        if candidate and candidate not in output:
            output.append(candidate)
    return output


def current_fields(task: dict[str, Any]) -> dict[str, Any]:
    return {
        "RESPONSIBLE_ID": _id(_pick(task, "responsibleId", "RESPONSIBLE_ID")),
        "CREATED_BY": _id(_pick(task, "createdBy", "CREATED_BY")),
        "ACCOMPLICES": _ids(_pick(task, "accomplices", "ACCOMPLICES")),
        "AUDITORS": _ids(_pick(task, "auditors", "AUDITORS")),
        "CHANGED_DATE": str(_pick(task, "changedDate", "CHANGED_DATE", default="") or "").strip() or None,
    }


def current_property_ids(task: dict[str, Any], field_id: str) -> list[str]:
    return _ids(_pick(task, field_id, default=None))


def linkage_state(task: dict[str, Any]) -> dict[str, Any]:
    current = current_fields(task)
    return {field: current[field] for field in LINKAGE_FIELDS}


def _source_other_roles(state: dict[str, Any], source_user_id: str, affected_role: str) -> list[str]:
    roles = []
    if state.get("CREATED_BY") == source_user_id and affected_role != "creator":
        roles.append("creator")
    if source_user_id in state.get("ACCOMPLICES", []) and affected_role != "accomplice":
        roles.append("accomplice")
    if source_user_id in state.get("AUDITORS", []) and affected_role != "auditor":
        roles.append("auditor")
    return roles


def _can_edit(task: dict[str, Any]) -> bool | None:
    action = _pick(task, "action", default=None)
    if not isinstance(action, dict):
        return None
    raw = action.get("edit", action.get("EDIT"))
    if raw is None:
        return None
    return raw is True or str(raw).upper() in {"Y", "1", "TRUE"}


def _target_active(user: dict[str, Any] | None) -> bool:
    if not user:
        return False
    raw = user.get("ACTIVE", user.get("active", False))
    return raw is True or str(raw).upper() in {"Y", "1", "TRUE"}


def _parse_changed_at(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    try:
        serial = float(text)
    except (TypeError, ValueError):
        candidate_text = text.replace("Z", "+00:00")
        try:
            candidate = datetime.fromisoformat(candidate_text)
        except ValueError:
            return None
    else:
        if not 1 <= serial <= 2958465:
            return None
        try:
            candidate = from_excel(serial)
        except (TypeError, ValueError, OverflowError):
            return None
    if candidate.tzinfo is None:
        candidate = candidate.replace(tzinfo=timezone.utc)
    return candidate.astimezone(timezone.utc)


def changed_at_comparison(audited: Any, live: Any) -> tuple[bool | None, str, str]:
    audited_value = _parse_changed_at(audited)
    live_value = _parse_changed_at(live)
    audited_label = audited_value.isoformat() if audited_value else str(audited or "ausente")
    live_label = live_value.isoformat() if live_value else str(live or "ausente")
    if not audited_value or not live_value:
        return None, audited_label, live_label
    return abs((audited_value - live_value).total_seconds()) <= 1.0, audited_label, live_label


def _error_result(exc: MigrationApiError) -> str:
    text = f"{exc.code} {exc}".upper()
    if exc.status in {401, 403} or any(marker in text for marker in ("ACCESS", "PERMISSION", "DENIED", "FORBIDDEN")):
        return "SEM_PERMISSAO"
    if exc.code == "NOT_ACCESSIBLE" or exc.status == 404:
        return "NAO_ACESSIVEL"
    return "FALHA"


def preflight_plan(
    plan: MigrationPlan,
    client: MigrationBitrixClient,
    *,
    on_progress: Callable[[int, int, str], None] | None = None,
) -> MigrationPlan:
    """Re-read all tasks. This is read-only and must happen before an approval can be issued."""
    if plan.migration_mode == "RULE_SET":
        return _preflight_rules_plan(plan, client, on_progress=on_progress)
    if plan.operation_type not in {"REPLACE_USER", "ADD_USER"}:
        raise ValueError("Tipo de operação do plano é inválido.")
    if any(row.operation_type != plan.operation_type for row in plan.preview):
        raise ValueError("A intenção do plano diverge da intenção registrada na prévia.")
    if plan.migration_mode == "OWNER_RULES":
        return _preflight_owner_plan(plan, client, on_progress=on_progress)
    if plan.migration_mode != "SIMPLE":
        raise ValueError("Modo de migração do plano é inválido.")
    plan.logic_version = 5
    try:
        target = client.get_user(plan.target_user_id)
    except MigrationApiError as exc:
        target = None
        target_error = str(exc)
    else:
        target_error = ""
    plan.target_user = {
        "id": plan.target_user_id,
        "name": " ".join(str(target.get(key, "") or "").strip() for key in ("NAME", "LAST_NAME")).strip() if target else "",
        "active": _target_active(target),
    }
    plan.preflight_at = utc_now()
    rows_by_key = {(row.task_id, row.role): row for row in plan.preview}
    updated_operations: list[PlannedOperation] = []
    eligible_task_ids: list[str] = []
    total = len(plan.operations)
    for index, operation in enumerate(plan.operations, 1):
        op = PlannedOperation(**asdict(operation))
        op.operation_type = plan.operation_type
        # Rebuild the candidate from the immutable preview values. A previous
        # preflight may have removed a temporarily divergent role from op.patch;
        # revalidation must be able to reconsider it without modifying the source file.
        candidate_rows = [
            row for row in plan.preview
            if row.task_id == op.task_id and row.role in SUPPORTED_WRITE_ROLES
        ]
        if candidate_rows:
            op.roles = [row.role for row in candidate_rows]
            op.before = {ROLE_FIELDS[row.role]: row.previous_value for row in candidate_rows}
            op.patch = {ROLE_FIELDS[row.role]: row.proposed_value for row in candidate_rows}
        if not _target_active(target):
            op.eligibility = "BLOQUEADO"
            op.blocking_reason = target_error or "Usuário de destino inexistente ou inativo."
            for role in op.roles:
                old = rows_by_key[(op.task_id, role)]
                rows_by_key[(op.task_id, role)] = replace(old, eligibility="BLOQUEADO", blocking_reason=op.blocking_reason)
            updated_operations.append(op)
            if on_progress:
                on_progress(index, total, op.task_id)
            continue
        try:
            raw = client.get_task(op.task_id)
        except MigrationApiError as exc:
            op.eligibility = _error_result(exc)
            op.blocking_reason = str(exc)
            for role in op.roles:
                old = rows_by_key[(op.task_id, role)]
                rows_by_key[(op.task_id, role)] = replace(old, eligibility=op.eligibility, blocking_reason=str(exc))
            updated_operations.append(op)
            if on_progress:
                on_progress(index, total, op.task_id)
            continue
        live = current_fields(raw)
        live_links = linkage_state(raw)
        if _can_edit(raw) is not True:
            op.eligibility = "SEM_PERMISSAO"
            op.blocking_reason = "A permissão action.edit não foi comprovada para o webhook."
        else:
            eligible_roles: list[str] = []
            patch: dict[str, Any] = {}
            for role in op.roles:
                field = ROLE_FIELDS[role]
                current = live[field]
                old_row = replace(
                    rows_by_key[(op.task_id, role)],
                    current_accomplices=list(live_links["ACCOMPLICES"]),
                    current_auditors=list(live_links["AUDITORS"]),
                    current_creator=live_links["CREATED_BY"],
                    source_other_roles=_source_other_roles(live_links, plan.source_user_id or "", role),
                    preservation_policy=(
                        "Somente os campos aprovados podem mudar; ACCOMPLICES, AUDITORS e CREATED_BY "
                        "serão comparados integralmente após a escrita."
                    ),
                )
                values = current if isinstance(current, list) else [current] if current else []
                if plan.source_user_id not in values:
                    if plan.operation_type == "REPLACE_USER" and plan.target_user_id in values:
                        rows_by_key[(op.task_id, role)] = replace(
                            old_row, eligibility="JA_ATENDIDO", blocking_reason="A origem já foi removida e o destino ocupa o papel."
                        )
                    else:
                        rows_by_key[(op.task_id, role)] = replace(
                            old_row, eligibility="DIVERGENTE", blocking_reason="O vínculo de origem não existe mais na tarefa."
                        )
                elif plan.operation_type == "ADD_USER" and plan.target_user_id in values:
                    rows_by_key[(op.task_id, role)] = replace(
                        old_row, eligibility="JA_ATENDIDO", blocking_reason="O destino já ocupa este papel; nenhuma escrita será feita."
                    )
                else:
                    if role == "responsible":
                        proposed = plan.target_user_id
                    elif plan.operation_type == "ADD_USER":
                        proposed = [*values, plan.target_user_id]
                    else:
                        proposed = []
                        for user_id in values:
                            replacement = plan.target_user_id if user_id == plan.source_user_id else user_id
                            if replacement not in proposed:
                                proposed.append(replacement)
                    eligible_roles.append(role)
                    patch[field] = proposed
                    op.before[field] = current
                    rows_by_key[(op.task_id, role)] = replace(
                        old_row,
                        previous_value=current,
                        proposed_value=proposed,
                        eligibility="ELEGIVEL",
                        blocking_reason="",
                        source_disposition="MANTIDO" if plan.operation_type == "ADD_USER" else "REMOVIDO",
                        role_disposition="MANTIDO_NO_PAPEL" if plan.operation_type == "ADD_USER" else "REMOVIDO_DO_PAPEL",
                    )
            if eligible_roles:
                op.roles = eligible_roles
                op.patch = patch
                op.before = {ROLE_FIELDS[role]: op.before[ROLE_FIELDS[role]] for role in eligible_roles}
                op.eligibility = "ELEGIVEL"
                op.blocking_reason = ""
                op.integrity_before = live_links
                finalization_authorized = _bind_responsible_final_state(
                    op, plan.source_user_id or ""
                )
                if "responsible" in eligible_roles:
                    old = rows_by_key[(op.task_id, "responsible")]
                    rows_by_key[(op.task_id, "responsible")] = replace(
                        old,
                        side_effect_correction_authorized=finalization_authorized,
                        preservation_policy=(
                            "RESPONSIBLE_ID e a lista final de AUDITORS serão enviados juntos. Se o Bitrix24 "
                            "reintroduzir apenas o responsável anterior, a mesma operação aprovada poderá "
                            "restaurar AUDITORS e verificará novamente todos os vínculos."
                        ),
                    )
                eligible_task_ids.append(op.task_id)
            else:
                states = [rows_by_key[(op.task_id, role)].eligibility for role in op.roles]
                op.eligibility = "JA_ATENDIDO" if states and set(states) == {"JA_ATENDIDO"} else "DIVERGENTE"
                op.patch = {}
        if op.eligibility != "ELEGIVEL" and op.blocking_reason:
            for role in op.roles:
                old = rows_by_key[(op.task_id, role)]
                rows_by_key[(op.task_id, role)] = replace(
                    old, eligibility=op.eligibility, blocking_reason=op.blocking_reason
                )
        updated_operations.append(op)
        if on_progress:
            on_progress(index, total, op.task_id)
    plan.operations = updated_operations
    plan.preview = [rows_by_key[(row.task_id, row.role)] for row in plan.preview]
    plan.task_ids = sorted(eligible_task_ids, key=int)
    plan.refresh_digest()
    return plan


def _compose_live_field(current: Any, field: str, bindings: list[dict[str, Any]]) -> tuple[Any, str]:
    if field == "RESPONSIBLE_ID":
        targets = {item["target_user_id"] for item in bindings}
        if len(targets) != 1:
            return None, "Regras propõem responsáveis diferentes."
        return next(iter(targets)), ""
    values = list(current or [])
    replacements: dict[str, str] = {}
    additions: list[str] = []
    add_sources: set[str] = set()
    for binding in bindings:
        if binding["operation_kind"] == "REPLACE":
            previous = replacements.get(binding["source_user_id"])
            if previous and previous != binding["target_user_id"]:
                return None, f"Regras propõem destinos incompatíveis em {field}."
            replacements[binding["source_user_id"]] = binding["target_user_id"]
        else:
            add_sources.add(binding["source_user_id"])
            if binding["target_user_id"] not in additions:
                additions.append(binding["target_user_id"])
    if add_sources.intersection(replacements):
        return None, f"Uma regra mantém e outra remove o mesmo usuário em {field}."
    output: list[str] = []
    for user_id in values:
        candidate = replacements.get(user_id, user_id)
        if candidate not in output:
            output.append(candidate)
    for target in additions:
        if target not in output:
            output.append(target)
    return output, ""


def _binding_condition_values(raw: dict[str, Any], binding: dict[str, Any]) -> list[str]:
    condition_type = binding.get("condition_type")
    if condition_type == "NONE":
        return []
    if condition_type == "CREATED_BY":
        value = current_fields(raw)["CREATED_BY"]
        return [value] if value else []
    return current_property_ids(raw, str(binding.get("condition_field_id") or ""))


def _bind_responsible_final_state(operation: PlannedOperation, source_user_id: str) -> bool:
    """Bind the final auditor list and its narrowly scoped fallback to the plan."""
    previous = _id(operation.integrity_before.get("RESPONSIBLE_ID"))
    proposed = _id(operation.patch.get("RESPONSIBLE_ID"))
    if not previous or not proposed or previous == proposed or previous != source_user_id:
        return False
    baseline_auditors = _ids(operation.integrity_before.get("AUDITORS"))
    operation.patch.setdefault("AUDITORS", list(baseline_auditors))
    final_auditors = _ids(operation.patch.get("AUDITORS"))
    correction_authorized = source_user_id not in final_auditors and (
        source_user_id not in baseline_auditors or "auditor" in operation.roles
    )
    operation.authorized_side_effect_corrections = (
        {"AUDITORS_REMOVE_IF_REINTRODUCED": [source_user_id]}
        if correction_authorized
        else {}
    )
    return correction_authorized


def _preflight_rules_plan(
    plan: MigrationPlan,
    client: MigrationBitrixClient,
    *,
    on_progress: Callable[[int, int, str], None] | None = None,
) -> MigrationPlan:
    if not plan.rule_snapshots:
        raise ValueError("Plano de regras sem cópias imutáveis das regras selecionadas.")
    target_ids = list(dict.fromkeys(str(item.get("target_user_id")) for item in plan.rule_snapshots))
    targets: dict[str, dict[str, Any]] = {}
    for target_id in target_ids:
        try:
            user = client.get_user(target_id)
        except MigrationApiError as exc:
            user = None
            error = str(exc)
        else:
            error = ""
        targets[target_id] = {
            "id": target_id,
            "name": " ".join(str(user.get(key, "") or "").strip() for key in ("NAME", "LAST_NAME")).strip() if user else "",
            "active": _target_active(user),
            "error": error,
        }
    plan.target_users = targets
    plan.target_user = None
    plan.logic_version = max(plan.logic_version, 5)
    plan.preflight_at = utc_now()
    rows = list(plan.preview)
    row_indexes = {(row.task_id, row.rule_id): index for index, row in enumerate(rows)}
    updated_operations: list[PlannedOperation] = []
    eligible_task_ids: list[str] = []
    total = len(plan.operations)
    for index, operation in enumerate(plan.operations, 1):
        op = PlannedOperation(**asdict(operation))
        extra_select = list(dict.fromkeys(
            str(binding.get("condition_field_id"))
            for binding in op.rule_bindings
            if binding.get("condition_type") in {"GROUP_ID", "CUSTOM_FIELD"}
            and binding.get("condition_field_id")
        ))
        try:
            raw = client.get_task(op.task_id, extra_select) if extra_select else client.get_task(op.task_id)
        except MigrationApiError as exc:
            op.eligibility = _error_result(exc)
            op.blocking_reason = str(exc)
            for binding in op.rule_bindings:
                row_index = row_indexes[(op.task_id, binding["rule_id"])]
                rows[row_index] = replace(rows[row_index], eligibility=op.eligibility, blocking_reason=op.blocking_reason)
            updated_operations.append(op)
            if on_progress:
                on_progress(index, total, op.task_id)
            continue
        live = current_fields(raw)
        live_links = linkage_state(raw)
        for binding in op.rule_bindings:
            row_index = row_indexes[(op.task_id, binding["rule_id"])]
            rows[row_index] = replace(
                rows[row_index],
                current_accomplices=list(live_links["ACCOMPLICES"]),
                current_auditors=list(live_links["AUDITORS"]),
                current_creator=live_links["CREATED_BY"],
                source_other_roles=_source_other_roles(
                    live_links, binding["source_user_id"], binding["role"]
                ),
                preservation_policy=(
                    "Somente os campos aprovados podem mudar; todos os quatro vínculos serão comparados após a escrita."
                ),
            )
        if _can_edit(raw) is not True:
            op.eligibility = "SEM_PERMISSAO"
            op.blocking_reason = "A permissão action.edit não foi comprovada para o webhook."
            for binding in op.rule_bindings:
                row_index = row_indexes[(op.task_id, binding["rule_id"])]
                rows[row_index] = replace(rows[row_index], eligibility=op.eligibility, blocking_reason=op.blocking_reason)
            updated_operations.append(op)
            if on_progress:
                on_progress(index, total, op.task_id)
            continue

        by_field: dict[str, list[dict[str, Any]]] = {}
        for binding in op.rule_bindings:
            by_field.setdefault(binding["field"], []).append(binding)
        patch: dict[str, Any] = {}
        before: dict[str, Any] = {}
        active_bindings: list[dict[str, Any]] = []
        for field, bindings in by_field.items():
            field_block = ""
            all_satisfied = True
            current = live[field]
            values = current if isinstance(current, list) else [current] if current else []
            condition_by_rule: dict[str, list[str]] = {}
            for binding in bindings:
                target = targets.get(binding["target_user_id"], {})
                condition_values = _binding_condition_values(raw, binding)
                condition_by_rule[binding["rule_id"]] = condition_values
                if not target.get("active"):
                    field_block = target.get("error") or f"Destinatário {binding['target_user_id']} inexistente ou inativo."
                    break
                if binding["condition_type"] != "NONE" and condition_values != [binding["condition_value"]]:
                    field_block = (
                        f"A condição atual de {binding['rule_name']} não confirma exclusivamente "
                        f"{binding['condition_value']}."
                    )
                    break
                source_present = binding["source_user_id"] in values
                target_present = binding["target_user_id"] in values
                if binding["operation_kind"] == "ADD":
                    if not source_present:
                        field_block = f"A origem da regra {binding['rule_name']} não ocupa mais o papel."
                        break
                    satisfied = target_present
                else:
                    if not source_present and not target_present:
                        field_block = f"A origem da regra {binding['rule_name']} não ocupa mais o papel."
                        break
                    satisfied = not source_present and target_present
                all_satisfied = all_satisfied and satisfied
            if field_block:
                for binding in bindings:
                    row_index = row_indexes[(op.task_id, binding["rule_id"])]
                    rows[row_index] = replace(
                        rows[row_index],
                        previous_value=current,
                        current_condition_value=condition_by_rule.get(binding["rule_id"], []),
                        eligibility="DIVERGENTE",
                        blocking_reason=field_block,
                    )
                continue
            if all_satisfied:
                for binding in bindings:
                    row_index = row_indexes[(op.task_id, binding["rule_id"])]
                    rows[row_index] = replace(
                        rows[row_index], previous_value=current,
                        current_condition_value=condition_by_rule[binding["rule_id"]],
                        eligibility="JA_ATENDIDO", blocking_reason="A regra já está atendida.",
                    )
                continue
            proposed, conflict = _compose_live_field(current, field, bindings)
            if conflict:
                for binding in bindings:
                    row_index = row_indexes[(op.task_id, binding["rule_id"])]
                    rows[row_index] = replace(rows[row_index], eligibility="CONFLITO", blocking_reason=conflict)
                continue
            before[field] = current
            patch[field] = proposed
            active_bindings.extend(bindings)
            for binding in bindings:
                row_index = row_indexes[(op.task_id, binding["rule_id"])]
                rows[row_index] = replace(
                    rows[row_index],
                    previous_value=current,
                    proposed_value=proposed,
                    current_condition_value=condition_by_rule[binding["rule_id"]],
                    eligibility="ELEGIVEL",
                    blocking_reason="",
                )
        if patch:
            op.before = before
            op.patch = patch
            op.rule_bindings = active_bindings
            op.roles = list(dict.fromkeys(binding["role"] for binding in active_bindings))
            op.eligibility = "ELEGIVEL"
            op.blocking_reason = ""
            op.integrity_before = live_links
            responsible_sources = list(dict.fromkeys(
                binding["source_user_id"]
                for binding in active_bindings
                if binding["field"] == "RESPONSIBLE_ID"
            ))
            finalization_authorized = (
                _bind_responsible_final_state(op, responsible_sources[0])
                if len(responsible_sources) == 1
                else False
            )
            for binding in active_bindings:
                if binding["field"] != "RESPONSIBLE_ID":
                    continue
                row_index = row_indexes[(op.task_id, binding["rule_id"])]
                rows[row_index] = replace(
                    rows[row_index],
                    side_effect_correction_authorized=finalization_authorized,
                    preservation_policy=(
                        "RESPONSIBLE_ID e a lista final de AUDITORS serão enviados juntos. Se o Bitrix24 "
                        "reintroduzir apenas o responsável anterior, a mesma operação aprovada poderá "
                        "restaurar AUDITORS e verificará novamente todos os vínculos."
                    ),
                )
            eligible_task_ids.append(op.task_id)
        else:
            op.patch = {}
            op.rule_bindings = []
            op.eligibility = "DIVERGENTE"
            op.blocking_reason = "Nenhum campo desta tarefa permaneceu elegível."
        updated_operations.append(op)
        if on_progress:
            on_progress(index, total, op.task_id)
    plan.operations = updated_operations
    plan.preview = rows
    plan.task_ids = sorted(eligible_task_ids, key=int)
    plan.refresh_digest()
    return plan


def _preflight_owner_plan(
    plan: MigrationPlan,
    client: MigrationBitrixClient,
    *,
    on_progress: Callable[[int, int, str], None] | None = None,
) -> MigrationPlan:
    if not plan.owner_field_id or not plan.owner_rules:
        raise ValueError("Plano condicional sem campo de proprietário ou regras.")
    targets: dict[str, dict[str, Any]] = {}
    for target_id in dict.fromkeys(rule.target_user_id for rule in plan.owner_rules):
        try:
            user = client.get_user(target_id)
        except MigrationApiError as exc:
            user = None
            error = str(exc)
        else:
            error = ""
        targets[target_id] = {
            "id": target_id,
            "name": " ".join(str(user.get(key, "") or "").strip() for key in ("NAME", "LAST_NAME")).strip() if user else "",
            "active": _target_active(user),
            "error": error,
        }
    plan.target_users = targets
    plan.target_user = None
    plan.logic_version = max(plan.logic_version, 5)
    plan.preflight_at = utc_now()
    rows_by_task = {row.task_id: row for row in plan.preview}
    updated_operations: list[PlannedOperation] = []
    eligible_task_ids: list[str] = []
    total = len(plan.operations)
    for index, operation in enumerate(plan.operations, 1):
        op = PlannedOperation(**asdict(operation))
        row = rows_by_task[op.task_id]
        target_id = op.target_user_id or ""
        target = targets.get(target_id, {})
        if not target.get("active"):
            op.eligibility = "BLOQUEADO"
            op.blocking_reason = target.get("error") or f"Destinatário {target_id} inexistente ou inativo."
            rows_by_task[op.task_id] = replace(row, eligibility=op.eligibility, blocking_reason=op.blocking_reason)
            updated_operations.append(op)
            if on_progress:
                on_progress(index, total, op.task_id)
            continue
        try:
            raw = client.get_task(op.task_id, [plan.owner_field_id])
        except MigrationApiError as exc:
            op.eligibility = _error_result(exc)
            op.blocking_reason = str(exc)
            rows_by_task[op.task_id] = replace(row, eligibility=op.eligibility, blocking_reason=op.blocking_reason)
            updated_operations.append(op)
            if on_progress:
                on_progress(index, total, op.task_id)
            continue
        live = current_fields(raw)
        live_links = linkage_state(raw)
        owner_values = current_property_ids(raw, plan.owner_field_id)
        responsible = live["RESPONSIBLE_ID"]
        if _can_edit(raw) is not True:
            op.eligibility = "SEM_PERMISSAO"
            op.blocking_reason = "A permissão action.edit não foi comprovada para o webhook."
        elif owner_values != [op.owner_id]:
            op.eligibility = "DIVERGENTE"
            op.blocking_reason = (
                f"A propriedade atual em {plan.owner_field_id} não confirma exclusivamente o ID {op.owner_id}."
            )
        elif responsible == target_id:
            op.eligibility = "JA_ATENDIDO"
            op.blocking_reason = "O destinatário da regra já é o responsável."
            op.patch = {}
        elif responsible != plan.source_user_id:
            op.eligibility = "DIVERGENTE"
            op.blocking_reason = (
                f"O usuário de origem {plan.source_user_id} não ocupa mais o papel de responsável nesta tarefa."
            )
        else:
            op.before = {"RESPONSIBLE_ID": responsible}
            op.patch = {"RESPONSIBLE_ID": target_id}
            op.eligibility = "ELEGIVEL"
            op.blocking_reason = ""
            op.integrity_before = live_links
            finalization_authorized = _bind_responsible_final_state(
                op, plan.source_user_id or ""
            )
            eligible_task_ids.append(op.task_id)
        if op.eligibility != "ELEGIVEL":
            finalization_authorized = False
        rows_by_task[op.task_id] = replace(
            row,
            previous_value=responsible,
            proposed_value=target_id if op.eligibility == "ELEGIVEL" else row.proposed_value,
            current_owner_value=owner_values,
            current_accomplices=list(live_links["ACCOMPLICES"]),
            current_auditors=list(live_links["AUDITORS"]),
            current_creator=live_links["CREATED_BY"],
            source_other_roles=_source_other_roles(live_links, plan.source_user_id or "", "responsible"),
            role_disposition="REMOVIDO_DO_PAPEL",
            preservation_policy=(
                "RESPONSIBLE_ID e a lista final de AUDITORS serão enviados juntos; ACCOMPLICES e CREATED_BY "
                "serão preservados e todos os vínculos serão verificados após a escrita."
            ),
            side_effect_correction_authorized=finalization_authorized,
            eligibility=op.eligibility,
            blocking_reason=op.blocking_reason,
        )
        updated_operations.append(op)
        if on_progress:
            on_progress(index, total, op.task_id)
    plan.operations = updated_operations
    plan.preview = [rows_by_task.get(row.task_id, row) for row in plan.preview]
    plan.task_ids = sorted(eligible_task_ids, key=int)
    plan.refresh_digest()
    return plan


def _matches_patch(task: dict[str, Any], patch: dict[str, Any]) -> bool:
    current = current_fields(task)
    return all(current.get(field) == value for field, value in patch.items())


def _normalized_linkage_value(field: str, value: Any) -> Any:
    return _ids(value) if field in {"ACCOMPLICES", "AUDITORS"} else _id(value)


def _expected_linkage(operation: PlannedOperation) -> dict[str, Any]:
    expected = {
        field: _normalized_linkage_value(field, operation.integrity_before.get(field))
        for field in LINKAGE_FIELDS
    }
    for field, value in operation.patch.items():
        if field in expected:
            expected[field] = _normalized_linkage_value(field, value)
    return expected


def _same_linkage_value(field: str, left: Any, right: Any) -> bool:
    if field in {"ACCOMPLICES", "AUDITORS"}:
        return set(_ids(left)) == set(_ids(right))
    return _id(left) == _id(right)


def _linkage_differences(expected: dict[str, Any], actual: dict[str, Any]) -> list[dict[str, Any]]:
    differences: list[dict[str, Any]] = []
    for field in LINKAGE_FIELDS:
        expected_value = expected.get(field)
        actual_value = actual.get(field)
        if _same_linkage_value(field, expected_value, actual_value):
            continue
        detail: dict[str, Any] = {"field": field, "expected": expected_value, "actual": actual_value}
        if field in {"ACCOMPLICES", "AUDITORS"}:
            expected_ids = _ids(expected_value)
            actual_ids = _ids(actual_value)
            detail["added"] = [item for item in actual_ids if item not in expected_ids]
            detail["removed"] = [item for item in expected_ids if item not in actual_ids]
        differences.append(detail)
    return differences


def _integrity_matches(operation: PlannedOperation, task: dict[str, Any]) -> bool:
    return bool(operation.integrity_before) and not _linkage_differences(
        _expected_linkage(operation), linkage_state(task)
    )


def _baseline_matches(operation: PlannedOperation, task: dict[str, Any]) -> bool:
    if not operation.integrity_before:
        return False
    baseline = {
        field: _normalized_linkage_value(field, operation.integrity_before.get(field))
        for field in LINKAGE_FIELDS
    }
    return not _linkage_differences(baseline, linkage_state(task))


def _operation_identity(plan: MigrationPlan, operation: PlannedOperation) -> dict[str, Any]:
    bindings = operation.rule_bindings
    if bindings:
        sources = list(dict.fromkeys(str(item.get("source_user_id")) for item in bindings if item.get("source_user_id")))
        targets = list(dict.fromkeys(str(item.get("target_user_id")) for item in bindings if item.get("target_user_id")))
        rule_ids = list(dict.fromkeys(str(item.get("rule_id")) for item in bindings if item.get("rule_id")))
        rule_names = list(dict.fromkeys(str(item.get("rule_name")) for item in bindings if item.get("rule_name")))
    else:
        sources = [plan.source_user_id] if plan.source_user_id else []
        target = operation.target_user_id or plan.target_user_id
        targets = [target] if target else []
        rule_ids = [operation.rule_id] if operation.rule_id else []
        rule_names = [operation.owner_label] if operation.owner_label else []
    return {
        "source_user_ids": sources,
        "target_user_ids": targets,
        "rule_ids": rule_ids,
        "rule_names": rule_names,
        "affected_fields": list(operation.patch),
    }


def _verify_side_effects(
    operation: PlannedOperation,
    client: MigrationBitrixClient,
    verified: dict[str, Any],
) -> tuple[str, str, dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """Verify and, when pre-approved, finish one logical responsible transfer."""
    expected = _expected_linkage(operation)
    actual = linkage_state(verified)
    effects = _linkage_differences(expected, actual)
    corrections: list[dict[str, Any]] = []
    if not effects:
        return (
            "ATUALIZADA_E_VERIFICADA",
            "Todos os vínculos aprovados foram persistidos e preservados.",
            actual,
            effects,
            corrections,
        )

    authorized_sources = operation.authorized_side_effect_corrections.get(
        "AUDITORS_REMOVE_IF_REINTRODUCED", []
    )
    source_id = authorized_sources[0] if len(authorized_sources) == 1 else ""
    expected_auditors = _ids(expected.get("AUDITORS"))
    can_finalize = (
        bool(source_id)
        and len(effects) == 1
        and effects[0]["field"] == "AUDITORS"
        and effects[0].get("added") == [source_id]
        and not effects[0].get("removed")
        and source_id not in expected_auditors
    )
    if can_finalize:
        correction: dict[str, Any] = {
            "action": "RESTORE_APPROVED_AUDITORS",
            "field": "AUDITORS",
            "source_user_id": source_id,
            "before": actual["AUDITORS"],
            "patch": {"AUDITORS": expected_auditors},
        }
        corrections.append(correction)
        try:
            fresh = client.get_task(operation.task_id)
        except MigrationApiError as exc:
            correction.update(result="RESULTADO_INCERTO", error=str(exc))
            return (
                "RESULTADO_INCERTO",
                "O efeito previsto foi detectado, mas a releitura anterior à finalização falhou; nenhuma segunda escrita foi enviada.",
                actual,
                effects,
                corrections,
            )
        fresh_state = linkage_state(fresh)
        fresh_effects = _linkage_differences(expected, fresh_state)
        if fresh_effects != effects:
            correction.update(result="DIVERGENCIA_CONCORRENTE", observed=fresh_state)
            return (
                "CORRECAO_PENDENTE",
                "Os vínculos mudaram entre as etapas da operação lógica; a finalização não foi enviada.",
                fresh_state,
                fresh_effects,
                corrections,
            )
        try:
            response = client.update_task(operation.task_id, {"AUDITORS": expected_auditors})
            correction["rest_response"] = sanitized_response(response)
        except AmbiguousWriteError as exc:
            correction["write_error"] = str(exc)
        except MigrationApiError as exc:
            correction.update(result="FALHA_FINALIZACAO", error=str(exc))
            return (
                "CORRECAO_PENDENTE",
                "A transferência foi aplicada, mas a restauração aprovada de AUDITORS falhou.",
                fresh_state,
                fresh_effects,
                corrections,
            )
        try:
            final_task = client.get_task(operation.task_id)
        except MigrationApiError as exc:
            correction.update(result="RESULTADO_INCERTO", verification_error=str(exc))
            return (
                "RESULTADO_INCERTO",
                "A finalização foi enviada, mas a verificação final falhou; não repetir automaticamente.",
                fresh_state,
                fresh_effects,
                corrections,
            )
        final_state = linkage_state(final_task)
        final_effects = _linkage_differences(expected, final_state)
        if final_effects:
            correction.update(result="FALHA_VERIFICACAO", after=final_state)
            return (
                "CORRECAO_PENDENTE",
                "O estado final persistido não corresponde integralmente à operação aprovada.",
                final_state,
                final_effects,
                corrections,
            )
        correction.update(result="ATUALIZADA_E_VERIFICADA", after=final_state)
        return (
            "ATUALIZADA_E_VERIFICADA",
            "Transferência e estado final dos observadores concluídos e verificados.",
            final_state,
            effects,
            corrections,
        )

    if any(item["field"] not in operation.patch for item in effects):
        return (
            "CORRECAO_PENDENTE",
            "A atualização criou ou removeu vínculos fora dos campos autorizados; revisão manual necessária.",
            actual,
            effects,
            corrections,
        )
    return (
        "FALHA_VERIFICACAO",
        "Os campos efetivamente persistidos não correspondem ao plano aprovado.",
        actual,
        effects,
        corrections,
    )


def _responsible_retention_risk(operation: PlannedOperation) -> bool:
    previous = _id(operation.integrity_before.get("RESPONSIBLE_ID"))
    proposed = _id(operation.patch.get("RESPONSIBLE_ID"))
    previous_auditors = _ids(operation.integrity_before.get("AUDITORS"))
    return bool(previous and proposed and previous != proposed and previous not in previous_auditors)


def _responsible_finalization_is_authorized(operation: PlannedOperation) -> bool:
    sources = operation.authorized_side_effect_corrections.get(
        "AUDITORS_REMOVE_IF_REINTRODUCED", []
    )
    if len(sources) != 1 or "AUDITORS" not in operation.patch:
        return False
    source_id = sources[0]
    return (
        source_id not in _ids(operation.patch.get("AUDITORS"))
        and source_id not in _ids(operation.integrity_before.get("AUDITORS"))
    )


def unverified_responsible_task_ids(plan: MigrationPlan) -> list[str]:
    """Return only tasks whose responsible write still lacks a safe contract."""
    authorized = set(plan.task_ids)
    return [
        operation.task_id
        for operation in plan.operations
        if operation.task_id in authorized
        and operation.eligibility == "ELEGIVEL"
        and _responsible_retention_risk(operation)
        and not _responsible_finalization_is_authorized(operation)
    ]


def _revalidate_rules_plan(
    plan: MigrationPlan,
    operation: PlannedOperation,
    client: MigrationBitrixClient,
) -> tuple[str, dict[str, Any] | None, str]:
    extra_select = list(dict.fromkeys(
        str(binding.get("condition_field_id"))
        for binding in operation.rule_bindings
        if binding.get("condition_type") in {"GROUP_ID", "CUSTOM_FIELD"}
        and binding.get("condition_field_id")
    ))
    try:
        raw = client.get_task(operation.task_id, extra_select) if extra_select else client.get_task(operation.task_id)
    except MigrationApiError as exc:
        return _error_result(exc), None, str(exc)
    for binding in operation.rule_bindings:
        condition_values = _binding_condition_values(raw, binding)
        if binding["condition_type"] != "NONE" and condition_values != [binding["condition_value"]]:
            return (
                "DIVERGENTE", raw,
                f"A condição da regra {binding['rule_name']} mudou depois da prévia aprovada.",
            )
    if _integrity_matches(operation, raw):
        return "JA_ATENDIDO", raw, "O patch aprovado já está persistido."
    if _can_edit(raw) is not True:
        return "SEM_PERMISSAO", raw, "A permissão action.edit não foi comprovada."
    if not _baseline_matches(operation, raw):
        return "DIVERGENTE", raw, "Um ou mais vínculos mudaram depois da prévia aprovada."
    return "ELEGIVEL", raw, ""


def _revalidate(plan: MigrationPlan, operation: PlannedOperation, client: MigrationBitrixClient) -> tuple[str, dict[str, Any] | None, str]:
    if plan.migration_mode == "RULE_SET":
        return _revalidate_rules_plan(plan, operation, client)
    try:
        extra_select = [operation.owner_field_id] if operation.owner_field_id else []
        raw = client.get_task(operation.task_id, extra_select) if extra_select else client.get_task(operation.task_id)
    except MigrationApiError as exc:
        return _error_result(exc), None, str(exc)
    live = current_fields(raw)
    target_user_id = operation.target_user_id or plan.target_user_id or ""
    if plan.migration_mode == "OWNER_RULES":
        owner_values = current_property_ids(raw, operation.owner_field_id)
        if owner_values != [operation.owner_id]:
            return (
                "DIVERGENTE", raw,
                f"A propriedade atual em {operation.owner_field_id} não confirma exclusivamente o ID {operation.owner_id}.",
            )
    if _integrity_matches(operation, raw):
        return "JA_ATENDIDO", raw, "O patch já está persistido."
    if _can_edit(raw) is not True:
        return "SEM_PERMISSAO", raw, "A permissão action.edit não foi comprovada."
    if not _baseline_matches(operation, raw):
        return "DIVERGENTE", raw, "Um ou mais vínculos mudaram depois da prévia aprovada."
    for field, before in operation.before.items():
        values = live[field] if isinstance(live[field], list) else [live[field]] if live[field] else []
        if plan.source_user_id not in values:
            if plan.operation_type == "REPLACE_USER" and target_user_id in values:
                return "JA_ATENDIDO", raw, "A origem já foi removida e o destino ocupa o papel."
            return "DIVERGENTE", raw, f"A origem não ocupa mais {field}."
        if plan.operation_type == "ADD_USER" and plan.source_user_id not in operation.patch.get(field, []):
            return "DIVERGENTE", raw, f"O patch ADD_USER não preserva a origem em {field}."
    return "ELEGIVEL", raw, ""


def execute_plan(
    store: MigrationStore,
    plan_id: str,
    client: MigrationBitrixClient,
    *,
    dry_run: bool = True,
    allow_live_writes: bool = False,
    max_tasks: int | None = None,
    expected_operation_type: str | None = None,
    on_progress: Callable[[int, int, str], None] | None = None,
) -> dict[str, Any]:
    plan = store.read_plan(plan_id)
    if plan.logic_version < 5:
        raise MigrationStoreError("O plano usa lógica anterior ao hotfix de integridade; gere uma nova prévia antes de executar.")
    if expected_operation_type and plan.operation_type != expected_operation_type:
        raise MigrationStoreError(
            f"Objetivo incompatível: o plano é {plan.operation_type}, mas a execução solicitou {expected_operation_type}."
        )
    if plan.import_blockers:
        raise MigrationStoreError("O plano possui bloqueios de importação e não pode ser executado.")
    if not plan.task_ids or not any(
        item.eligibility == "ELEGIVEL" and item.task_id in set(plan.task_ids) for item in plan.operations
    ):
        raise MigrationStoreError("O plano não possui tarefas elegíveis após a prévia; revalide ou corrija os bloqueios antes de executar.")
    if not dry_run:
        if not allow_live_writes or os.environ.get("BITRIX24_MIGRATION_WRITES_ENABLED", "").strip() != "YES":
            raise MigrationStoreError("Escritas reais estão bloqueadas por configuração segura.")
        store.valid_approval(plan_id)
    state = store.read_state(plan_id)
    dry_run_checkpoint = store.results_for_mode(state, True)
    live_checkpoint = store.results_for_mode(state, False)
    if not dry_run:
        pending_corrections = [
            task_id for task_id, item in live_checkpoint.items()
            if item.get("plan_digest") == plan.digest and item.get("result") == "CORRECAO_PENDENTE"
        ]
        if pending_corrections:
            raise MigrationStoreError(
                "O plano está bloqueado por correção pendente nas tarefas: "
                + ", ".join(sorted(pending_corrections, key=int))
                + ". Gere um novo plano após a revisão."
            )
    existing = dry_run_checkpoint if dry_run else live_checkpoint
    checkpoint_key = "dry_run_results" if dry_run else "live_results"
    status_key = "dry_run_status" if dry_run else "live_status"
    with store.execution_lock(plan_id):
        running_status = "DRY_RUN_EM_EXECUCAO" if dry_run else "EM_EXECUCAO"
        store.update_state(
            plan_id,
            status=running_status,
            dry_run=dry_run,
            schema_version=2,
            dry_run_results=dry_run_checkpoint,
            live_results=live_checkpoint,
            **{status_key: running_status},
        )
        total = len(plan.operations)
        processed = 0
        paused_for_batch = False
        for index, operation in enumerate(plan.operations, 1):
            if store.read_state(plan_id).get("cancel_requested"):
                store.update_state(
                    plan_id,
                    status="CANCELADA_ENTRE_TAREFAS",
                    results=existing,
                    **{checkpoint_key: existing, status_key: "CANCELADA_ENTRE_TAREFAS"},
                )
                break
            previous = existing.get(operation.task_id, {})
            previous_result = previous.get("result")
            can_skip = previous_result in TERMINAL_RESULTS and previous.get("plan_digest") == plan.digest
            if not dry_run and (previous.get("dry_run") or previous_result == "PREVISTO_DRY_RUN"):
                can_skip = False
            if can_skip:
                if on_progress:
                    on_progress(index, total, operation.task_id)
                continue
            if max_tasks is not None and processed >= max_tasks:
                paused_for_batch = True
                break
            attempt: dict[str, Any] = {
                "at": utc_now(), "plan_id": plan.plan_id, "plan_digest": plan.digest,
                "operation_type": plan.operation_type,
                "task_id": operation.task_id, "roles": operation.roles, "patch": operation.patch,
                "before": operation.before, "dry_run": dry_run,
                "integrity_before": operation.integrity_before,
                "authorized_side_effect_corrections": operation.authorized_side_effect_corrections,
                **_operation_identity(plan, operation),
            }
            if operation.eligibility != "ELEGIVEL" or not operation.patch:
                result = operation.eligibility
                attempt.update(result=result, reason=operation.blocking_reason)
            elif (
                not dry_run
                and _responsible_retention_risk(operation)
                and not _responsible_finalization_is_authorized(operation)
            ):
                attempt.update(
                    result="OPERACAO_NAO_COMPROVADA",
                    reason=(
                        "Reatribuição não executada: o plano não aprovou a lista final de AUDITORS nem a "
                        "finalização restrita do vínculo eventualmente reintroduzido. Gere uma nova prévia."
                    ),
                )
            else:
                result, before_task, reason = _revalidate(plan, operation, client)
                attempt["precheck"] = {
                    "result": result,
                    "reason": reason,
                    "fields": linkage_state(before_task) if before_task else None,
                }
                if result != "ELEGIVEL":
                    attempt.update(result=result, reason=reason)
                elif dry_run:
                    attempt.update(result="PREVISTO_DRY_RUN", reason="Dry-run: nenhuma chamada de escrita foi realizada.")
                else:
                    try:
                        response = client.update_task(operation.task_id, operation.patch)
                        attempt["rest_response"] = sanitized_response(response)
                    except AmbiguousWriteError as exc:
                        attempt["write_error"] = str(exc)
                        try:
                            verified = client.get_task(operation.task_id)
                        except MigrationApiError as verify_exc:
                            attempt.update(result="RESULTADO_INCERTO", reason=f"{exc}; verificação falhou: {verify_exc}")
                        else:
                            status, message, postcheck, effects, corrections = _verify_side_effects(
                                operation, client, verified
                            )
                            attempt.update(
                                result=status,
                                reason=f"Persistência consultada após timeout. {message}",
                                postcheck=postcheck,
                                side_effects=effects,
                                corrections=corrections,
                            )
                            if status == "FALHA_VERIFICACAO":
                                attempt.update(
                                    result="RESULTADO_INCERTO",
                                    reason="Timeout após escrita e estado final incompatível; não repetir automaticamente.",
                                )
                    except MigrationApiError as exc:
                        error_result = _error_result(exc)
                        attempt.update(
                            result="FALHA_ATUALIZACAO" if error_result == "FALHA" else error_result,
                            reason=str(exc),
                        )
                    else:
                        try:
                            verified = client.get_task(operation.task_id)
                        except MigrationApiError as exc:
                            attempt.update(
                                result="RESULTADO_INCERTO",
                                reason=f"Atualização respondeu, mas a releitura falhou; não repetir automaticamente: {exc}",
                            )
                        else:
                            status, message, postcheck, effects, corrections = _verify_side_effects(
                                operation, client, verified
                            )
                            attempt.update(
                                result=status,
                                reason=message,
                                postcheck=postcheck,
                                side_effects=effects,
                                corrections=corrections,
                            )
            store.append_attempt(plan_id, attempt)
            existing[operation.task_id] = {
                "result": attempt["result"], "reason": attempt.get("reason", ""),
                "updated_at": utc_now(), "plan_digest": plan.digest, "dry_run": dry_run,
            }
            processed += 1
            store.update_state(
                plan_id,
                results=existing,
                completed_tasks=index,
                total_tasks=total,
                **{checkpoint_key: existing},
            )
            if not dry_run and attempt["result"] in {"CORRECAO_PENDENTE", "RESULTADO_INCERTO"}:
                store.update_state(
                    plan_id,
                    status="BLOQUEADA_CORRECAO_PENDENTE",
                    results=existing,
                    **{checkpoint_key: existing, status_key: "BLOQUEADA_CORRECAO_PENDENTE"},
                )
                if on_progress:
                    on_progress(index, total, operation.task_id)
                break
            if on_progress:
                on_progress(index, total, operation.task_id)
        else:
            has_unproven = any(
                item.get("plan_digest") == plan.digest
                and item.get("result") == "OPERACAO_NAO_COMPROVADA"
                for item in existing.values()
            )
            completed_status = (
                "DRY_RUN_CONCLUIDO"
                if dry_run
                else "CONCLUIDA_COM_BLOQUEIOS" if has_unproven else "CONCLUIDA"
            )
            store.update_state(
                plan_id,
                status=completed_status,
                results=existing,
                **{checkpoint_key: existing, status_key: completed_status},
            )
        if paused_for_batch:
            store.update_state(
                plan_id,
                status="PAUSADA_LOTE",
                results=existing,
                **{checkpoint_key: existing, status_key: "PAUSADA_LOTE"},
            )
    return build_final_report(store, plan_id)


def report_snapshot(store: MigrationStore, plan_id: str) -> dict[str, Any]:
    plan = store.read_plan(plan_id)
    state = store.read_state(plan_id)
    attempts = store.read_attempts(plan_id)
    dry_run_results = store.results_for_mode(state, True)
    live_results = store.results_for_mode(state, False)

    def result_counts(results: dict[str, Any]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in results.values():
            status = item.get("result", "PENDENTE_VERIFICACAO")
            counts[status] = counts.get(status, 0) + 1
        return counts

    dry_counts = result_counts(dry_run_results)
    live_counts = result_counts(live_results)
    report = {
        "schema_version": 2,
        "generated_at": utc_now(),
        "plan": plan.to_dict(),
        "approval": store.read_optional(plan_id, "approval.json"),
        "state": state,
        "checkpoints": {
            "dry_run": dry_run_results,
            "live": live_results,
        },
        "summary": {
            "eligible": len(plan.task_ids),
            "updated_verified": (
                live_counts.get("ATUALIZADO_VERIFICADO", 0)
                + live_counts.get("ATUALIZADA_E_VERIFICADA", 0)
            ),
            "already_satisfied": live_counts.get("JA_ATENDIDO", 0),
            "ignored": live_counts.get("IGNORADO", 0) + sum(row.eligibility == "IGNORADO" for row in plan.preview),
            "not_supported": sum(row.eligibility == "NAO_SUPORTADO" for row in plan.preview),
            "permission_denied": live_counts.get("SEM_PERMISSAO", 0) + sum(
                row.eligibility == "SEM_PERMISSAO" for row in plan.preview
            ),
            "divergent": live_counts.get("DIVERGENTE", 0) + sum(
                row.eligibility == "DIVERGENTE" for row in plan.preview
            ),
            "conflicts": sum(row.eligibility == "CONFLITO" for row in plan.preview),
            "side_effects_detected": sum(
                bool(item.get("side_effects")) for item in attempts if not item.get("dry_run")
            ),
            "correction_pending": live_counts.get("CORRECAO_PENDENTE", 0),
            "unproven_operation": live_counts.get("OPERACAO_NAO_COMPROVADA", 0),
            "failed_update": live_counts.get("FALHA_ATUALIZACAO", 0),
            "failed_verification": live_counts.get("FALHA_VERIFICACAO", 0),
            "failed": (
                live_counts.get("FALHA", 0)
                + live_counts.get("FALHA_ATUALIZACAO", 0)
                + live_counts.get("FALHA_VERIFICACAO", 0)
            ),
            "pending_verification": live_counts.get("PENDENTE_VERIFICACAO", 0) + live_counts.get("RESULTADO_INCERTO", 0),
            "dry_run": dry_counts.get("PREVISTO_DRY_RUN", 0),
            "dry_run_divergent": dry_counts.get("DIVERGENTE", 0),
            "dry_run_failed": dry_counts.get("FALHA", 0) + dry_counts.get("RESULTADO_INCERTO", 0),
        },
        "attempts": attempts,
    }
    return report


def build_final_report(store: MigrationStore, plan_id: str) -> dict[str, Any]:
    report = report_snapshot(store, plan_id)
    plan = store.read_plan(plan_id)
    attempts = report["attempts"]
    directory = store.plan_dir(plan_id)
    export_json(directory / "relatorio_migracao.json", report)
    headers = [
        "task_id", "rule_ids", "rule_names", "source_user_ids", "target_user_ids",
        "operation_type", "roles", "affected_fields", "result", "reason", "dry_run", "at",
        "before", "integrity_before", "patch", "precheck", "postcheck", "side_effects", "corrections",
    ]
    rows = [[item.get(key) for key in headers] for item in attempts]
    preview_headers = list(asdict(plan.preview[0]).keys()) if plan.preview else []
    preview_rows = [[asdict(row).get(key) for key in preview_headers] for row in plan.preview]
    export_xlsx(directory / "relatorio_migracao.xlsx", [
        ("Resultados", headers, rows),
        ("Prévia Aprovada", preview_headers, preview_rows),
        ("Resumo", ["Indicador", "Valor"], [[key, value] for key, value in report["summary"].items()]),
    ])
    MigrationStore._atomic_json(directory / "final.json", report)
    return report
