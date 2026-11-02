from __future__ import annotations

import hashlib
import io
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO

from openpyxl import load_workbook
from openpyxl.utils.datetime import from_excel


SUPPORTED_SCHEMA_VERSIONS = {1}
INVENTORY_SHEETS = ("Inventário Consolidado", "Tarefas")
LINK_SHEETS = ("Matriz de Vínculos", "Vínculos")


class AuditImportError(ValueError):
    pass


@dataclass(frozen=True)
class ImportedTask:
    task_id: str
    title: str
    responsible_id: str | None
    created_by: str | None
    accomplices: tuple[str, ...]
    auditors: tuple[str, ...]
    changed_at: str | None
    properties: dict[str, tuple[str, ...]] = field(default_factory=dict)


@dataclass(frozen=True)
class ImportedLink:
    task_id: str
    user_id: str
    role: str
    verification: str
    evidence_field: str
    observed_user_ids: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class ImportedAudit:
    filename: str
    sha256: str
    schema_version: int
    metadata: dict[str, Any]
    tasks: dict[str, ImportedTask]
    links: tuple[ImportedLink, ...]
    blockers: tuple[str, ...]
    warnings: tuple[str, ...]
    property_fields: dict[str, str] = field(default_factory=dict)

    @property
    def executable(self) -> bool:
        return not self.blockers


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).strip().lstrip("'")


def _id(value: Any) -> str:
    text = _text(value)
    if re.fullmatch(r"\d+\.0", text):
        text = text[:-2]
    return text if text.isdigit() and int(text) > 0 else ""


def _ids(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (list, tuple, set)):
        parts = value
    else:
        text = _text(value)
        if not text:
            return ()
        try:
            decoded = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            decoded = re.split(r"[,;\s]+", text)
        parts = decoded if isinstance(decoded, (list, tuple, set)) else [decoded]
    output: list[str] = []
    for item in parts:
        candidate = _id(item.get("id", item.get("ID"))) if isinstance(item, dict) else _id(item)
        if candidate and candidate not in output:
            output.append(candidate)
    return tuple(output)


def _bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    normalized = _text(value).casefold()
    if normalized in {"true", "sim", "yes", "y", "1"}:
        return True
    if normalized in {"false", "não", "nao", "no", "n", "0"}:
        return False
    return None


def _datetime_text(value: Any) -> str | None:
    """Normalize auditor timestamps, including Excel serials, to ISO 8601 UTC.

    Excel converts ISO timestamps with an offset to an offset-less UTC serial in
    common copy/export flows. Treating the serial as plain text caused every
    current Bitrix timestamp to look different from the spreadsheet.
    """
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        candidate = value
    elif isinstance(value, date):
        candidate = datetime.combine(value, datetime.min.time())
    else:
        text = _text(value)
        try:
            serial = float(text)
        except (TypeError, ValueError):
            return text or None
        if not 1 <= serial <= 2958465:
            return text or None
        try:
            candidate = from_excel(serial)
        except (TypeError, ValueError, OverflowError):
            return text or None
    if candidate.tzinfo is None:
        candidate = candidate.replace(tzinfo=timezone.utc)
    return candidate.astimezone(timezone.utc).isoformat()


def _sheet(workbook, names: tuple[str, ...]):
    for name in names:
        if name in workbook.sheetnames:
            return workbook[name]
    return None


def _records(sheet) -> list[dict[str, Any]]:
    rows = list(sheet.iter_rows(values_only=True))
    if not rows:
        return []
    headers = [_text(item) for item in rows[0]]
    if not all(headers) or len(headers) != len(set(headers)):
        raise AuditImportError(f"Cabeçalho inválido ou duplicado na aba {sheet.title}.")
    return [dict(zip(headers, row)) for row in rows[1:] if any(item not in (None, "") for item in row)]


def _headers(sheet) -> list[str]:
    first = next(sheet.iter_rows(min_row=1, max_row=1, values_only=True), ())
    return [_text(item) for item in first]


def _property_fields(metadata: dict[str, Any], headers: list[str], warnings: list[str]) -> dict[str, str]:
    """Map an explicit spreadsheet column to its REST field identifier."""
    output: dict[str, str] = {}
    header_set = set(headers)
    if "Grupo" in header_set:
        output["GROUP_ID"] = "Grupo"
    for header in headers:
        field_id = header.strip().upper()
        if re.fullmatch(r"UF_[A-Z0-9_]+", field_id):
            output[field_id] = header
    raw_custom = metadata.get("custom_fields", [])
    if isinstance(raw_custom, list):
        labels: dict[str, list[str]] = {}
        for item in raw_custom:
            if not isinstance(item, dict):
                continue
            field_id = _text(item.get("id")).upper()
            label = _text(item.get("label"))
            if re.fullmatch(r"UF_[A-Z0-9_]+", field_id) and label in header_set:
                labels.setdefault(label, []).append(field_id)
        for label, field_ids in labels.items():
            if len(field_ids) > 1:
                warnings.append(
                    f"A coluna {label} corresponde a mais de um campo personalizado e não pode identificar proprietário."
                )
                continue
            output[field_ids[0]] = label
    return output


def _metadata(workbook) -> dict[str, Any]:
    if "Metadados" not in workbook.sheetnames:
        raise AuditImportError("Aba Metadados ausente.")
    records = _records(workbook["Metadados"])
    output: dict[str, Any] = {}
    for row in records:
        key = _text(row.get("Propriedade"))
        raw = row.get("Valor")
        if not key:
            continue
        text = _text(raw)
        try:
            output[key] = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            output[key] = raw if not isinstance(raw, str) else text
    return output


def _read_bytes(source: bytes | bytearray | Path | BinaryIO) -> tuple[bytes, str]:
    if isinstance(source, (bytes, bytearray)):
        return bytes(source), "auditoria.xlsx"
    if isinstance(source, Path):
        return source.read_bytes(), source.name
    name = Path(getattr(source, "name", "auditoria.xlsx")).name
    position = source.tell() if hasattr(source, "tell") else None
    data = source.read()
    if position is not None and hasattr(source, "seek"):
        source.seek(position)
    return data, name


def import_audit_xlsx(source: bytes | bytearray | Path | BinaryIO, *, filename: str | None = None) -> ImportedAudit:
    data, detected_name = _read_bytes(source)
    if not data:
        raise AuditImportError("Arquivo Excel vazio.")
    if not (filename or detected_name).lower().endswith(".xlsx"):
        raise AuditImportError("Somente arquivos .xlsx gerados pelo auditor são aceitos.")
    sha256 = hashlib.sha256(data).hexdigest()
    try:
        workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:
        raise AuditImportError(f"Arquivo XLSX inválido: {exc}") from exc
    try:
        inventory_sheet = _sheet(workbook, INVENTORY_SHEETS)
        links_sheet = _sheet(workbook, LINK_SHEETS)
        if inventory_sheet is None:
            raise AuditImportError("Inventário Consolidado/Tarefas ausente.")
        if links_sheet is None:
            raise AuditImportError("Matriz de Vínculos/Vínculos ausente.")
        metadata = _metadata(workbook)
        schema_raw = metadata.get("schema_version")
        try:
            schema = int(schema_raw)
        except (TypeError, ValueError) as exc:
            raise AuditImportError("Versão do relatório ausente ou inválida.") from exc
        blockers: list[str] = []
        warnings: list[str] = []
        if schema not in SUPPORTED_SCHEMA_VERSIONS:
            blockers.append(f"Versão de relatório não suportada: {schema}.")
        if _bool(metadata.get("read_only")) is not True:
            blockers.append("A proveniência READ-ONLY do relatório não foi comprovada.")
        if _bool(metadata.get("partial")) is not False:
            blockers.append("A auditoria é parcial ou não registra completude.")
        if _bool(metadata.get("pagination_complete")) is not True:
            blockers.append("A paginação completa da auditoria não foi comprovada.")
        if _text(metadata.get("run_status")).casefold() != "completed":
            blockers.append("A auditoria não está marcada como concluída.")
        provenance = _text(metadata.get("source")).casefold()
        if provenance and provenance not in {"bitrix24-web", "bitrix24-cli", "bitrix24-task-auditor"}:
            blockers.append(f"Proveniência do relatório não reconhecida: {provenance}.")
        elif not provenance:
            warnings.append("O relatório não registra a propriedade de proveniência source.")

        property_fields = _property_fields(metadata, _headers(inventory_sheet), warnings)
        tasks: dict[str, ImportedTask] = {}
        duplicate_tasks: set[str] = set()
        for row in _records(inventory_sheet):
            task_id = _id(row.get("ID", row.get("task_id")))
            if not task_id:
                blockers.append("Inventário contém tarefa sem ID numérico positivo.")
                continue
            task = ImportedTask(
                task_id=task_id,
                title=_text(row.get("Título", row.get("title"))),
                responsible_id=_id(row.get("Responsável", row.get("responsible_id"))) or None,
                created_by=_id(row.get("Criador", row.get("created_by"))) or None,
                accomplices=_ids(row.get("Participantes", row.get("accomplices"))),
                auditors=_ids(row.get("Observadores", row.get("auditors"))),
                changed_at=_datetime_text(row.get("Alterada em", row.get("changed_at"))),
                properties={field_id: _ids(row.get(column)) for field_id, column in property_fields.items()},
            )
            if task_id in tasks:
                duplicate_tasks.add(task_id)
                if tasks[task_id] != task:
                    blockers.append(f"Inventário contém versões conflitantes da tarefa {task_id}.")
            else:
                tasks[task_id] = task
        if duplicate_tasks:
            blockers.append(f"Tarefas duplicadas no inventário: {', '.join(sorted(duplicate_tasks))}.")

        links: list[ImportedLink] = []
        seen_links: set[tuple[str, str, str]] = set()
        for row in _records(links_sheet):
            task_id = _id(row.get("task_id", row.get("ID")))
            user_id = _id(row.get("user_id"))
            role = _text(row.get("role")).casefold()
            verification = _text(row.get("verification")).casefold()
            if not task_id or not user_id or role not in {"responsible", "creator", "accomplice", "auditor"}:
                blockers.append("Matriz de Vínculos contém tarefa, usuário ou papel inválido.")
                continue
            key = (task_id, user_id, role)
            if key in seen_links:
                blockers.append(f"Vínculo duplicado na matriz: tarefa {task_id}, usuário {user_id}, papel {role}.")
                continue
            seen_links.add(key)
            if task_id not in tasks:
                blockers.append(f"Vínculo da tarefa {task_id} não existe no inventário.")
            link = ImportedLink(
                task_id=task_id,
                user_id=user_id,
                role=role,
                verification=verification,
                evidence_field=_text(row.get("evidence_field")),
                observed_user_ids=_ids(row.get("observed_user_ids")),
                reason=_text(row.get("reason")),
            )
            links.append(link)
            task = tasks.get(task_id)
            if task and verification == "confirmed":
                if not link.evidence_field:
                    blockers.append(f"Vínculo confirmado sem campo de evidência na tarefa {task_id}, papel {role}.")
                if not link.observed_user_ids or user_id not in link.observed_user_ids:
                    blockers.append(f"Evidência observada não confirma o usuário {user_id} na tarefa {task_id}, papel {role}.")
                expected = {
                    "responsible": (task.responsible_id,) if task.responsible_id else (),
                    "creator": (task.created_by,) if task.created_by else (),
                    "accomplice": task.accomplices,
                    "auditor": task.auditors,
                }[role]
                if user_id not in expected:
                    blockers.append(
                        f"Inconsistência entre inventário e matriz na tarefa {task_id}, usuário {user_id}, papel {role}."
                    )
        if not tasks:
            blockers.append("O inventário não contém tarefas.")
        if not links:
            blockers.append("A matriz não contém vínculos.")
        return ImportedAudit(
            filename=Path(filename or detected_name).name,
            sha256=sha256,
            schema_version=schema,
            metadata=metadata,
            tasks=tasks,
            links=tuple(links),
            blockers=tuple(dict.fromkeys(blockers)),
            warnings=tuple(dict.fromkeys(warnings)),
            property_fields=property_fields,
        )
    finally:
        workbook.close()
