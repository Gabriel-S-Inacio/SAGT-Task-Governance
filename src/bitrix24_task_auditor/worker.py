from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from .client import BitrixClient, BitrixError, PaginationError, extract_page_info, validate_page_transition
from .config import CustomFieldConfig, load_config, redact_secret, webhook_from_environment
from .models import Task, User
from .run_store import RunStore, utc_now
from .roles import confirmed_links_from_fields, merge_links, verify_role
from .service import write_reports
from .web_models import CORE_FIELDS, ROLE_FILTERS, QuerySpec, QueryUnit, build_query_units, build_task_params
from .web_service import load_field_catalog


def _id(raw: dict[str, Any]) -> str:
    return str(raw.get("id", raw.get("ID", ""))).strip()


def _write_state(store: RunStore, run_id: str, state: dict[str, Any]) -> None:
    state["worker_heartbeat_at"] = utc_now()
    store.write(run_id, state)


def _environment_safe(text: str) -> str:
    safe = str(text)
    for key, value in os.environ.items():
        if value and len(value) >= 5 and any(marker in key.upper() for marker in ("WEBHOOK", "TOKEN", "SECRET")):
            safe = safe.replace(value, "***")
    return safe[:2000]


def _enrich_task(
    client: BitrixClient,
    raw: dict[str, Any],
    spec: QuerySpec,
    cache: dict[str, dict[str, Any] | None],
) -> tuple[dict[str, Any], str | None]:
    task_id = _id(raw)
    if not task_id:
        return raw, "Tarefa sem ID; enriquecimento não disponível."
    if task_id not in cache:
        try:
            payload = client.call("tasks.task.get", {
                "taskId": task_id,
                "select": list(dict.fromkeys([*CORE_FIELDS, *spec.standard_fields, *spec.custom_fields])),
            })
            result = payload.get("result", {})
            detail = result.get("task", {}) if isinstance(result, dict) else {}
            cache[task_id] = detail if isinstance(detail, dict) and detail else None
        except BitrixError as exc:
            cache[task_id] = None
            return raw, f"Enriquecimento somente leitura falhou para a tarefa {task_id}: {exc}"
    detail = cache[task_id]
    if detail is None:
        return raw, f"tasks.task.get não retornou dados verificáveis para a tarefa {task_id}."
    return {**raw, **detail}, None


def _links_for_page(
    client: BitrixClient,
    items: list[dict[str, Any]],
    spec: QuerySpec,
    unit: QueryUnit,
    unit_index: int,
    enrichment_cache: dict[str, dict[str, Any] | None],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    enriched_items: list[dict[str, Any]] = []
    links: list[dict[str, Any]] = []
    limitations: list[str] = []
    selected_pairs = [(user_id, role) for user_id in spec.user_ids for role in spec.roles]
    for raw in items:
        candidate = raw
        if unit.user_id and unit.role:
            evidence = verify_role(candidate, unit.user_id, unit.role)
            if evidence.verification == "unverifiable":
                candidate, limitation = _enrich_task(client, candidate, spec, enrichment_cache)
                if limitation:
                    limitations.append(limitation)
                evidence = verify_role(candidate, unit.user_id, unit.role)
            links.append(evidence.to_link(source_unit=unit_index, query_filter=ROLE_FILTERS[unit.role]))
            # Preserve additional selected roles that are directly visible in the same task.
            for selected_user, selected_role in selected_pairs:
                extra = verify_role(candidate, selected_user, selected_role)
                if extra.verification == "confirmed":
                    links.append(extra.to_link(source_unit=unit_index, query_filter=ROLE_FILTERS[unit.role]))
        else:
            links.extend(confirmed_links_from_fields(candidate, source_unit=unit_index))
        enriched_items.append(candidate)
    return enriched_items, merge_links(links), list(dict.fromkeys(limitations))


def _deduplicated(
    store: RunStore, run_id: str, filename: str, key_fields: tuple[str, ...]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    values: dict[tuple[str, ...], dict[str, Any]] = {}
    anonymous: list[dict[str, Any]] = []
    records, issues = store.read_records_report(run_id, filename)
    for item in records:
        key = tuple(str(item.get(field, "")) for field in key_fields)
        if all(key):
            values[key] = item
        else:
            anonymous.append(item)
    return [*values.values(), *anonymous], issues


def _collect_users(client: BitrixClient, store: RunStore, run_id: str) -> bool:
    cursor: int | None = 0
    seen: set[int] = set()
    while cursor is not None:
        if store.cancel_requested(run_id):
            return False
        if cursor in seen:
            raise PaginationError(f"cursor repetido durante a coleta de usuários: {cursor}")
        seen.add(cursor)
        page = extract_page_info(client.call("user.get", {"FILTER": {"ACTIVE": "Y"}, "start": cursor}))
        validate_page_transition(cursor, page)
        store.append_records(run_id, "users.ndjson", page.items)
        cursor = page.next_cursor
    return True


def _ensure_query_stats(progress: dict[str, Any], spec: QuerySpec, units: list[QueryUnit]) -> list[dict[str, Any]]:
    existing = progress.get("query_stats")
    stats = existing if isinstance(existing, list) else []
    while len(stats) < len(units):
        index = len(stats)
        unit = units[index]
        params = build_task_params(spec, unit)
        stats.append({
            "unit_index": index, "user_id": unit.user_id, "role": unit.role,
            "filter": params["filter"], "fields": params["select"], "pages": 0, "records": 0,
            "tasks_unique": 0, "confirmed_links": 0, "unverifiable_links": 0,
            "contradictory_links": 0, "status": "pending", "coverage": "pending",
        })
    progress["query_stats"] = stats
    return stats


def _snapshot(root: Path, config_path: Path, store: RunStore, run_id: str, *, status: str, partial: bool) -> None:
    state = store.read(run_id)
    spec = QuerySpec.from_dict(state["spec"])
    config = load_config(config_path, project_root=root)
    catalog = load_field_catalog(root, config)
    configured = {item.id.upper(): item for item in config.custom_fields}
    custom = list(config.custom_fields)
    for field_id in spec.custom_fields:
        if field_id.upper() not in configured:
            custom.append(CustomFieldConfig(field_id, catalog.get(field_id, field_id), (), True))
    config = replace(config, custom_fields=tuple(custom))
    raw_tasks, task_issues = _deduplicated(store, run_id, "tasks.ndjson", ("id",))
    # IDs podem vir em caixa alta; a segunda passagem cobre ambas as formas.
    by_id: dict[str, dict[str, Any]] = {}
    for raw in raw_tasks:
        by_id[_id(raw) or f"anonymous-{len(by_id)}"] = raw
    tasks = [Task.from_api(raw, tuple(item.id for item in custom), spec.standard_fields) for raw in by_id.values()]
    # Keep every observation until evidence reconciliation; pre-deduplicating here
    # could let a later contradictory response overwrite an earlier confirmation.
    raw_links, link_issues = store.read_records_report(run_id, "links.ndjson")
    reconciled_links: list[dict[str, Any]] = []
    for link in raw_links:
        if link.get("verification"):
            reconciled_links.append(link)
            continue
        task_raw = by_id.get(str(link.get("task_id", "")))
        if task_raw and link.get("user_id") and link.get("role"):
            evidence = verify_role(task_raw, str(link["user_id"]), str(link["role"]))
            reconciled_links.append(evidence.to_link(
                source_unit=link.get("source_unit"), query_filter=link.get("query_filter")
            ))
        else:
            reconciled_links.append({
                **link, "verification": "unverifiable",
                "reason": "Vínculo legado sem tarefa ou campos suficientes para reconciliação.",
            })
    links = merge_links(reconciled_links)
    raw_users, user_issues = _deduplicated(store, run_id, "users.ndjson", ("ID",))
    users = [User.from_api(raw) for raw in raw_users]
    spool_issues = [*task_issues, *link_issues, *user_issues]
    role_divergences = [link for link in links if link.get("verification") != "confirmed"]
    effective_partial = partial or bool(spool_issues) or bool(role_divergences)
    effective_status = "partial" if status == "completed" and effective_partial else status
    target = (root / spec.output_directory).resolve()
    prefix = f"{spec.report_name}_{run_id}"
    progress = state.get("progress", {})
    unit_total = int(progress.get("unit_total", 0))
    completed_units = len(progress.get("completed_units", []))
    json_path, xlsx_path, _ = write_reports(
        config, tasks, users, prefix=prefix, source="bitrix24-web", links=links,
        extra_metadata={
            "run_id": run_id, "partial": effective_partial, "run_status": effective_status, "query": spec.to_dict(),
            "pagination_complete": status == "completed" and completed_units == unit_total,
            "completed_scopes": completed_units,
            "total_scopes": unit_total,
            "pending_scopes": max(unit_total - completed_units, 0),
            "pending_cursor": progress.get("next_cursor"),
            "query_stats": progress.get("query_stats", []),
            "role_coverage_complete": not role_divergences,
            "role_divergences": len(role_divergences),
            "spool_issues": spool_issues,
        },
        output_directory=target,
    )
    state = store.read(run_id)
    state["status"] = effective_status
    state["partial"] = effective_partial
    state["result"] = {"json": str(json_path.relative_to(root)), "xlsx": str(xlsx_path.relative_to(root))}
    state["worker_finished_at"] = utc_now()
    state["worker_exit_code"] = 2 if status == "failed" else 0
    _write_state(store, run_id, state)
    print(f"[worker] finalizado run={run_id} status={effective_status} partial={effective_partial}", flush=True)


def export_run_snapshot(root: Path, config_path: Path, run_id: str) -> dict[str, Any]:
    """Regenerate local artifacts for a terminal run without making REST calls."""
    store = RunStore(root)
    state = store.read(run_id)
    status = str(state.get("status", "failed"))
    if status in {"queued", "starting", "running", "cancelling"}:
        raise RuntimeError("a auditoria ainda está em andamento; aguarde o checkpoint terminal")
    _snapshot(root, config_path, store, run_id, status=status, partial=bool(state.get("partial", status != "completed")))
    return store.read(run_id)


def _execute_run(root: Path, config_path: Path, run_id: str, *, client: BitrixClient | None = None) -> int:
    store = RunStore(root)
    state = store.read(run_id)
    state["worker_started_at"] = utc_now()
    state["worker_heartbeat_at"] = state["worker_started_at"]
    state["worker_executable"] = sys.executable
    state["status"] = "running"
    state["partial"] = True
    state.pop("last_error", None)
    state.pop("worker_finished_at", None)
    state.pop("worker_exit_code", None)
    store.write(run_id, state)
    print(f"[worker] iniciado run={run_id} python={sys.executable}", flush=True)
    spec = QuerySpec.from_dict(state["spec"])
    config = load_config(config_path, project_root=root)
    catalog = load_field_catalog(root, config)
    spec.validate(confirmed_custom_fields=catalog, project_root=root)
    webhook = webhook_from_environment(config.bitrix) if client is None else None
    active_client = client or BitrixClient(webhook or "", config.bitrix)
    units = build_query_units(spec)
    progress = state["progress"]
    progress["unit_total"] = len(units)
    progress.setdefault("completed_units", [])
    progress.setdefault("pagination_proofs", [])
    progress.setdefault("current_unit_pages", 0)
    progress.setdefault("current_unit_records", 0)
    progress.setdefault("current_api_total", None)
    progress.setdefault("limitations", [])
    query_stats = _ensure_query_stats(progress, spec, units)
    _write_state(store, run_id, state)
    started = time.monotonic()
    previous_elapsed = float(progress.get("elapsed_seconds", 0.0))
    existing_tasks, task_read_issues = store.read_records_report(run_id, "tasks.ndjson")
    existing_links, link_read_issues = store.read_records_report(run_id, "links.ndjson")
    if task_read_issues or link_read_issues:
        progress["limitations"].append("Spool contém registros inválidos; a cobertura será marcada como parcial.")
    task_ids = {_id(item) for item in existing_tasks if _id(item)}
    merged_existing_links = merge_links(existing_links)
    link_keys = {(str(item.get("task_id")), str(item.get("user_id")), str(item.get("role"))) for item in merged_existing_links}
    query_task_ids: dict[int, set[str]] = {index: set() for index in range(len(units))}
    query_links: dict[int, dict[tuple[str, str, str], dict[str, Any]]] = {index: {} for index in range(len(units))}
    for item in merged_existing_links:
        source_unit = item.get("source_unit")
        if isinstance(source_unit, int) and source_unit in query_task_ids:
            key = (str(item.get("task_id", "")), str(item.get("user_id", "")), str(item.get("role", "")))
            query_task_ids[source_unit].add(key[0])
            query_links[source_unit][key] = item
    start_unit = int(progress.get("unit_index", 0))
    if not progress["completed_units"] and start_unit > 0:
        # Migração de checkpoints criados antes das provas explícitas de paginação.
        progress["completed_units"] = list(range(start_unit))
        for legacy_index in range(start_unit):
            progress["pagination_proofs"].append({
                "unit_index": legacy_index, "evidence": "legacy_checkpoint",
                "user_id": units[legacy_index].user_id, "role": units[legacy_index].role,
            })
            query_stats[legacy_index]["status"] = "completed_legacy"
            query_stats[legacy_index]["coverage"] = "partial"
    enrichment_cache: dict[str, dict[str, Any] | None] = {}
    try:
        for index in range(start_unit, len(units)):
            unit = units[index]
            query_stat = query_stats[index]
            query_stat["status"] = "running"
            cursor = progress.get("next_cursor", 0) if index == start_unit else 0
            if progress.get("current_scope_index") != index:
                progress["current_unit_pages"] = 0
                progress["current_unit_records"] = 0
                progress["current_api_total"] = None
            progress["current_scope_index"] = index
            seen_cursors: set[int] = set()
            while cursor is not None:
                if store.cancel_requested(run_id):
                    progress["elapsed_seconds"] = previous_elapsed + time.monotonic() - started
                    state = store.read(run_id); state["progress"] = progress; _write_state(store, run_id, state)
                    _snapshot(root, config_path, store, run_id, status="cancelled", partial=True)
                    return 0
                if cursor in seen_cursors:
                    raise RuntimeError("cursor de paginação repetido")
                seen_cursors.add(cursor)
                progress["current_unit"] = unit.label
                progress["current_user"] = unit.user_id
                progress["current_role"] = unit.role
                progress["unit_index"] = index
                progress["next_cursor"] = cursor
                progress["attempts"] = int(progress.get("attempts", 0)) + 1
                progress["elapsed_seconds"] = previous_elapsed + time.monotonic() - started
                state = store.read(run_id); state["progress"] = progress; _write_state(store, run_id, state)
                params = build_task_params(spec, unit)
                params["start"] = cursor
                page = extract_page_info(active_client.call("tasks.task.list", params), "tasks")
                validate_page_transition(cursor, page)
                items, next_cursor = page.items, page.next_cursor
                enriched_items, links, limitations = _links_for_page(
                    active_client, items, spec, unit, index, enrichment_cache
                )
                progress["limitations"] = list(dict.fromkeys([*progress["limitations"], *limitations]))
                store.append_records(run_id, "tasks.ndjson", enriched_items)
                store.append_records(run_id, "links.ndjson", links)
                task_ids.update(_id(item) for item in enriched_items if _id(item))
                link_keys.update((item["task_id"], item["user_id"], item["role"]) for item in links)
                query_task_ids[index].update(_id(item) for item in enriched_items if _id(item))
                for link in links:
                    key = (str(link["task_id"]), str(link["user_id"]), str(link["role"]))
                    query_links[index][key] = link
                progress["pages"] += 1
                progress["records"] += len(items)
                progress["current_unit_pages"] += 1
                progress["current_unit_records"] += len(items)
                if page.total is not None:
                    progress["current_api_total"] = page.total
                progress["unique_tasks"] = len(task_ids)
                progress["links"] = len(link_keys)
                progress["confirmed_links"] = len({
                    key for unit_links in query_links.values() for key, value in unit_links.items()
                    if value.get("verification") == "confirmed"
                })
                progress["role_divergences"] = len({
                    key for unit_links in query_links.values() for key, value in unit_links.items()
                    if value.get("verification") != "confirmed"
                })
                query_stat["pages"] += 1
                query_stat["records"] += len(items)
                query_stat["tasks_unique"] = len(query_task_ids[index])
                verification_counts = {
                    name: sum(1 for link in query_links[index].values() if link.get("verification") == name)
                    for name in ("confirmed", "unverifiable", "contradictory")
                }
                query_stat["confirmed_links"] = verification_counts["confirmed"]
                query_stat["unverifiable_links"] = verification_counts["unverifiable"]
                query_stat["contradictory_links"] = verification_counts["contradictory"]
                progress["next_cursor"] = next_cursor
                progress["elapsed_seconds"] = previous_elapsed + time.monotonic() - started
                if next_cursor is None:
                    proof = {
                        "unit_index": index, "user_id": unit.user_id, "role": unit.role,
                        "terminal_cursor": cursor, "terminal_items": len(items), "api_total": page.total,
                        "evidence": "next_null" if page.next_present else "next_absent",
                    }
                    proofs = [item for item in progress["pagination_proofs"] if item.get("unit_index") != index]
                    proofs.append(proof)
                    progress["pagination_proofs"] = proofs
                    progress["completed_units"] = sorted(set([*progress["completed_units"], index]))
                    query_stat["status"] = "completed"
                    query_stat["result"] = "zero_results" if query_stat["records"] == 0 else "results_found"
                    query_stat["coverage"] = (
                        "partial" if query_stat["unverifiable_links"] or query_stat["contradictory_links"] else "complete"
                    )
                    progress["unit_index"] = index + 1
                    progress["next_cursor"] = 0
                state = store.read(run_id); state["progress"] = progress; _write_state(store, run_id, state)
                print(
                    f"[worker] pagina={progress['pages']} registros={progress['records']} unicas={progress['unique_tasks']}",
                    flush=True,
                )
                cursor = next_cursor
        if len(set(progress["completed_units"])) != len(units):
            raise PaginationError("nem todos os escopos possuem prova de término da paginação")
        if store.cancel_requested(run_id):
            _snapshot(root, config_path, store, run_id, status="cancelled", partial=True)
            return 0
        if not _collect_users(active_client, store, run_id):
            _snapshot(root, config_path, store, run_id, status="cancelled", partial=True)
            return 0
        progress["current_unit"] = "Concluída"
        progress["elapsed_seconds"] = previous_elapsed + time.monotonic() - started
        state = store.read(run_id); state["progress"] = progress; _write_state(store, run_id, state)
        _snapshot(root, config_path, store, run_id, status="completed", partial=False)
        return 0
    except Exception as exc:
        safe = redact_secret(str(exc), webhook)
        progress.setdefault("errors", []).append(safe[:1000])
        current_index = int(progress.get("unit_index", 0))
        if 0 <= current_index < len(query_stats):
            query_stats[current_index]["status"] = "failed"
            query_stats[current_index]["coverage"] = "partial"
        progress["elapsed_seconds"] = previous_elapsed + time.monotonic() - started
        print(f"[worker] erro run={run_id}: {safe[:1000]}", flush=True)
        state = store.read(run_id); state["progress"] = progress; state["last_error"] = safe[:1000]; _write_state(store, run_id, state)
        try:
            _snapshot(root, config_path, store, run_id, status="failed", partial=True)
        except Exception:
            state = store.read(run_id); state["status"] = "failed"; state["partial"] = True
            state["worker_finished_at"] = utc_now(); state["worker_exit_code"] = 2
            _write_state(store, run_id, state)
        return 2


def execute_run(root: Path, config_path: Path, run_id: str, *, client: BitrixClient | None = None) -> int:
    try:
        return _execute_run(root, config_path, run_id, client=client)
    except Exception as exc:
        safe = _environment_safe(exc)
        print(f"[worker] falha de inicialização run={run_id}: {safe}", flush=True)
        try:
            RunStore(root).record_worker_failure(run_id, f"Falha de inicialização do worker: {safe}", exit_code=2)
        except Exception as state_exc:
            print(f"[worker] não foi possível registrar a falha: {_environment_safe(state_exc)}", flush=True)
        return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args(argv)
    root = Path.cwd().resolve()
    return execute_run(root, (root / args.config).resolve(), args.run_id)


if __name__ == "__main__":
    raise SystemExit(main())
