from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Iterable

from .config import AppConfig, ROLE_NAMES
from .models import Finding, Task, User


CLOSED_STATUSES = {"5"}


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def role_users(task: Task) -> dict[str, tuple[str, ...]]:
    return {
        "responsible": (task.responsible_id,) if task.responsible_id else (),
        "creator": (task.created_by,) if task.created_by else (),
        "accomplice": task.accomplices,
        "auditor": task.auditors,
    }


def role_is_in_scope(task: Task, role: str, configured: tuple[str, ...]) -> bool:
    assigned = role_users(task)[role]
    return bool(assigned) and (not configured or bool(set(assigned) & set(configured)))


def audit_tasks(tasks: Iterable[Task], config: AppConfig, *, now: datetime | None = None) -> list[Finding]:
    current = _aware(now or datetime.now(timezone.utc))
    findings: list[Finding] = []
    for task in tasks:
        users_by_role = role_users(task)
        closed = task.status in CLOSED_STATUSES
        if closed and not config.audit.include_closed:
            continue
        if not task.id:
            findings.append(Finding("", "MISSING_ID", "critical", "Tarefa sem identificador"))
        if not task.title:
            findings.append(Finding(task.id, "MISSING_TITLE", "high", "Título não preenchido"))
        if not task.responsible_id:
            findings.append(Finding(task.id, "MISSING_RESPONSIBLE", "critical", "Responsável não atribuído", role="responsible"))
        if not task.deadline and not closed:
            findings.append(Finding(task.id, "MISSING_DEADLINE", "high", "Prazo não preenchido", task.responsible_id, "responsible"))

        deadline = _aware(task.deadline)
        if deadline and not closed and deadline < current:
            findings.append(Finding(task.id, "OVERDUE", "critical", "Tarefa em atraso", task.responsible_id, "responsible"))
        elif deadline and not closed and deadline <= current + timedelta(hours=config.audit.due_soon_hours):
            findings.append(Finding(task.id, "DUE_SOON", "medium", "Prazo próximo", task.responsible_id, "responsible"))
        if deadline and closed and _aware(task.closed_at) and _aware(task.closed_at) > deadline:
            findings.append(Finding(task.id, "CLOSED_LATE", "medium", "Tarefa concluída após o prazo", task.responsible_id, "responsible"))

        last_change = _aware(task.changed_at or task.created_at)
        if not closed and last_change and last_change < current - timedelta(hours=config.audit.stale_after_hours):
            findings.append(Finding(task.id, "STALE", "medium", "Tarefa sem atualização recente", task.responsible_id, "responsible"))

        for custom in config.custom_fields:
            value = task.custom_fields.get(custom.id)
            missing = value is None or value == "" or value == [] or value == {}
            if not missing:
                continue
            for role in custom.required_for:
                if role_is_in_scope(task, role, config.roles.get(role, ())):
                    matched = users_by_role[role]
                    configured = config.roles.get(role, ())
                    if configured:
                        matched = tuple(uid for uid in matched if uid in configured)
                    for uid in matched:
                        findings.append(Finding(
                            task.id,
                            "MISSING_CUSTOM_FIELD",
                            "high",
                            f"Campo obrigatório não preenchido: {custom.label}",
                            uid,
                            role,
                            custom.id,
                        ))
    return findings


def build_user_report(tasks: Iterable[Task], users: Iterable[User], findings: Iterable[Finding]) -> list[dict[str, object]]:
    tasks = list(tasks)
    findings = list(findings)
    user_map = {user.id: user for user in users}
    assignment_counts: dict[tuple[str, str], int] = Counter()
    relevant_ids: set[str] = set(user_map)
    for task in tasks:
        for role, ids in role_users(task).items():
            for uid in ids:
                relevant_ids.add(uid)
                assignment_counts[(uid, role)] += 1
    finding_counts: dict[str, Counter[str]] = defaultdict(Counter)
    for finding in findings:
        if finding.user_id:
            relevant_ids.add(finding.user_id)
            finding_counts[finding.user_id][finding.severity] += 1
    rows = []
    for uid in sorted(relevant_ids, key=lambda item: (int(item) if item.isdigit() else 10**30, item)):
        user = user_map.get(uid)
        counts = finding_counts[uid]
        rows.append({
            "user_id": uid,
            "name": user.name if user else "",
            "active": user.active if user else None,
            "email": user.email if user else "",
            **{f"tasks_as_{role}": assignment_counts[(uid, role)] for role in ROLE_NAMES},
            "critical": counts["critical"],
            "high": counts["high"],
            "medium": counts["medium"],
            "low": counts["low"],
            "total_findings": sum(counts.values()),
        })
    return rows


def build_summary(tasks: Iterable[Task], findings: Iterable[Finding]) -> dict[str, object]:
    tasks = list(tasks)
    findings = list(findings)
    return {
        "tasks": len(tasks),
        "open_tasks": sum(task.status not in CLOSED_STATUSES for task in tasks),
        "closed_tasks": sum(task.status in CLOSED_STATUSES for task in tasks),
        "findings": len(findings),
        "by_severity": dict(sorted(Counter(item.severity for item in findings).items())),
        "by_code": dict(sorted(Counter(item.code for item in findings).items())),
    }
