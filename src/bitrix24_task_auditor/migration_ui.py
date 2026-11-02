from __future__ import annotations

import hashlib
import os
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlsplit

import streamlit as st

from .config import AppConfig, ConfigurationError, webhook_from_environment
from .migration_client import MigrationApiError, MigrationBitrixClient
from .migration_importer import AuditImportError, import_audit_xlsx
from .migration_models import (
    MigrationValidationError, OPERATION_LABELS, ROLE_LABELS, OwnerRule, create_plan,
)
from .migration_rule_engine import create_rules_plan
from .migration_rules import (
    CONDITION_TYPES, RULE_OPERATIONS, MigrationRule, MigrationRuleError, MigrationRuleStore,
)
from .migration_service import execute_plan, preflight_plan, report_snapshot, unverified_responsible_task_ids
from .migration_store import MigrationStore, MigrationStoreError
from .run_store import RunStore


FLOW_VERSION = 4
TERMINAL_RESULTS = {
    "ATUALIZADO_VERIFICADO", "JA_ATENDIDO", "IGNORADO", "NAO_SUPORTADO",
    "DIVERGENTE", "SEM_PERMISSAO", "NAO_ACESSIVEL", "RESULTADO_INCERTO",
    "PREVISTO_DRY_RUN", "ATUALIZADA_E_VERIFICADA", "CORRECAO_PENDENTE",
    "EFEITO_COLATERAL_DETECTADO", "FALHA_ATUALIZACAO", "FALHA_VERIFICACAO",
    "OPERACAO_NAO_COMPROVADA",
}


def _audit_files(root: Path) -> list[Path]:
    candidates = list((root / "output").glob("auditoria_*.xlsx"))
    for run in RunStore(root).list_runs():
        relative = run.get("result", {}).get("xlsx")
        if not relative:
            continue
        path = (root / str(relative)).resolve()
        try:
            path.relative_to(root.resolve())
        except ValueError:
            continue
        if path.is_file() and path.suffix.casefold() == ".xlsx":
            candidates.append(path)
    unique = {path.resolve(): path.resolve() for path in candidates}
    return sorted(unique.values(), key=lambda item: item.stat().st_mtime, reverse=True)


def _client(config: AppConfig) -> MigrationBitrixClient:
    return MigrationBitrixClient(webhook_from_environment(config.bitrix), config.bitrix)


def _normalized_id(value: object) -> str | None:
    text = str(value or "").strip()
    return str(int(text)) if text.isdigit() and int(text) > 0 else None


def _source_hash(selected_path: Path | None, uploaded: object | None) -> str | None:
    try:
        if selected_path is not None:
            return hashlib.sha256(selected_path.read_bytes()).hexdigest()
        if uploaded is not None:
            return hashlib.sha256(uploaded.getvalue()).hexdigest()
    except (OSError, AttributeError):
        return None
    return None


def _render_rule_manager(root: Path) -> MigrationRuleStore:
    catalog = MigrationRuleStore(root)
    rules = catalog.list_rules()
    with st.expander("Gerenciar Regras", expanded=False):
        if rules:
            st.dataframe([{
                "ID": rule.rule_id,
                "Versão": rule.version,
                "Nome": rule.name,
                "Ativa": rule.active,
                "Origem": rule.source_user_id,
                "Operação": RULE_OPERATIONS[rule.operation]["label"],
                "Destino": rule.target_user_id,
                "Condição": CONDITION_TYPES[rule.condition_type],
                "Campo": rule.condition_field_id or "",
                "Valor": rule.condition_value or "",
            } for rule in rules], use_container_width=True, hide_index=True)
        else:
            st.info("Nenhuma regra cadastrada.")

        options = ["__new__", *[rule.rule_id for rule in rules]]
        selected = st.selectbox(
            "Criar ou editar regra",
            options,
            format_func=lambda value: "Criar nova regra" if value == "__new__" else next(
                rule.name for rule in rules if rule.rule_id == value
            ),
            key="rule_manager_selected",
        )
        current = next((rule for rule in rules if rule.rule_id == selected), None)
        editor_key = "new" if current is None else f"{current.rule_id}_{current.version}"
        with st.container(border=True):
            name = st.text_input("Nome da regra", value=current.name if current else "", key=f"rule_name_{editor_key}")
            description = st.text_area(
                "Descrição opcional", value=current.description if current else "", key=f"rule_description_{editor_key}"
            )
            c1, c2 = st.columns(2)
            source_id = c1.text_input(
                "ID do usuário de origem", value=current.source_user_id if current else "", key=f"rule_source_{editor_key}"
            )
            target_id = c2.text_input(
                "ID do usuário de destino", value=current.target_user_id if current else "", key=f"rule_target_{editor_key}"
            )
            operation_options = list(RULE_OPERATIONS)
            operation = st.selectbox(
                "Operação",
                operation_options,
                index=operation_options.index(current.operation) if current else 0,
                format_func=lambda value: RULE_OPERATIONS[value]["label"],
                key=f"rule_operation_{editor_key}",
            )
            condition_options = list(CONDITION_TYPES)
            condition_type = st.selectbox(
                "Condição adicional",
                condition_options,
                index=condition_options.index(current.condition_type) if current else 0,
                format_func=lambda value: CONDITION_TYPES[value],
                key=f"rule_condition_{editor_key}",
            )
            condition_field = ""
            condition_value = ""
            if condition_type == "CUSTOM_FIELD":
                condition_field = st.text_input(
                    "ID do campo personalizado (UF_*)",
                    value=current.condition_field_id if current and current.condition_type == "CUSTOM_FIELD" else "",
                    key=f"rule_custom_field_{editor_key}",
                )
            if condition_type != "NONE":
                condition_value = st.text_input(
                    "Valor exato da condição",
                    value=current.condition_value if current and current.condition_type == condition_type else "",
                    key=f"rule_condition_value_{editor_key}_{condition_type}",
                )
            active = st.checkbox(
                "Regra ativa", value=current.active if current else True, key=f"rule_active_{editor_key}"
            )
            save = st.button("Salvar regra", type="primary", key=f"save_rule_{editor_key}")
        if save:
            try:
                values = {
                    "name": name,
                    "description": description,
                    "source_user_id": source_id,
                    "operation": operation,
                    "target_user_id": target_id,
                    "condition_type": condition_type,
                    "condition_field_id": condition_field or None,
                    "condition_value": condition_value or None,
                    "active": active,
                    "cleanup_previous_responsible_auditor": False,
                }
                if current:
                    catalog.edit(current.rule_id, **values)
                else:
                    catalog.create(**values)
                st.rerun()
            except MigrationRuleError as exc:
                st.error(str(exc))

        if current:
            a1, a2, a3 = st.columns(3)
            if a1.button("Duplicar regra", key=f"duplicate_{current.rule_id}"):
                try:
                    catalog.duplicate(current.rule_id)
                    st.rerun()
                except MigrationRuleError as exc:
                    st.error(str(exc))
            toggle_label = "Desativar regra" if current.active else "Ativar regra"
            if a2.button(toggle_label, key=f"toggle_{current.rule_id}"):
                try:
                    catalog.set_active(current.rule_id, not current.active)
                    st.rerun()
                except MigrationRuleError as exc:
                    st.error(str(exc))
            if a3.button("Excluir regra", key=f"delete_{current.rule_id}"):
                try:
                    catalog.delete(current.rule_id)
                    st.rerun()
                except MigrationRuleError as exc:
                    st.error(str(exc))

        st.markdown("**Modelos de regras**")
        template_name = st.text_input("Nome do modelo", key="new_rule_template_name")
        template_rules = st.multiselect(
            "Regras do modelo",
            [rule.rule_id for rule in rules],
            format_func=lambda value: next(rule.name for rule in rules if rule.rule_id == value),
            key="new_rule_template_rules",
        )
        if st.button("Salvar conjunto como modelo", key="save_rule_template"):
            try:
                catalog.save_template(template_name, template_rules)
                st.rerun()
            except MigrationRuleError as exc:
                st.error(str(exc))
    return catalog


def _configuration_matches(
    plan,
    *,
    spreadsheet_sha256: str | None,
    source_user_id: object,
    target_user_id: object,
    operation_type: str,
    roles: list[str],
    migration_mode: str = "SIMPLE",
    owner_field_id: str | None = None,
    owner_rules: list[OwnerRule] | None = None,
    rule_task_limit: int | None = None,
    selected_rules: list[MigrationRule] | None = None,
    allow_responsible_auditor_cleanup: bool = False,
) -> bool:
    base_matches = (
        spreadsheet_sha256 == plan.spreadsheet_sha256
        and operation_type == plan.operation_type
        and list(dict.fromkeys(roles)) == plan.selected_roles
        and migration_mode == plan.migration_mode
    )
    if not base_matches:
        return False
    if migration_mode == "RULE_SET":
        return [rule.snapshot() for rule in (selected_rules or [])] == plan.rule_snapshots
    if migration_mode == "OWNER_RULES":
        current_rules = [
            {
                "rule_id": item.rule_id.strip().upper(),
                "owner_label": item.owner_label.strip(),
                "owner_id": _normalized_id(item.owner_id),
                "target_user_id": _normalized_id(item.target_user_id),
            }
            for item in (owner_rules or [])
        ]
        return (
            str(owner_field_id or "").upper() == (plan.owner_field_id or "")
            and current_rules == [asdict(item) for item in plan.owner_rules]
            and rule_task_limit == plan.rule_task_limit
            and _normalized_id(source_user_id) == plan.source_user_id
        )
    return (
        _normalized_id(source_user_id) == plan.source_user_id
        and _normalized_id(target_user_id) == plan.target_user_id
        and bool(allow_responsible_auditor_cleanup)
        == bool(plan.side_effect_policy.get("remove_new_previous_responsible_from_auditors", False))
    )


def _preview_rows(plan, host: str | None) -> list[dict]:
    rows = []
    operations = {operation.task_id: operation for operation in plan.operations}
    for item in plan.preview:
        value = asdict(item)
        operation = operations.get(item.task_id)
        rows.append({
            "ID": item.task_id,
            "Link": f"https://{host}/company/personal/user/0/tasks/task/view/{item.task_id}/" if host else "",
            "Título": item.title,
            "Operação": OPERATION_LABELS.get(item.operation_type, item.operation_type),
            "Papel": ROLE_LABELS.get(item.role, item.role),
            "Origem": item.current_user,
            "Destino": item.target_user,
            "Regra aplicada": item.rule_name or item.owner_label,
            "Campo da condição": item.condition_field_id or item.owner_field_id,
            "Condição esperada": item.condition_value,
            "Condição auditada": item.audited_owner_value,
            "Condição atual": item.current_condition_value or item.current_owner_value,
            "Efeito no papel": item.role_disposition or item.source_disposition,
            "Remoção total da tarefa": "NÃO COMPROVADA NA PRÉVIA",
            "Participantes atuais": item.current_accomplices,
            "Observadores atuais": item.current_auditors,
            "Observadores finais aprovados": (
                operation.patch.get("AUDITORS", item.current_auditors)
                if operation else item.current_auditors
            ),
            "Finalização lógica autorizada": item.side_effect_correction_authorized,
            "Criador atual": item.current_creator,
            "Outros vínculos da origem": item.source_other_roles,
            "Política de preservação": item.preservation_policy,
            "Valor auditado": value.get("audited_value"),
            "Lista atual": item.previous_value,
            "Lista proposta": item.proposed_value,
            "Elegibilidade": item.eligibility,
            "Motivo": item.blocking_reason,
        })
    return rows


def _eligibility_counts(plan) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in plan.preview:
        counts[row.eligibility] = counts.get(row.eligibility, 0) + 1
    return counts


def _execution_blockers(
    plan,
    state: dict,
    *,
    dry_run: bool,
    writes_enabled: bool,
    approval_error: str,
    requested_operation_type: str | None = None,
) -> list[str]:
    blockers = list(plan.import_blockers)
    if requested_operation_type != plan.operation_type:
        blockers.append("A operação configurada não corresponde à operação exibida na prévia.")
    if plan.logic_version < 5 or not plan.preflight_at:
        blockers.append("A prévia não foi validada pela lógica atual; gere uma nova prévia.")
    if plan.migration_mode in {"OWNER_RULES", "RULE_SET"}:
        target_ids = (
            {rule.target_user_id for rule in plan.owner_rules}
            if plan.migration_mode == "OWNER_RULES"
            else {str(rule.get("target_user_id")) for rule in plan.rule_snapshots}
        )
        inactive = [target_id for target_id in target_ids if not plan.target_users.get(target_id, {}).get("active")]
        if not target_ids or inactive:
            blockers.append("Todos os destinatários das regras precisam ser confirmados como usuários ativos.")
    elif plan.migration_mode == "SIDE_EFFECT_CORRECTION":
        pass
    elif not plan.target_user or not plan.target_user.get("active"):
        blockers.append("O usuário de destino não foi confirmado como ativo.")
    if not plan.task_ids:
        detail = ", ".join(f"{key}: {value}" for key, value in sorted(_eligibility_counts(plan).items())) or "sem vínculos"
        blockers.append(f"Nenhuma tarefa está elegível ({detail}).")
    if not dry_run and approval_error:
        blockers.append(f"Confirmação ausente ou inválida: {approval_error}")
    if state.get("cancel_requested"):
        blockers.append("A execução está cancelada entre tarefas; use Retomar para continuar.")
    if not dry_run and not writes_enabled:
        blockers.append("A escrita real está bloqueada pela configuração segura do ambiente.")
    unverified_tasks = set(unverified_responsible_task_ids(plan))
    eligible_tasks = {
        operation.task_id
        for operation in plan.operations
        if operation.eligibility == "ELEGIVEL" and operation.task_id in set(plan.task_ids)
    }
    if not dry_run and eligible_tasks and eligible_tasks <= unverified_tasks:
        blockers.append(
            "As operações elegíveis dependem de reatribuição de responsável ainda não homologada para preservar "
            "AUDITORS neste portal. Nenhuma escrita será enviada; operações de participante e observador continuam disponíveis."
        )

    results = MigrationStore.results_for_mode(state, dry_run)
    if not dry_run:
        correction_pending = [
            task_id for task_id in plan.task_ids
            if results.get(task_id, {}).get("plan_digest") == plan.digest
            and results.get(task_id, {}).get("result") == "CORRECAO_PENDENTE"
        ]
        if correction_pending:
            blockers.append(
                "Execução bloqueada por efeito colateral pendente nas tarefas: "
                + ", ".join(correction_pending)
                + ". Revise o relatório e gere um novo plano com autorização específica, se aplicável."
            )
    pending = []
    for task_id in plan.task_ids:
        result = results.get(task_id, {})
        completed = (
            result.get("plan_digest") == plan.digest
            and bool(result.get("dry_run")) == dry_run
            and result.get("result") in TERMINAL_RESULTS
        )
        if not completed:
            pending.append(task_id)
    if plan.task_ids and not pending:
        if dry_run:
            blockers.append("A simulação deste plano já foi concluída.")
        else:
            blockers.append("Todas as operações reais deste plano já foram concluídas.")
    return list(dict.fromkeys(blockers))


def _show_results(store: MigrationStore, plan_id: str) -> None:
    try:
        report = report_snapshot(store, plan_id)
    except (OSError, MigrationStoreError, ValueError) as exc:
        st.error(f"Não foi possível ler o resultado: {exc}")
        return
    summary = report["summary"]
    labels = [
        ("Elegíveis", "eligible"), ("Atualizadas e verificadas", "updated_verified"),
        ("Já atendidas", "already_satisfied"), ("Dry-run", "dry_run"),
        ("Não suportadas", "not_supported"), ("Sem permissão", "permission_denied"),
        ("Divergências", "divergent"), ("Efeitos detectados", "side_effects_detected"),
        ("Correções pendentes", "correction_pending"), ("Falhas de atualização", "failed_update"),
        ("Falhas de verificação", "failed_verification"), ("Resultados incertos", "pending_verification"),
    ]
    for start in range(0, len(labels), 4):
        columns = st.columns(4)
        for column, (label, key) in zip(columns, labels[start:start + 4]):
            value = summary.get(key, 0)
            column.metric(label, value)
    attempts = report.get("attempts", [])
    if attempts:
        st.dataframe(attempts, use_container_width=True, hide_index=True)
    directory = store.plan_dir(plan_id)
    xlsx = directory / "relatorio_migracao.xlsx"
    json_path = directory / "relatorio_migracao.json"
    c1, c2 = st.columns(2)
    if xlsx.exists():
        c1.download_button("Baixar relatório Excel", xlsx.read_bytes(), file_name=f"migracao_{plan_id}.xlsx")
    if json_path.exists():
        c2.download_button(
            "Baixar relatório JSON", json_path.read_bytes(),
            file_name=f"migracao_{plan_id}.json", mime="application/json",
        )


def render_migration(root: Path, config: AppConfig) -> None:
    store = MigrationStore(root)
    st.title("Migração de Vínculos")
    st.caption("CONFIGURAR → VALIDAR → SIMULAR → CONFIRMAR → EXECUTAR REAL → VERIFICAR")
    writes_enabled = os.environ.get("BITRIX24_MIGRATION_WRITES_ENABLED", "").strip() == "YES"
    if writes_enabled:
        st.warning("A trava de escrita do ambiente está liberada. Use execução real somente com autorização específica.")
    else:
        st.info("Modo seguro: escritas reais estão bloqueadas; a execução disponível é dry-run.")

    if st.session_state.get("migration_flow_version") != FLOW_VERSION:
        st.session_state.pop("active_migration_plan", None)
        st.session_state["migration_flow_version"] = FLOW_VERSION

    st.subheader("1. Configurar")
    existing = _audit_files(root)
    source_mode = st.radio("Fonte da auditoria", ["Auditoria existente", "Enviar Excel"], horizontal=True)
    selected_path: Path | None = None
    uploaded = None
    if source_mode == "Auditoria existente":
        if existing:
            selected_path = st.selectbox("Planilha", existing, format_func=lambda path: path.name)
        else:
            st.warning("Nenhuma planilha de auditoria foi encontrada em output/.")
    else:
        uploaded = st.file_uploader("Arquivo do auditor (.xlsx)", type=["xlsx"], accept_multiple_files=False)

    rule_catalog = _render_rule_manager(root)
    strategy = st.selectbox(
        "Tipo de migração",
        ["SIMPLE", "RULE_SET"],
        format_func=lambda value: "Migração simples" if value == "SIMPLE" else "Migração por regras",
        key="migration_strategy",
    )
    inspected_audit = None
    try:
        if source_mode == "Auditoria existente" and selected_path is not None:
            inspected_audit = import_audit_xlsx(selected_path)
        elif source_mode == "Enviar Excel" and uploaded is not None:
            inspected_audit = import_audit_xlsx(uploaded.getvalue(), filename=uploaded.name)
    except (AuditImportError, OSError) as exc:
        st.error(f"A planilha não pôde ser inspecionada: {exc}")

    owner_field_id = None
    owner_rules: list[OwnerRule] = []
    rule_task_limit = None
    selected_rules: list[MigrationRule] = []
    allow_responsible_auditor_cleanup = False
    if strategy == "SIMPLE":
        c1, c2 = st.columns(2)
        source_id = c1.text_input("ID do colaborador de origem", key="migration_source_id")
        target_id = c2.text_input("ID do colaborador de destino", key="migration_target_id")
        operation = st.selectbox(
            "Operação",
            ["ADD_USER", "REPLACE_USER"],
            format_func=lambda value: OPERATION_LABELS[value],
            key="migration_operation_type_linear",
        )
        role_options = ["accomplice", "auditor"] if operation == "ADD_USER" else ["responsible", "accomplice", "auditor", "creator"]
        roles = st.multiselect(
            "Papéis",
            role_options,
            default=["accomplice", "auditor"] if operation == "ADD_USER" else ["responsible"],
            format_func=lambda role: ROLE_LABELS[role],
            key=f"migration_roles_linear_{operation}",
        )
    else:
        source_id = ""
        target_id = ""
        operation = "RULE_SET"
        active_rules = rule_catalog.list_rules(active_only=True)
        templates = rule_catalog.list_templates()
        if templates:
            template_id = st.selectbox(
                "Modelo de regras",
                ["", *[item["template_id"] for item in templates]],
                format_func=lambda value: "— selecionar modelo —" if not value else next(
                    item["name"] for item in templates if item["template_id"] == value
                ),
                key="selected_rule_template",
            )
            if st.button("Carregar modelo", disabled=not template_id):
                template = next(item for item in templates if item["template_id"] == template_id)
                active_ids = {rule.rule_id for rule in active_rules}
                st.session_state["migration_selected_rules"] = [
                    rule_id for rule_id in template["rule_ids"] if rule_id in active_ids
                ]
                st.rerun()
        active_ids = [rule.rule_id for rule in active_rules]
        if "migration_selected_rules" in st.session_state:
            st.session_state["migration_selected_rules"] = [
                rule_id for rule_id in st.session_state["migration_selected_rules"] if rule_id in active_ids
            ]
        selected_rule_ids = st.multiselect(
            "Regras desta migração",
            active_ids,
            format_func=lambda value: next(rule.name for rule in active_rules if rule.rule_id == value),
            key="migration_selected_rules",
        )
        selected_rules = [next(rule for rule in active_rules if rule.rule_id == rule_id) for rule_id in selected_rule_ids]
        roles = sorted({RULE_OPERATIONS[rule.operation]["role"] for rule in selected_rules})
        if not active_rules:
            st.warning("Crie e ative ao menos uma regra no gerenciador.")
        elif not selected_rules:
            st.info("Selecione uma ou mais regras ativas para gerar a prévia.")
        if inspected_audit and selected_rules:
            missing_fields = sorted({
                rule.condition_field_id
                for rule in selected_rules
                if rule.condition_type in {"GROUP_ID", "CUSTOM_FIELD"}
                and rule.condition_field_id not in inspected_audit.property_fields
            })
            if missing_fields:
                st.error("A planilha não mapeia os campos exigidos: " + ", ".join(missing_fields))

    if st.button("Gerar nova prévia", type="primary"):
        try:
            if source_mode == "Auditoria existente":
                if selected_path is None:
                    raise AuditImportError("Selecione uma planilha.")
                imported = import_audit_xlsx(selected_path)
            else:
                if uploaded is None:
                    raise AuditImportError("Envie uma planilha .xlsx.")
                imported = import_audit_xlsx(uploaded.getvalue(), filename=uploaded.name)
            if strategy == "RULE_SET":
                plan = create_rules_plan(imported, selected_rules)
            else:
                plan = create_plan(
                    imported, source_id, target_id, roles, operation_type=operation,
                    allow_responsible_auditor_cleanup=allow_responsible_auditor_cleanup,
                )
            if not imported.blockers and plan.operations:
                progress = st.progress(0.0, text="Consultando o estado atual das tarefas")
                plan = preflight_plan(
                    plan,
                    _client(config),
                    on_progress=lambda done, total, task_id: progress.progress(
                        min(done / max(total, 1), 1.0), text=f"Tarefa {task_id} · {done}/{total}"
                    ),
                )
            store.create(plan)
            if strategy == "RULE_SET":
                rule_catalog.mark_used(selected_rules, plan.plan_id)
            store.update_state(
                plan.plan_id,
                status="PREVIA_PRONTA" if plan.task_ids else "PREVIA_COM_BLOQUEIOS",
            )
            st.session_state["active_migration_plan"] = plan.plan_id
            st.session_state["migration_flow_version"] = FLOW_VERSION
            st.rerun()
        except (
            AuditImportError, MigrationValidationError, MigrationRuleError, MigrationApiError,
            MigrationStoreError, ConfigurationError, OSError,
        ) as exc:
            st.error(str(exc))

    plan_id = st.session_state.get("active_migration_plan")
    if not plan_id:
        st.info("Configure os dados e gere uma nova prévia. Planos antigos permanecem apenas no histórico interno.")
        return
    try:
        plan = store.read_plan(plan_id)
        state = store.read_state(plan_id)
    except MigrationStoreError as exc:
        st.session_state.pop("active_migration_plan", None)
        st.error(f"A prévia atual não pôde ser carregada: {exc}. Gere uma nova prévia.")
        return

    current_hash = _source_hash(selected_path, uploaded)
    if not _configuration_matches(
        plan,
        spreadsheet_sha256=current_hash,
        source_user_id=source_id,
        target_user_id=target_id,
        operation_type=operation,
        roles=roles,
        migration_mode=strategy,
        owner_field_id=owner_field_id,
        owner_rules=owner_rules,
        rule_task_limit=rule_task_limit,
        selected_rules=selected_rules,
        allow_responsible_auditor_cleanup=allow_responsible_auditor_cleanup,
    ):
        st.session_state.pop("active_migration_plan", None)
        st.warning("A configuração mudou. A prévia anterior foi retirada da sessão; gere uma nova prévia para confirmar os novos dados.")
        return

    st.subheader("2. Prévia")
    for message in plan.import_blockers:
        st.error(message)
    for message in plan.import_warnings:
        st.warning(message)
    st.caption(
        f"Arquivo: {plan.spreadsheet_filename} · SHA-256: `{plan.spreadsheet_sha256}` · "
        f"Plano novo desta sessão: `{plan.plan_id}`"
    )
    if plan.migration_mode == "OWNER_RULES":
        st.info(
            f"Campo confirmado: {plan.owner_field_label} ({plan.owner_field_id}) · "
            f"limite de {plan.rule_task_limit} tarefa(s) por regra."
        )
    elif plan.migration_mode == "RULE_SET":
        st.info(
            "Regras imutáveis deste plano: " + ", ".join(
                f"{item['name']} (v{item['version']})" for item in plan.rule_snapshots
            )
        )
    metrics = st.columns(4)
    metrics[0].metric("Tarefas na planilha", plan.audit_task_count or 0)
    metrics[1].metric("Vínculos encontrados", plan.matched_link_count or 0)
    metrics[2].metric("Tarefas elegíveis", len(plan.task_ids))
    metrics[3].metric(
        "Bloqueios",
        sum(row.eligibility in {"DIVERGENTE", "CONFLITO", "BLOQUEADO", "SEM_PERMISSAO"} for row in plan.preview),
    )

    host = None
    try:
        host = urlsplit(webhook_from_environment(config.bitrix)).hostname
    except ConfigurationError:
        pass
    st.dataframe(
        _preview_rows(plan, host), use_container_width=True, hide_index=True,
        column_config={"Link": st.column_config.LinkColumn("Link da tarefa", display_text="Abrir")},
    )
    counts = _eligibility_counts(plan)
    if counts:
        st.write("Estados da prévia:", counts)
    if not plan.task_ids:
        st.error("Não existem operações elegíveis. A execução permanece desabilitada; corrija os motivos mostrados na tabela e gere uma nova prévia.")
        return
    unverified_tasks = unverified_responsible_task_ids(plan)
    if unverified_tasks:
        st.warning(
            f"{len(unverified_tasks)} tarefa(s) têm reatribuição de responsável ainda não homologada quanto ao estado "
            "final de observadores. Essas tarefas não serão escritas; outros modos elegíveis do plano continuam disponíveis."
        )

    dry_results = store.results_for_mode(state, True)
    dry_plan_results = {
        task_id: result for task_id, result in dry_results.items()
        if result.get("plan_digest") == plan.digest
    }
    simulation_complete = all(
        dry_plan_results.get(task_id, {}).get("result") in TERMINAL_RESULTS
        for task_id in plan.task_ids
    )
    simulation_apt = sum(
        dry_plan_results.get(task_id, {}).get("result") == "PREVISTO_DRY_RUN"
        for task_id in plan.task_ids
    )

    st.subheader("3. Simular")
    simulation_blockers = _execution_blockers(
        plan,
        state,
        dry_run=True,
        writes_enabled=writes_enabled,
        approval_error="",
        requested_operation_type=operation,
    )
    if simulation_complete:
        st.success(f"Simulação concluída — {simulation_apt} tarefas aptas.")
        affected = len(plan.task_ids) - simulation_apt
        if affected:
            st.warning(f"{affected} tarefa(s) tiveram bloqueio ou já estavam atendidas durante a simulação; consulte o resultado.")
    else:
        for blocker in simulation_blockers:
            st.error(blocker)
        if state.get("cancel_requested") and st.button("Retomar simulação"):
            store.clear_cancel(plan_id)
            st.rerun()
        simulate_label = "Continuar simulação" if state.get("dry_run_status") == "PAUSADA_LOTE" else "Simular"
        if st.button(simulate_label, type="primary", disabled=bool(simulation_blockers)):
            progress = st.progress(0.0, text="Simulando lote")
            try:
                execute_plan(
                    store,
                    plan_id,
                    _client(config),
                    dry_run=True,
                    allow_live_writes=False,
                    max_tasks=10,
                    expected_operation_type=operation,
                    on_progress=lambda done, total, task_id: progress.progress(
                        min(done / max(total, 1), 1.0), text=f"Tarefa {task_id} · {done}/{total}"
                    ),
                )
                st.rerun()
            except (MigrationStoreError, MigrationApiError, ConfigurationError, OSError, ValueError, TypeError, KeyError) as exc:
                st.error(f"Simulação não iniciada: {type(exc).__name__}: {exc}")
        st.subheader("5. Resultado")
        _show_results(store, plan_id)
        return

    source_pending_corrections = [
        task_id for task_id in plan.task_ids
        if store.results_for_mode(state, False).get(task_id, {}).get("plan_digest") == plan.digest
        and store.results_for_mode(state, False).get(task_id, {}).get("result") == "CORRECAO_PENDENTE"
    ]
    if source_pending_corrections:
        st.subheader("4. Resultado bloqueado")
        _show_results(store, plan_id)
        st.error(
            "O plano permanece bloqueado por efeito colateral. Não há correção pontual automática "
            "nem retomada do lote pela interface."
        )
        return

    st.subheader("4. Confirmar e executar")
    execution_mode = st.radio(
        "Próxima ação",
        ["Revisar simulação", "Execução real"],
        horizontal=True,
        key=f"execution_mode_{plan_id}",
    )
    if execution_mode != "Execução real":
        st.info("Selecione Execução real quando estiver pronto para usar a confirmação vinculada a esta prévia.")
        st.subheader("5. Resultado")
        _show_results(store, plan_id)
        return

    if not writes_enabled:
        st.error(
            "Execução real bloqueada: BITRIX24_MIGRATION_WRITES_ENABLED não está habilitada. "
            "A aplicação não altera essa configuração automaticamente."
        )
    try:
        approval = store.valid_approval(plan_id)
        approval_error = ""
    except MigrationStoreError as exc:
        approval = None
        approval_error = str(exc)
    if approval is None:
        st.warning(
            f"Confirme uma única vez esta prévia para execução real: {len(plan.task_ids)} tarefa(s), "
            f"operação {OPERATION_LABELS.get(plan.operation_type, 'Conjunto de regras')}, papéis "
            f"{', '.join(ROLE_LABELS[role] for role in plan.selected_roles)}."
        )
        if st.button("Confirmar esta prévia para execução real", type="primary"):
            try:
                store.approve(plan_id, store.confirmation_phrase(plan))
                st.rerun()
            except MigrationStoreError as exc:
                st.error(f"A confirmação não foi registrada: {exc}")
        st.subheader("5. Resultado")
        _show_results(store, plan_id)
        return
    st.success(f"Execução real autorizada em {approval['approved_at']} exclusivamente para este plano e seus patches.")

    live_results = store.results_for_mode(state, False)
    live_plan_results = {
        task_id: result for task_id, result in live_results.items()
        if result.get("plan_digest") == plan.digest
    }
    live_complete = all(
        live_plan_results.get(task_id, {}).get("result") in TERMINAL_RESULTS
        for task_id in plan.task_ids
    )
    updated_verified = sum(
        live_plan_results.get(task_id, {}).get("result") in {"ATUALIZADO_VERIFICADO", "ATUALIZADA_E_VERIFICADA"}
        for task_id in plan.task_ids
    )
    correction_pending = [
        task_id for task_id in plan.task_ids
        if live_plan_results.get(task_id, {}).get("result") == "CORRECAO_PENDENTE"
    ]
    unproven_operations = [
        task_id for task_id in plan.task_ids
        if live_plan_results.get(task_id, {}).get("result") == "OPERACAO_NAO_COMPROVADA"
    ]
    if correction_pending:
        st.error(
            "Execução interrompida por efeito colateral não resolvido nas tarefas: "
            + ", ".join(correction_pending)
            + ". Nenhum novo lote será iniciado com este plano."
        )
    elif live_complete and unproven_operations:
        st.warning(
            f"Execução concluída com bloqueios — {updated_verified} tarefa(s) atualizadas e verificadas; "
            f"{len(unproven_operations)} reatribuição(ões) não executada(s) por falta de homologação."
        )
    elif live_complete:
        st.success(f"Execução real concluída — {updated_verified} tarefas atualizadas e verificadas.")
    else:
        real_blockers = _execution_blockers(
            plan,
            state,
            dry_run=False,
            writes_enabled=writes_enabled,
            approval_error=approval_error,
            requested_operation_type=operation,
        )
        for blocker in real_blockers:
            st.error(blocker)
        if state.get("cancel_requested") and st.button("Retomar execução real"):
            store.clear_cancel(plan_id)
            st.rerun()
        execute_label = "Continuar execução real" if state.get("live_status") == "PAUSADA_LOTE" else "Executar migração real"
        if st.button(execute_label, type="primary", disabled=bool(real_blockers)):
            progress = st.progress(0.0, text="Executando lote real")
            try:
                execute_plan(
                    store,
                    plan_id,
                    _client(config),
                    dry_run=False,
                    allow_live_writes=writes_enabled,
                    max_tasks=10,
                    expected_operation_type=operation,
                    on_progress=lambda done, total, task_id: progress.progress(
                        min(done / max(total, 1), 1.0), text=f"Tarefa {task_id} · {done}/{total}"
                    ),
                )
                st.rerun()
            except (MigrationStoreError, MigrationApiError, ConfigurationError, OSError, ValueError, TypeError, KeyError) as exc:
                st.error(f"Execução real não iniciada: {type(exc).__name__}: {exc}")

    st.subheader("5. Verificar resultado")
    _show_results(store, plan_id)
