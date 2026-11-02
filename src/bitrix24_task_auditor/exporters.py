from __future__ import annotations

import json
import os
import re
import tempfile
import zipfile
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable
from xml.etree.ElementTree import Element, SubElement, tostring

from .config import CustomFieldConfig
from .models import Finding, Task, User


ILLEGAL_XML = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F]")


def _json_value(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, tuple):
        return list(value)
    raise TypeError(f"tipo não serializável: {type(value).__name__}")


def export_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, default=_json_value)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def report_payload(
    tasks: list[Task],
    users: list[User],
    findings: list[Finding],
    user_report: list[dict[str, object]],
    summary: dict[str, object],
    metadata: dict[str, object],
    links: list[dict[str, Any]] | None = None,
    actions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "metadata": metadata,
        "summary": summary,
        "tasks": [task.to_dict() for task in tasks],
        "findings": [item.to_dict() for item in findings],
        "users": user_report,
        "links": links or [],
        "actions": actions or [],
    }


def _safe_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple)):
        text = json.dumps(value, ensure_ascii=False, default=_json_value, separators=(",", ":"))
    elif isinstance(value, (datetime, date)):
        text = value.isoformat()
    elif isinstance(value, bool):
        text = "Sim" if value else "Não"
    else:
        text = str(value)
    text = ILLEGAL_XML.sub("", text)
    if text.startswith(("=", "+", "-", "@")):
        text = "'" + text
    return text[:32767]


def _column_name(index: int) -> str:
    output = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        output = chr(65 + remainder) + output
    return output


def _worksheet_xml(headers: list[str], rows: Iterable[Iterable[Any]]) -> bytes:
    root = Element("worksheet", xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main")
    views = SubElement(root, "sheetViews")
    SubElement(views, "sheetView", workbookViewId="0")
    SubElement(root, "sheetFormatPr", defaultRowHeight="15")
    data = SubElement(root, "sheetData")
    all_rows = [headers, *[list(row) for row in rows]]
    for row_index, values in enumerate(all_rows, start=1):
        row_node = SubElement(data, "row", r=str(row_index))
        for column_index, value in enumerate(values, start=1):
            ref = f"{_column_name(column_index)}{row_index}"
            cell = SubElement(row_node, "c", r=ref, t="inlineStr", s="1" if row_index == 1 else "0")
            inline = SubElement(cell, "is")
            text = SubElement(inline, "t")
            safe = _safe_text(value)
            if safe[:1].isspace() or safe[-1:].isspace():
                text.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
            text.text = safe
    if headers:
        SubElement(root, "autoFilter", ref=f"A1:{_column_name(len(headers))}{len(all_rows)}")
        panes = views.find("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}sheetView")
        if panes is None:
            panes = views[0]
        SubElement(panes, "pane", ySplit="1", topLeftCell="A2", activePane="bottomLeft", state="frozen")
    return b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' + tostring(root, encoding="utf-8")


def export_xlsx(path: Path, sheets: list[tuple[str, list[str], list[list[Any]]]]) -> None:
    try:
        _export_xlsx_openpyxl(path, sheets)
        return
    except ImportError:
        # Preserva o CLI original antes da instalação das dependências web.
        pass
    _export_xlsx_legacy(path, sheets)


def _export_xlsx_openpyxl(path: Path, sheets: list[tuple[str, list[str], list[list[Any]]]]) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    os.close(descriptor)
    try:
        workbook = Workbook()
        workbook.remove(workbook.active)
        for name, headers, rows in sheets:
            sheet = workbook.create_sheet(title=name[:31])
            sheet.append([_safe_text(value) for value in headers])
            for row in rows:
                sheet.append([_safe_text(value) for value in row])
            sheet.freeze_panes = "A2"
            sheet.auto_filter.ref = sheet.dimensions
            for cell in sheet[1]:
                cell.font = Font(bold=True, color="FFFFFF")
                cell.fill = PatternFill("solid", fgColor="1F4E78")
            for column in sheet.columns:
                width = min(max((len(str(cell.value or "")) for cell in column), default=8) + 2, 60)
                sheet.column_dimensions[column[0].column_letter].width = width
        workbook.save(temp_name)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _export_xlsx_legacy(path: Path, sheets: list[tuple[str, list[str], list[list[Any]]]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    os.close(descriptor)
    try:
        with zipfile.ZipFile(temp_name, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml", _content_types(len(sheets)))
            archive.writestr("_rels/.rels", _root_rels())
            archive.writestr("xl/workbook.xml", _workbook(sheets))
            archive.writestr("xl/_rels/workbook.xml.rels", _workbook_rels(len(sheets)))
            archive.writestr("xl/styles.xml", _styles())
            for index, (_, headers, rows) in enumerate(sheets, start=1):
                archive.writestr(f"xl/worksheets/sheet{index}.xml", _worksheet_xml(headers, rows))
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def report_sheets(
    tasks: list[Task],
    findings: list[Finding],
    user_report: list[dict[str, object]],
    summary: dict[str, object],
    metadata: dict[str, object],
    custom_fields: tuple[CustomFieldConfig, ...],
    links: list[dict[str, Any]] | None = None,
    actions: list[dict[str, Any]] | None = None,
) -> list[tuple[str, list[str], list[list[Any]]]]:
    exported_fields = [item for item in custom_fields if item.export]
    extra_field_ids = sorted({key for task in tasks for key in task.extra_fields})
    links_by_task: dict[str, list[dict[str, Any]]] = {}
    for link in links or []:
        links_by_task.setdefault(str(link.get("task_id", "")), []).append(link)
    task_headers = [
        "ID", "Título", "Status", "Prioridade", "Responsável", "Criador", "Participantes", "Observadores",
        "Criada em", "Alterada em", "Prazo", "Concluída em", "Grupo", "Papéis por usuário",
        *extra_field_ids, *[item.label for item in exported_fields],
    ]
    task_rows = []
    for task in tasks:
        user_roles: dict[str, list[str]] = {}
        for link in links_by_task.get(task.id, []):
            label = str(link.get("role", ""))
            if link.get("verification") != "confirmed":
                label += f" [{link.get('verification')}]"
            user_roles.setdefault(str(link.get("user_id", "")), []).append(label)
        mapping = "; ".join(f"{user}: {', '.join(sorted(set(roles)))}" for user, roles in sorted(user_roles.items()))
        task_rows.append([
            task.id, task.title, task.status, task.priority, task.responsible_id, task.created_by,
            ", ".join(task.accomplices), ", ".join(task.auditors), task.created_at,
            task.changed_at, task.deadline, task.closed_at, task.group_id, mapping,
            *[task.extra_fields.get(item) for item in extra_field_ids],
            *[task.custom_fields.get(item.id) for item in exported_fields],
        ])
    user_headers = [
        "user_id", "name", "active", "email", "tasks_as_responsible", "tasks_as_creator",
        "tasks_as_accomplice", "tasks_as_auditor", "critical", "high", "medium", "low", "total_findings",
    ]
    user_rows = [[row.get(key) for key in user_headers] for row in user_report]
    summary_rows = [["Indicador", "Valor"]] + [[key, value] for key, value in summary.items()]
    metadata_rows = [["Propriedade", "Valor"]] + [[key, value] for key, value in metadata.items()]
    link_headers = [
        "task_id", "user_id", "role", "verification", "evidence_field", "observed_user_ids",
        "source_unit", "query_filter", "source_units", "query_filters", "reason",
    ]
    link_rows = [[item.get(key) for key in link_headers] for item in (links or [])]
    finding_headers = ["task_id", "code", "severity", "message", "user_id", "role", "field_id"]
    finding_rows = [[getattr(item, key) for key in finding_headers] for item in findings]
    return [
        ("Tarefas", task_headers, task_rows),
        ("Vínculos", link_headers, link_rows),
        ("Achados", finding_headers, finding_rows),
        ("Usuários", user_headers, user_rows),
        ("Resumo", summary_rows[0], summary_rows[1:]),
        ("Metadados", metadata_rows[0], metadata_rows[1:]),
    ]


def _content_types(count: int) -> bytes:
    root = Element("Types", xmlns="http://schemas.openxmlformats.org/package/2006/content-types")
    SubElement(root, "Default", Extension="rels", ContentType="application/vnd.openxmlformats-package.relationships+xml")
    SubElement(root, "Default", Extension="xml", ContentType="application/xml")
    SubElement(root, "Override", PartName="/xl/workbook.xml", ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml")
    SubElement(root, "Override", PartName="/xl/styles.xml", ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml")
    for index in range(1, count + 1):
        SubElement(root, "Override", PartName=f"/xl/worksheets/sheet{index}.xml", ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml")
    return tostring(root, encoding="utf-8", xml_declaration=True)


def _root_rels() -> bytes:
    root = Element("Relationships", xmlns="http://schemas.openxmlformats.org/package/2006/relationships")
    SubElement(root, "Relationship", Id="rId1", Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument", Target="xl/workbook.xml")
    return tostring(root, encoding="utf-8", xml_declaration=True)


def _workbook(sheets: list[tuple[str, list[str], list[list[Any]]]]) -> bytes:
    root = Element("workbook", xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main")
    root.set("xmlns:r", "http://schemas.openxmlformats.org/officeDocument/2006/relationships")
    node = SubElement(root, "sheets")
    for index, (name, _, _) in enumerate(sheets, start=1):
        sheet = SubElement(node, "sheet", name=name[:31], sheetId=str(index))
        sheet.set("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id", f"rId{index}")
    return tostring(root, encoding="utf-8", xml_declaration=True)


def _workbook_rels(count: int) -> bytes:
    root = Element("Relationships", xmlns="http://schemas.openxmlformats.org/package/2006/relationships")
    for index in range(1, count + 1):
        SubElement(root, "Relationship", Id=f"rId{index}", Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet", Target=f"worksheets/sheet{index}.xml")
    SubElement(root, "Relationship", Id=f"rId{count + 1}", Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles", Target="styles.xml")
    return tostring(root, encoding="utf-8", xml_declaration=True)


def _styles() -> bytes:
    return b'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><color rgb="FFFFFFFF"/><sz val="11"/><name val="Calibri"/></font></fonts>
<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF1F4E78"/><bgColor indexed="64"/></patternFill></fill></fills>
<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/></cellXfs>
<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>'''
