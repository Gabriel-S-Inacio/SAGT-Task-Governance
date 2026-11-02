from __future__ import annotations

import json
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import BitrixConfig, redact_secret


READ_METHODS = frozenset({"tasks.task.get", "user.get"})
WRITE_METHOD = "tasks.task.update"


class MigrationApiError(RuntimeError):
    def __init__(self, message: str, *, code: str = "", status: int | None = None):
        super().__init__(message)
        self.code = code
        self.status = status


class AmbiguousWriteError(MigrationApiError):
    """Transport failed after a write may have reached Bitrix24; never retry blindly."""


@dataclass
class MigrationBitrixClient:
    webhook: str
    config: BitrixConfig
    opener: Callable[..., Any] = urlopen
    sleeper: Callable[[float], None] = time.sleep
    min_interval_seconds: float = 0.25
    _last_request_at: float = field(default=0.0, init=False, repr=False)

    def _rate_limit(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.min_interval_seconds:
            self.sleeper(self.min_interval_seconds - elapsed)
        self._last_request_at = time.monotonic()

    def _request(self, method: str, params: dict[str, Any], *, is_write: bool) -> dict[str, Any]:
        if is_write and method != WRITE_METHOD:
            raise MigrationApiError(f"Método de escrita não permitido: {method}")
        if not is_write and method not in READ_METHODS:
            raise MigrationApiError(f"Método de leitura não permitido: {method}")
        url = f"{self.webhook.rstrip('/')}/{method}.json"
        body = json.dumps(params, ensure_ascii=False).encode("utf-8")
        request = Request(url, data=body, headers={"Content-Type": "application/json", "Accept": "application/json"}, method="POST")
        attempts = 1 if is_write else self.config.max_retries + 1
        for attempt in range(attempts):
            self._rate_limit()
            try:
                with self.opener(request, timeout=self.config.timeout_seconds) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                if not isinstance(payload, dict):
                    raise MigrationApiError("Resposta Bitrix24 não é um objeto JSON.")
                if payload.get("error"):
                    error = payload.get("error")
                    if isinstance(error, dict):
                        code = str(error.get("code", ""))
                        description = str(error.get("message", ""))
                    else:
                        code = str(error)
                        description = str(payload.get("error_description", ""))
                    raise MigrationApiError(f"Bitrix24: {code} - {description}", code=code)
                return payload
            except HTTPError as exc:
                if is_write:
                    if 500 <= exc.code < 600:
                        raise AmbiguousWriteError(
                            f"Resultado da atualização é incerto após falha HTTP {exc.code}.",
                            status=exc.code,
                        ) from exc
                    raise MigrationApiError(f"Falha HTTP {exc.code} ao atualizar tarefa.", status=exc.code) from exc
                retryable = exc.code == 429 or 500 <= exc.code < 600
                if not retryable or attempt + 1 >= attempts:
                    raise MigrationApiError(f"Falha HTTP {exc.code} ao consultar Bitrix24.", status=exc.code) from exc
                self.sleeper(self._delay(attempt, exc.headers.get("Retry-After") if exc.headers else None))
            except (URLError, TimeoutError) as exc:
                safe = redact_secret(str(exc), self.webhook)
                if is_write:
                    raise AmbiguousWriteError(f"Resultado da atualização é incerto: {safe}") from exc
                if attempt + 1 >= attempts:
                    raise MigrationApiError(f"Falha de rede ao consultar Bitrix24: {safe}") from exc
                self.sleeper(self._delay(attempt, None))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                if is_write:
                    raise AmbiguousWriteError("Resposta inválida após tentativa de atualização.") from exc
                raise MigrationApiError("Bitrix24 retornou JSON inválido.") from exc
        raise AssertionError("unreachable")

    @staticmethod
    def _delay(attempt: int, retry_after: str | None) -> float:
        if retry_after:
            try:
                return min(max(float(retry_after), 0.0), 60.0)
            except ValueError:
                pass
        return min(0.5 * (2**attempt) + random.uniform(0, 0.25), 30.0)

    def get_task(self, task_id: str, extra_select: tuple[str, ...] | list[str] = ()) -> dict[str, Any]:
        extra: list[str] = []
        for field_id in extra_select:
            normalized = str(field_id).strip().upper()
            if normalized != "GROUP_ID" and not re.fullmatch(r"UF_[A-Z0-9_]+", normalized):
                raise MigrationApiError(f"Campo adicional de leitura não permitido: {field_id}")
            if normalized not in extra:
                extra.append(normalized)
        payload = self._request("tasks.task.get", {
            "taskId": int(task_id),
            "select": [
                "ID", "TITLE", "RESPONSIBLE_ID", "CREATED_BY", "ACCOMPLICES", "AUDITORS", "CHANGED_DATE", "ACTION",
                *extra,
            ],
        }, is_write=False)
        result = payload.get("result", {})
        task = result.get("task") if isinstance(result, dict) else None
        if not isinstance(task, dict):
            raise MigrationApiError("Tarefa não acessível ou resposta sem objeto task.", code="NOT_ACCESSIBLE")
        return task

    def get_user(self, user_id: str) -> dict[str, Any] | None:
        payload = self._request("user.get", {"FILTER": {"ID": int(user_id)}}, is_write=False)
        result = payload.get("result", [])
        if isinstance(result, dict):
            result = result.get("users", result.get("items", []))
        return result[0] if isinstance(result, list) and result and isinstance(result[0], dict) else None

    def update_task(self, task_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        allowed = {"RESPONSIBLE_ID", "ACCOMPLICES", "AUDITORS"}
        if not fields or set(fields) - allowed:
            raise MigrationApiError("Patch contém campo não permitido para migração.")
        return self._request("tasks.task.update", {"taskId": int(task_id), "fields": fields}, is_write=True)


def sanitized_response(payload: Any) -> Any:
    """Keep only operational response evidence; never persist credentials or arbitrary task data."""
    if not isinstance(payload, dict):
        return {"type": type(payload).__name__}
    output: dict[str, Any] = {}
    result = payload.get("result")
    if isinstance(result, bool):
        output["result"] = result
    elif isinstance(result, dict):
        safe_result: dict[str, Any] = {}
        if isinstance(result.get("result"), bool):
            safe_result["result"] = result["result"]
        task = result.get("task")
        if isinstance(task, dict):
            safe_result["task"] = {"id": task.get("id", task.get("ID"))}
        output["result"] = safe_result
    for key in ("error", "error_description"):
        if key in payload:
            output[key] = str(payload[key])[:1000]
    timing = payload.get("time")
    if isinstance(timing, dict):
        output["time"] = {key: timing[key] for key in ("duration", "processing") if key in timing}
    return output
