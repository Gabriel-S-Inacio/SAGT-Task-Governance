from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any


def parse_datetime(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    candidate = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(candidate)
    except ValueError:
        return None


def _ids(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        value = [value]
    output = []
    for item in value:
        if isinstance(item, dict):
            item = item.get("id", item.get("ID"))
        if item is not None and str(item).strip():
            output.append(str(item).strip())
    return tuple(dict.fromkeys(output))


@dataclass(frozen=True)
class Task:
    id: str
    title: str
    status: str
    responsible_id: str | None
    created_by: str | None
    priority: str = ""
    accomplices: tuple[str, ...] = ()
    auditors: tuple[str, ...] = ()
    created_at: datetime | None = None
    changed_at: datetime | None = None
    deadline: datetime | None = None
    closed_at: datetime | None = None
    group_id: str | None = None
    custom_fields: dict[str, Any] = field(default_factory=dict)
    extra_fields: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_api(
        cls, raw: dict[str, Any], custom_field_ids: tuple[str, ...], standard_field_ids: tuple[str, ...] = ()
    ) -> "Task":
        def pick(*names: str, default: Any = None) -> Any:
            for name in names:
                if name in raw:
                    return raw[name]
            return default

        normalized_keys = {"".join(character for character in str(key).lower() if character.isalnum()): value for key, value in raw.items()}

        def custom_value(field_id: str) -> Any:
            direct = pick(field_id, field_id.upper(), field_id.lower(), default=None)
            if direct is not None:
                return direct
            normalized = "".join(character for character in field_id.lower() if character.isalnum())
            return normalized_keys.get(normalized)

        rid = pick("responsibleId", "RESPONSIBLE_ID")
        creator = pick("createdBy", "CREATED_BY")
        return cls(
            id=str(pick("id", "ID", default="")).strip(),
            title=str(pick("title", "TITLE", default="") or "").strip(),
            status=str(pick("status", "STATUS", default="") or "").strip(),
            responsible_id=str(rid).strip() if rid not in (None, "") else None,
            created_by=str(creator).strip() if creator not in (None, "") else None,
            priority=str(pick("priority", "PRIORITY", default="") or "").strip(),
            accomplices=_ids(pick("accomplices", "ACCOMPLICES", default=[])),
            auditors=_ids(pick("auditors", "AUDITORS", default=[])),
            created_at=parse_datetime(pick("createdDate", "CREATED_DATE")),
            changed_at=parse_datetime(pick("changedDate", "CHANGED_DATE")),
            deadline=parse_datetime(pick("deadline", "DEADLINE")),
            closed_at=parse_datetime(pick("closedDate", "CLOSED_DATE")),
            group_id=str(pick("groupId", "GROUP_ID")).strip() if pick("groupId", "GROUP_ID") not in (None, "") else None,
            custom_fields={fid: custom_value(fid) for fid in custom_field_ids},
            extra_fields={
                field_id: pick(field_id, field_id.lower(), default=None)
                for field_id in standard_field_ids
                if field_id not in {
                    "ID", "TITLE", "STATUS", "PRIORITY", "RESPONSIBLE_ID", "CREATED_BY", "ACCOMPLICES",
                    "AUDITORS", "CREATED_DATE", "CHANGED_DATE", "DEADLINE", "CLOSED_DATE", "GROUP_ID",
                }
            },
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key in ("created_at", "changed_at", "deadline", "closed_at"):
            data[key] = data[key].isoformat() if data[key] else None
        return data


@dataclass(frozen=True)
class User:
    id: str
    name: str
    active: bool
    email: str = ""

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> "User":
        first = str(raw.get("NAME", raw.get("name", "")) or "").strip()
        last = str(raw.get("LAST_NAME", raw.get("lastName", "")) or "").strip()
        active_raw = raw.get("ACTIVE", raw.get("active", True))
        active = active_raw is True or str(active_raw).upper() in {"Y", "1", "TRUE"}
        return cls(
            id=str(raw.get("ID", raw.get("id", ""))).strip(),
            name=" ".join(x for x in (first, last) if x),
            active=active,
            email=str(raw.get("EMAIL", raw.get("email", "")) or "").strip(),
        )


@dataclass(frozen=True)
class Finding:
    task_id: str
    code: str
    severity: str
    message: str
    user_id: str | None = None
    role: str | None = None
    field_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
