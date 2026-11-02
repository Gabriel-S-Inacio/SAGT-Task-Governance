from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import streamlit as st

from bitrix24_task_auditor.client import BitrixError
from bitrix24_task_auditor.config import ConfigurationError, ROLE_NAMES, load_config
from bitrix24_task_auditor.migration_ui import render_migration
from bitrix24_task_auditor.presenter import (
    filter_result_rows, load_result_bundle, result_indicators, result_rows, select_result_run_id,
)
from bitrix24_task_auditor.run_store import RunStore, RunStoreError
from bitrix24_task_auditor.roles import ROLE_LABELS
from bitrix24_task_auditor.web_models import (
    CORE_FIELDS, PRIORITY_LABELS, STANDARD_FIELDS, STATUS_LABELS, QuerySpec, QueryValidationError, query_summary,
)
from bitrix24_task_auditor.web_service import (
    connection_status, create_and_start, load_field_catalog, reconcile_worker_state, refresh_field_catalog,
    start_worker, test_connection,
)
from bitrix24_task_auditor.worker import export_run_snapshot


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config" / "auditor.toml"
STORE = RunStore(ROOT)
STATUS_NAMES = {
    "queued": "Na fila", "starting": "Iniciando", "running": "Em execução", "cancelling": "Cancelando",
    "cancelled": "Cancelada", "completed": "Concluída", "partial": "Parcial — cobertura incompleta",
    "limited": "Parcial legado — limite atingido", "failed": "Falhou",
}


st.set_page_config(page_title="SAGT — Auditoria e Governança de Tarefas", page_icon="🔎", layout="wide")
st.markdown("""
<style>
.block-container {padding-top: 1.8rem; max-width: 1450px;}
[data-testid="stMetric"] {background:#fff; border:1px solid #dfe6ee; padding:12px; border-radius:10px;}
.muted {color:#5d6b78; font-size:.92rem}
</style>
""", unsafe_allow_html=True)


def config_and_catalog():
    config = load_config(CONFIG_PATH, project_root=ROOT)
    return config, load_field_catalog(ROOT, config)


def parse_ids(text: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(item for item in re.split(r"[\s,;]+", text.strip()) if item))


def latest_runs():
    runs = STORE.list_runs()
    for item in runs:
        if item.get("status") in {"queued", "starting", "running", "cancelling"}:
            reconcile_worker_state(STORE, item["id"])
    return STORE.list_runs()


def run_label(item):
    return f"{item.get('id')} · {STATUS_NAMES.get(item.get('status'), item.get('status'))} · {item.get('spec', {}).get('report_name', '')}"


def render_overview(config):
    st.title("SAGT — Sistema de Auditoria e Governança de Tarefas")
    st.caption("Auditoria de vínculos e migração supervisionada integrada ao Bitrix24.")
    status = connection_status(config)
    col1, col2, col3 = st.columns(3)
    col1.metric("Webhook", "Configurado" if status["configured"] else "Pendente")
    col2.metric("Host autorizado", status.get("host") or "Não disponível")
    col3.metric("Modo", "READ-ONLY")
    if status["configured"]:
        st.success(status["message"] + ". A credencial permanece oculta.")
    else:
        st.warning(status["message"])
    if st.button("Testar conectividade", type="primary", disabled=not status["configured"]):
        with st.spinner("Executando consulta mínima user.current…"):
            try:
                result = test_connection(config)
                label = f" Usuário: {result['user_id']} {result['name']}".strip()
                st.success(result["message"] + (f" — {label}" if label else ""))
            except (ConfigurationError, BitrixError) as exc:
                message = str(exc)
                if "401" in message or "AUTH" in message.upper():
                    st.error("Falha de autenticação. Revise o webhook sem compartilhá-lo na tela.")
                elif "403" in message or "PERMISSION" in message.upper():
                    st.error("Webhook autenticado, mas sem permissão para esta leitura.")
                else:
                    st.error(f"Conectividade não confirmada: {message}")
    runs = latest_runs()
    if runs:
        latest = runs[0]
        progress = latest.get("progress", {})
        st.subheader("Última auditoria")
        a, b, c, d = st.columns(4)
        a.metric("Estado", STATUS_NAMES.get(latest.get("status"), latest.get("status")))
        b.metric("Tarefas únicas", progress.get("unique_tasks", 0))
        c.metric("Vínculos", progress.get("links", 0))
        d.metric("Páginas", progress.get("pages", 0))
    else:
        st.info("Ainda não há auditorias no histórico local.")


def apply_template(spec: QuerySpec):
    st.session_state.selected_users = list(spec.user_ids)
    st.session_state.roles = list(spec.roles)
    st.session_state.group_text = ", ".join(spec.group_ids)
    st.session_state.statuses = list(spec.statuses)
    st.session_state.priorities = list(spec.priorities)
    st.session_state.standard_fields = list(spec.standard_fields)
    st.session_state.custom_fields = list(spec.custom_fields)
    st.session_state.report_name = spec.report_name
    st.session_state.output_directory = spec.output_directory
    st.session_state.pop("user_selection", None)


def render_new_audit(config, catalog):
    st.title("Nova Auditoria")
    st.caption("Monte um escopo validado. A execução só começa ao pressionar o botão de confirmação.")
    templates = STORE.list_templates()
    if templates:
        c1, c2 = st.columns([3, 1])
        selected_template = c1.selectbox("Carregar modelo local", ["—"] + templates)
        if c2.button("Carregar", disabled=selected_template == "—"):
            apply_template(STORE.load_template(selected_template)); st.rerun()
    st.subheader("1. Usuários e papéis")
    defaults = {
        "selected_users": [], "roles": ["responsible"], "group_text": "", "statuses": [], "priorities": [],
        "standard_fields": list(CORE_FIELDS), "custom_fields": [], "report_name": "auditoria", "output_directory": "output",
    }
    for key, value in defaults.items(): st.session_state.setdefault(key, value)
    c1, c2 = st.columns([4, 1])
    new_ids = c1.text_input("Adicionar IDs de colaboradores", placeholder="Ex.: 12, 48, 105")
    if c2.button("Adicionar IDs"):
        candidates = parse_ids(new_ids)
        if any(not item.isdigit() or int(item) <= 0 for item in candidates):
            st.error("Use somente IDs numéricos positivos.")
        else:
            st.session_state.selected_users = list(dict.fromkeys([*st.session_state.selected_users, *candidates]))
            st.session_state.pop("user_selection", None); st.rerun()
    st.session_state.setdefault("user_selection", st.session_state.selected_users)
    selected_users = st.multiselect(
        "Colaboradores selecionados — remova um item pelo ×", st.session_state.selected_users,
        key="user_selection",
    )
    st.session_state.selected_users = selected_users
    all_roles = st.checkbox("Selecionar todos os papéis", value=False)
    roles = list(ROLE_NAMES) if all_roles else st.multiselect(
        "Papéis", list(ROLE_NAMES), key="roles", format_func=lambda role: f"{ROLE_LABELS.get(role, role)} ({role})",
    )
    st.caption("Sem colaboradores, a consulta usa apenas os filtros abaixo e independe de papéis.")

    st.subheader("2. Filtros permitidos")
    c1, c2 = st.columns(2)
    groups = parse_ids(c1.text_input("IDs de grupos/projetos", key="group_text", placeholder="Opcional: 3, 18"))
    statuses = c1.multiselect("Status", list(STATUS_LABELS), format_func=STATUS_LABELS.get, key="statuses")
    priorities = c2.multiselect("Prioridade", list(PRIORITY_LABELS), format_func=PRIORITY_LABELS.get, key="priorities")
    use_deadline = c2.checkbox("Filtrar por intervalo de prazo")
    deadline_from = deadline_to = None
    if use_deadline:
        d1, d2 = st.columns(2)
        deadline_from = d1.date_input("Prazo inicial", value=date.today()).isoformat()
        deadline_to = d2.date_input("Prazo final", value=date.today()).isoformat()

    st.subheader("3. Campos e saída")
    standard_fields = st.multiselect(
        "Campos padrão", list(STANDARD_FIELDS), format_func=STANDARD_FIELDS.get, key="standard_fields",
    )
    custom_fields = st.multiselect(
        "Campos personalizados confirmados", list(catalog), format_func=lambda item: f"{catalog[item]} ({item})", key="custom_fields",
    )
    st.info("A paginação é automática: todas as páginas serão percorridas até a API deixar de retornar `next`.")
    c1, c2 = st.columns(2)
    report_name = c1.text_input("Nome do relatório", key="report_name")
    output_directory = c2.text_input("Pasta de saída relativa", key="output_directory")
    spec = QuerySpec(
        user_ids=tuple(selected_users), roles=tuple(roles), group_ids=groups, statuses=tuple(statuses),
        priorities=tuple(priorities), deadline_from=deadline_from, deadline_to=deadline_to,
        standard_fields=tuple(standard_fields), custom_fields=tuple(custom_fields), report_name=report_name,
        output_directory=output_directory,
    )
    try:
        spec.validate(confirmed_custom_fields=catalog, project_root=ROOT)
        valid, validation_message = True, "Configuração válida para execução."
        st.success(validation_message)
    except QueryValidationError as exc:
        valid, validation_message = False, str(exc)
        st.error(validation_message)
    with st.expander("Resumo da consulta", expanded=True):
        st.json(query_summary(spec))
    c1, c2 = st.columns(2)
    template_name = c1.text_input("Nome para salvar como modelo", placeholder="Ex.: auditoria_semanal")
    if c1.button("Salvar modelo", disabled=not valid or not template_name):
        try:
            STORE.save_template(template_name, spec); st.success("Modelo salvo sem credenciais.")
        except RunStoreError as exc:
            st.error(str(exc))
    if c2.button("Iniciar auditoria READ-ONLY", type="primary", disabled=not valid):
        if not connection_status(config)["configured"]:
            st.error("Configure o webhook no ambiente antes de iniciar.")
        else:
            try:
                state = create_and_start(ROOT, CONFIG_PATH, STORE, spec, confirmed_fields=list(catalog))
                st.session_state.active_run = state["id"]
                st.session_state.selected_result_run = state["id"]
                st.query_params["run_id"] = state["id"]
                status_label = STATUS_NAMES.get(state.get("status"), state.get("status"))
                st.success(f"Worker confirmado para a auditoria {state['id']} — estado: {status_label}; PID: {state.get('pid')}.")
                st.info("Abra Acompanhamento para observar heartbeat, páginas e registros persistidos.")
            except (RunStoreError, QueryValidationError, ConfigurationError) as exc:
                st.error(str(exc))


def render_monitoring():
    st.title("Acompanhamento")
    runs = latest_runs()
    if not runs:
        st.info("Nenhuma auditoria disponível."); return
    ids = [item["id"] for item in runs]
    default_id = st.session_state.get("active_run", ids[0])
    selected = st.selectbox("Auditoria", ids, index=ids.index(default_id) if default_id in ids else 0, format_func=lambda rid: run_label(next(item for item in runs if item["id"] == rid)))
    state = STORE.read(selected); progress = state.get("progress", {})
    total = max(int(progress.get("unit_total", 0)), 1); done = min(int(progress.get("unit_index", 0)), total)
    st.info(
        f"Progresso indeterminado · {progress.get('current_unit') or 'Aguardando'} · "
        f"{done}/{total} escopos totalmente percorridos"
    )
    cols = st.columns(6)
    for col, label, value in zip(cols, ["Estado", "Páginas", "Tarefas recuperadas", "Tarefas únicas", "Vínculos", "Tempo"], [
        STATUS_NAMES.get(state.get("status"), state.get("status")), progress.get("pages", 0), progress.get("records", 0),
        progress.get("unique_tasks", 0), progress.get("links", 0), f"{float(progress.get('elapsed_seconds', 0)):.1f}s",
    ]): col.metric(label, value)
    st.write(
        f"**Escopo atual:** usuário `{progress.get('current_user') or 'todos'}` · "
        f"papel `{progress.get('current_role') or 'consulta independente'}`"
    )
    st.write(f"**Tentativas de página:** {progress.get('attempts', 0)}")
    st.caption(
        f"PID: {state.get('pid', '—')} · Python: {state.get('worker_executable') or state.get('worker_python') or '—'} · "
        f"Heartbeat: {state.get('worker_heartbeat_at', '—')} · Saída: "
        f"{state.get('worker_exit_code', 'em execução' if state.get('status') in {'queued', 'starting', 'running', 'cancelling'} else 'não registrado')}"
    )
    current_total = progress.get("current_api_total")
    if current_total is None:
        st.caption("A API não informou o total do escopo atual; nenhum percentual é estimado.")
    else:
        st.caption(f"Total informado pela API para o escopo atual: {current_total}.")
    if state.get("last_error"):
        st.error(str(state["last_error"]))
    errors = progress.get("errors", [])
    if errors:
        st.error("\n\n".join(errors[-5:]))
    c1, c2, c3 = st.columns(3)
    if c1.button("Atualizar estado"):
        st.rerun()
    if c2.button("Cancelar com segurança", disabled=state.get("status") not in {"queued", "starting", "running"}):
        STORE.request_cancel(selected); st.warning("Cancelamento solicitado; será aplicado entre páginas."); st.rerun()
    if c3.button("Retomar", disabled=state.get("status") not in {"cancelled", "failed", "limited"}):
        try:
            start_worker(ROOT, CONFIG_PATH, STORE, selected); st.success("Retomada iniciada."); st.rerun()
        except RunStoreError as exc: st.error(str(exc))
    st.caption("O trabalhador continua mesmo quando esta página é recarregada ou fechada. O estado é persistido em disco.")


@st.fragment(run_every=3)
def render_results():
    st.title("Resultados")
    runs = latest_runs()
    if not runs:
        st.info("Nenhuma auditoria disponível."); return
    ids = [item["id"] for item in runs]
    requested = st.query_params.get("run_id")
    if isinstance(requested, list):
        requested = requested[0] if requested else None
    session_selected = st.session_state.get("selected_result_run")
    chosen = session_selected if session_selected in ids else select_result_run_id(
        runs, str(requested) if requested else None, st.session_state.get("active_run")
    )
    if session_selected != chosen:
        st.session_state.selected_result_run = chosen
    selected = st.selectbox(
        "Auditoria", ids, key="selected_result_run",
        format_func=lambda rid: run_label(next(item for item in runs if item["id"] == rid)),
    )
    st.query_params["run_id"] = selected
    state = STORE.read(selected)
    status = str(state.get("status", ""))
    if status in {"queued", "starting", "running", "cancelling"}:
        st.info(f"Auditoria {selected} em andamento. Esta área é atualizada automaticamente.")
    elif state.get("partial"):
        st.warning("Auditoria parcial. Os artefatos e indicadores abaixo contêm somente os dados confirmados até o checkpoint.")
    elif status == "completed":
        st.success("Auditoria concluída integralmente.")
    else:
        st.error(f"Auditoria encerrada com estado: {STATUS_NAMES.get(status, status)}.")
    if state.get("last_error"):
        st.error(str(state["last_error"]))
    bundle = load_result_bundle(ROOT, state)
    for error in bundle["errors"]:
        st.error(error)
    payload = bundle["payload"]
    if not payload:
        st.warning("Esta auditoria ainda não possui um conjunto de resultados válido vinculado ao seu run_id.")
        can_rebuild = status in {"completed", "partial", "cancelled", "failed", "limited"}
        if st.button("Gerar exportação local do checkpoint", disabled=not can_rebuild, key=f"snapshot_{selected}"):
            try:
                export_run_snapshot(ROOT, CONFIG_PATH, selected)
                st.success("Artefatos regenerados a partir do checkpoint local, sem consulta REST.")
                st.rerun()
            except (OSError, ValueError, RuntimeError, RunStoreError, ConfigurationError) as exc:
                st.error(f"Falha ao gerar a exportação local: {exc}")
        return
    indicators = result_indicators(payload); cols = st.columns(6)
    for col, label, value in zip(cols, [
        "Tarefas únicas", "Vínculos totais", "Como responsável", "Como criador", "Como participante", "Como observador",
    ], [
        indicators["unique_tasks"], indicators["links"], indicators["responsible"], indicators["creator"],
        indicators["accomplice"], indicators["auditor"],
    ]): col.metric(label, value)
    c1, c2, c3 = st.columns(3)
    c1.metric("Pendências", indicators["findings"])
    c2.metric("Exceções", indicators["exceptions"])
    c3.metric("Estado", STATUS_NAMES.get(status, status))
    d1, d2, d3 = st.columns(3)
    d1.write("**Distribuição por papel confirmado**"); d1.json(indicators["by_role"])
    d2.write("**Distribuição por status**"); d2.json(indicators["by_status"])
    d3.write("**Verificação dos vínculos**"); d3.json(indicators["by_verification"])
    rows = result_rows(payload)
    search = st.text_input("Pesquisar nos resultados locais")
    status_options = sorted({row["Status"] for row in rows}); role_options = sorted({r.strip() for row in rows for r in str(row["Papéis encontrados"]).split(",") if r.strip()})
    c1, c2 = st.columns(2)
    selected_statuses = c1.multiselect("Filtrar status", status_options)
    selected_roles = c2.multiselect("Filtrar papéis", role_options)
    filtered = filter_result_rows(rows, search=search, statuses=selected_statuses, roles=selected_roles)
    inventory_tab, links_tab, actions_tab = st.tabs(["Inventário Consolidado", "Matriz de Vínculos", "Matriz de Ações"])
    inventory_tab.dataframe(filtered, use_container_width=True, hide_index=True)
    links_tab.dataframe(payload.get("links", []), use_container_width=True, hide_index=True)
    actions_tab.dataframe(payload.get("actions", []), use_container_width=True, hide_index=True)
    query_stats = payload.get("metadata", {}).get("query_stats", [])
    with st.expander("Rastreabilidade das consultas por usuário e papel"):
        if query_stats:
            st.dataframe(query_stats, use_container_width=True, hide_index=True)
        else:
            st.caption("Auditoria legada sem estatísticas detalhadas por consulta.")
    d1, d2 = st.columns(2)
    for col, key, label, mime in (
        (d1, "xlsx_path", "Baixar Excel desta auditoria", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        (d2, "json_path", "Baixar JSON desta auditoria", "application/json"),
    ):
        path = bundle[key]
        if path:
            col.download_button(label, path.read_bytes(), file_name=path.name, mime=mime, key=f"download_{key}_{selected}")


def render_history(config, catalog):
    st.title("Histórico e Configurações")
    runs = latest_runs()
    st.subheader("Histórico local")
    if runs:
        st.dataframe([{"ID": item.get("id"), "Criada em": item.get("created_at"), "Estado": STATUS_NAMES.get(item.get("status"), item.get("status")), "Parcial": item.get("partial"), "Relatório": item.get("spec", {}).get("report_name")} for item in runs], use_container_width=True, hide_index=True)
    else: st.info("Histórico vazio.")
    st.subheader("Configuração segura")
    status = connection_status(config)
    st.write({"webhook_configurado": status["configured"], "host": status.get("host"), "variável": config.bitrix.webhook_env, "métodos": ["user.current", "user.get", "tasks.task.list", "task.item.userfield.getlist"]})
    st.write(f"**Campos personalizados confirmados:** {len(catalog)}")
    if st.button("Atualizar catálogo de campos personalizados", disabled=not status["configured"]):
        try:
            fields = refresh_field_catalog(ROOT, config); st.success(f"Catálogo atualizado: {len(fields)} campos confirmados."); st.rerun()
        except (ConfigurationError, BitrixError, ValueError) as exc: st.error(str(exc))
    st.caption("A atualização acima é uma consulta explícita e somente leitura. Nenhuma credencial é gravada no catálogo ou histórico.")


def main():
    try: config, catalog = config_and_catalog()
    except ConfigurationError as exc:
        st.error(f"Configuração inválida: {exc}"); st.stop()
    st.sidebar.markdown("### SAGT")
    page = st.sidebar.radio("Navegação", ["Visão Geral", "Nova Auditoria", "Acompanhamento", "Resultados", "Migração de Vínculos", "Histórico e Configurações"])
    st.sidebar.caption("Servidor local · 127.0.0.1 · auditoria READ-ONLY · migração supervisionada")
    if page == "Visão Geral": render_overview(config)
    elif page == "Nova Auditoria": render_new_audit(config, catalog)
    elif page == "Acompanhamento": render_monitoring()
    elif page == "Resultados": render_results()
    elif page == "Migração de Vínculos": render_migration(ROOT, config)
    else: render_history(config, catalog)


if __name__ == "__main__":
    main()
