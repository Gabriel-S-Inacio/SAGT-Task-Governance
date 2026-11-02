from __future__ import annotations

from collections import Counter, defaultdict
import json
from pathlib import Path
from typing import Any

from .roles import ROLE_LABELS
from .web_models import STATUS_LABELS


def select_result_run_id(
    runs: list[dict[str, Any]], requested_run_id: str | None = None, active_run_id: str | None = None
) -> str | None:
    """Select a persisted run deterministically, without depending on widget order."""
    available = {str(item.get("id")) for item in runs if item.get("id")}
    for candidate in (requested_run_id, active_run_id):
        if candidate and str(candidate) in available:
            return str(candidate)
    return str(runs[0]["id"]) if runs else None


def load_result_bundle(root: Path, state: dict[str, Any]) -> dict[str, Any]:
    """Load only artifacts that demonstrably belong to the selected run."""
    root = root.resolve()
    run_id = str(state.get("id", ""))
    bundle: dict[str, Any] = {"payload": None, "json_path": None, "xlsx_path": None, "errors": []}
    result = state.get("result", {}) if isinstance(state.get("result"), dict) else {}
    for kind in ("json", "xlsx"):
        relative = result.get(kind)
        if not relative:
            continue
        path = (root / str(relative)).resolve()
        try:
            path.relative_to(root)
        except ValueError:
            bundle["errors"].append(f"Caminho {kind.upper()} aponta para fora do projeto.")
            continue
        if not path.is_file():
            bundle["errors"].append(f"Arquivo {kind.upper()} da auditoria não foi encontrado.")
            continue
        if run_id not in path.stem:
            bundle["errors"].append(f"Arquivo {kind.upper()} não corresponde ao run_id selecionado.")
            continue
        bundle[f"{kind}_path"] = path
    json_path = bundle["json_path"]
    if json_path:
        try:
            payload = json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            bundle["errors"].append(f"JSON da auditoria não pôde ser lido: {exc}")
        else:
            metadata = payload.get("metadata", {}) if isinstance(payload, dict) else {}
            if str(metadata.get("run_id", "")) != run_id:
                bundle["errors"].append("O conteúdo JSON não corresponde ao run_id selecionado.")
            else:
                bundle["payload"] = payload
    return bundle


def result_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    tasks = payload.get("tasks", []) if isinstance(payload, dict) else []
    links = payload.get("links", []) if isinstance(payload, dict) else []
    users = payload.get("users", []) if isinstance(payload, dict) else []
    names = {str(item.get("user_id", "")): str(item.get("name", "")) for item in users if isinstance(item, dict)}
    by_task: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for link in links:
        if isinstance(link, dict):
            by_task[str(link.get("task_id", ""))].append(link)
    rows = []
    for task in tasks:
        if not isinstance(task, dict):
            continue
        task_id = str(task.get("id", ""))
        task_links = by_task[task_id]
        user_roles: dict[str, list[str]] = defaultdict(list)
        for item in task_links:
            user_id, role = str(item.get("user_id", "")), str(item.get("role", ""))
            if not user_id or not role:
                continue
            label = ROLE_LABELS.get(role, role)
            if item.get("verification") not in (None, "confirmed"):
                label += f" [{item.get('verification')}]"
            user_roles[user_id].append(label)
        user_ids = sorted(user_roles)
        roles = sorted({str(item.get("role", "")) for item in task_links if item.get("role")})
        role_mapping = "; ".join(
            f"{uid} - {names.get(uid, '')}: {', '.join(sorted(set(user_roles[uid])))}".replace(" - :", ":")
            for uid in user_ids
        )
        custom = task.get("custom_fields", {}) if isinstance(task.get("custom_fields"), dict) else {}
        row = {
            "ID": task_id,
            "Título": task.get("title", ""),
            "Status": STATUS_LABELS.get(str(task.get("status", "")), str(task.get("status", ""))),
            "Prioridade": task.get("priority", ""),
            "Prazo": task.get("deadline", ""),
            "Grupo": task.get("group_id", ""),
            "Usuário analisado": ", ".join(f"{uid} - {names.get(uid, '')}".rstrip(" -") for uid in user_ids),
            "Papéis encontrados": ", ".join(roles),
            "Papéis por usuário": role_mapping,
            "Vínculos": len(task_links),
        }
        row.update(custom)
        extra = task.get("extra_fields", {}) if isinstance(task.get("extra_fields"), dict) else {}
        row.update(extra)
        rows.append(row)
    return rows


def filter_result_rows(
    rows: list[dict[str, Any]], *, search: str = "", statuses: list[str] | None = None, roles: list[str] | None = None
) -> list[dict[str, Any]]:
    needle = search.casefold().strip()
    wanted_statuses = set(statuses or [])
    wanted_roles = set(roles or [])
    output = []
    for row in rows:
        if needle and needle not in " ".join(str(value) for value in row.values()).casefold():
            continue
        if wanted_statuses and row.get("Status") not in wanted_statuses:
            continue
        found_roles = {item.strip() for item in str(row.get("Papéis encontrados", "")).split(",") if item.strip()}
        if wanted_roles and not (wanted_roles & found_roles):
            continue
        output.append(row)
    return output


def result_indicators(payload: dict[str, Any]) -> dict[str, Any]:
    rows = result_rows(payload)
    links = payload.get("links", []) if isinstance(payload, dict) and isinstance(payload.get("links", []), list) else []
    findings = payload.get("findings", []) if isinstance(payload, dict) and isinstance(payload.get("findings", []), list) else []
    confirmed = [item for item in links if isinstance(item, dict) and item.get("verification", "confirmed") == "confirmed"]
    by_role = Counter(str(item.get("role", "")) for item in confirmed)
    by_verification = Counter(str(item.get("verification", "confirmed")) for item in links if isinstance(item, dict))
    return {
        "unique_tasks": len(rows),
        "links": len(links),
        "responsible": by_role["responsible"],
        "creator": by_role["creator"],
        "accomplice": by_role["accomplice"],
        "auditor": by_role["auditor"],
        "by_role": dict(sorted(by_role.items())),
        "by_verification": dict(sorted(by_verification.items())),
        "by_status": dict(sorted(Counter(str(row.get("Status", "")) for row in rows).items())),
        "findings": len(findings),
        "exceptions": sum(1 for item in findings if isinstance(item, dict) and item.get("severity") in {"critical", "high"}),
    }
