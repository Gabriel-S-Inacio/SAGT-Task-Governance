from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .audit import audit_tasks, build_summary, build_user_report, role_users
from .checkpoint import CheckpointStore, query_fingerprint
from .client import BitrixClient, paginate
from .config import AppConfig
from .exporters import export_json, export_xlsx, report_payload, report_sheets
from .models import Task, User
from .roles import merge_links


TASK_FIELDS = (
    "ID", "TITLE", "STATUS", "RESPONSIBLE_ID", "CREATED_BY", "ACCOMPLICES", "AUDITORS",
    "CREATED_DATE", "CHANGED_DATE", "DEADLINE", "CLOSED_DATE", "GROUP_ID", "PRIORITY",
)


def collect(client: BitrixClient, config: AppConfig, store: CheckpointStore, *, incremental: bool) -> tuple[list[Task], list[User]]:
    select = [*TASK_FIELDS, *[item.id for item in config.custom_fields]]
    base_filter: dict[str, Any] = {}
    if not config.audit.include_closed:
        base_filter["!STATUS"] = "5"
    base_params = {
        "order": {"ID": "ASC"},
        "filter": base_filter,
        "select": select,
    }
    fingerprint = query_fingerprint({"params": base_params, "incremental": incremental})
    active = store.begin(fingerprint, incremental=incremental, overlap_minutes=config.audit.incremental_overlap_minutes)
    if active.get("updated_since"):
        base_params["filter"][">=CHANGED_DATE"] = active["updated_since"]
    if not active.get("collection_complete"):
        paginate(
            client,
            "tasks.task.list",
            base_params,
            item_key="tasks",
            start=active.get("next", 0),
            on_page=store.append_page,
        )
    raw_tasks = store.read_spool()
    # Defensive de-duplication keeps the newest occurrence if an API page overlaps.
    by_id: dict[str, dict[str, Any]] = {}
    anonymous: list[dict[str, Any]] = []
    for raw in raw_tasks:
        identifier = str(raw.get("id", raw.get("ID", ""))).strip()
        if identifier:
            by_id[identifier] = raw
        else:
            anonymous.append(raw)
    tasks = [Task.from_api(raw, tuple(item.id for item in config.custom_fields)) for raw in [*by_id.values(), *anonymous]]
    raw_users = paginate(client, "user.get", {"FILTER": {"ACTIVE": "Y"}}, start=0)
    users = [User.from_api(raw) for raw in raw_users]
    return tasks, users


def write_reports(
    config: AppConfig,
    tasks: list[Task],
    users: list[User],
    *,
    prefix: str,
    source: str,
    links: list[dict[str, Any]] | None = None,
    extra_metadata: dict[str, object] | None = None,
    output_directory: Path | None = None,
) -> tuple[Path, Path, datetime | None]:
    now = datetime.now(timezone.utc)
    findings = audit_tasks(tasks, config, now=now)
    user_report = build_user_report(tasks, users, findings)
    summary = build_summary(tasks, findings)
    metadata: dict[str, object] = {
        "schema_version": 1,
        "generated_at": now.isoformat(),
        "source": source,
        "read_only": True,
        "timezone": config.audit.timezone,
        "role_scope": {key: list(value) for key, value in config.roles.items()},
        "custom_fields": [
            {"id": item.id, "label": item.label, "required_for": list(item.required_for), "export": item.export}
            for item in config.custom_fields
        ],
    }
    if extra_metadata:
        metadata.update(extra_metadata)
    report_links = merge_links(links if links is not None else build_task_links(tasks))
    actions = build_actions(findings, report_links)
    summary["links"] = len(report_links)
    summary["role_divergences"] = sum(link.get("verification") != "confirmed" for link in report_links)
    payload = report_payload(tasks, users, findings, user_report, summary, metadata, report_links, actions)
    target = output_directory or config.audit.output_directory
    json_path = target / f"{prefix}.json"
    xlsx_path = target / f"{prefix}.xlsx"
    export_json(json_path, payload)
    export_xlsx(
        xlsx_path,
        report_sheets(tasks, findings, user_report, summary, metadata, config.custom_fields, report_links, actions),
    )
    changed = [task.changed_at for task in tasks if task.changed_at]
    watermark = max(changed) if changed else now
    return json_path, xlsx_path, watermark


def build_task_links(tasks: list[Task]) -> list[dict[str, str]]:
    links: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for task in tasks:
        for role, user_ids in role_users(task).items():
            for user_id in user_ids:
                key = (task.id, user_id, role)
                if key not in seen:
                    seen.add(key)
                    links.append({
                        "task_id": task.id, "user_id": user_id, "role": role,
                        "verification": "confirmed", "reason": "Vínculo derivado dos campos da tarefa.",
                    })
    return links


def build_actions(findings, links: list[dict[str, Any]]) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for link in links:
        if link.get("verification") == "confirmed":
            continue
        action = {
            "task_id": link.get("task_id"), "user_id": link.get("user_id"), "role": link.get("role"),
            "verification": link.get("verification"), "severity": "high",
            "reason": link.get("reason") or "Vínculo requer validação manual.",
            "recommended_action": "Avaliar manualmente o vínculo; nenhuma reatribuição foi definida.",
        }
        key = tuple(action.values())
        if key not in seen:
            seen.add(key); actions.append(action)
    for finding in findings:
        action = {
            "task_id": finding.task_id, "user_id": finding.user_id, "role": finding.role,
            "verification": "finding", "severity": finding.severity, "reason": finding.message,
            "recommended_action": "Avaliar manualmente a pendência; nenhuma reatribuição foi definida.",
        }
        key = tuple(action.values())
        if key not in seen:
            seen.add(key); actions.append(action)
    return actions
