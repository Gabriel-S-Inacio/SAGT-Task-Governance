from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import BitrixConfig, redact_secret


READ_ONLY_METHODS = frozenset({
    "tasks.task.list", "tasks.task.get", "user.get", "user.current", "task.item.userfield.getlist",
})


class BitrixError(RuntimeError):
    pass


class PaginationError(BitrixError):
    pass


@dataclass(frozen=True)
class PageInfo:
    items: list[dict[str, Any]]
    next_cursor: int | None
    total: int | None
    next_present: bool


@dataclass
class BitrixClient:
    webhook: str
    config: BitrixConfig
    opener: Callable[..., Any] = urlopen
    sleeper: Callable[[float], None] = time.sleep

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if method not in READ_ONLY_METHODS:
            raise BitrixError(f"método não permitido no modo somente leitura: {method}")
        url = f"{self.webhook.rstrip('/')}/{method}.json"
        body = json.dumps(params or {}, ensure_ascii=False).encode("utf-8")
        request = Request(url, data=body, headers={"Content-Type": "application/json", "Accept": "application/json"}, method="POST")
        attempts = self.config.max_retries + 1
        for attempt in range(attempts):
            try:
                with self.opener(request, timeout=self.config.timeout_seconds) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                if not isinstance(payload, dict):
                    raise BitrixError("resposta Bitrix24 não é um objeto JSON")
                if payload.get("error"):
                    raise BitrixError(f"Bitrix24: {payload.get('error')} - {payload.get('error_description', '')}")
                return payload
            except HTTPError as exc:
                retryable = exc.code == 429 or 500 <= exc.code < 600
                if not retryable or attempt + 1 >= attempts:
                    raise BitrixError(f"falha HTTP {exc.code} ao consultar Bitrix24") from exc
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                self.sleeper(self._delay(attempt, retry_after))
            except (URLError, TimeoutError) as exc:
                if attempt + 1 >= attempts:
                    safe = redact_secret(str(exc), self.webhook)
                    raise BitrixError(f"falha de rede ao consultar Bitrix24: {safe}") from exc
                self.sleeper(self._delay(attempt, None))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise BitrixError("Bitrix24 retornou JSON inválido") from exc
        raise AssertionError("unreachable")

    @staticmethod
    def _delay(attempt: int, retry_after: str | None) -> float:
        if retry_after:
            try:
                return min(max(float(retry_after), 0.0), 60.0)
            except ValueError:
                pass
        return min(0.5 * (2**attempt) + random.uniform(0, 0.25), 30.0)


def _optional_non_negative_integer(value: Any, label: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise PaginationError(f"{label} de paginação inválido")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise PaginationError(f"{label} de paginação inválido") from exc
    if parsed < 0:
        raise PaginationError(f"{label} de paginação não pode ser negativo")
    return parsed


def extract_page_info(payload: dict[str, Any], item_key: str | None = None) -> PageInfo:
    result = payload.get("result", [])
    if item_key and isinstance(result, dict):
        items = result.get(item_key, [])
    else:
        items = result
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise PaginationError("formato de página inesperado na resposta Bitrix24")
    next_present = "next" in payload
    raw_next = payload.get("next")
    if not next_present and isinstance(result, dict) and "next" in result:
        next_present = True
        raw_next = result.get("next")
    raw_total = payload.get("total")
    if raw_total is None and isinstance(result, dict):
        raw_total = result.get("total")
    return PageInfo(
        items=items,
        next_cursor=_optional_non_negative_integer(raw_next, "cursor") if next_present else None,
        total=_optional_non_negative_integer(raw_total, "total"),
        next_present=next_present,
    )


def extract_page(payload: dict[str, Any], item_key: str | None = None) -> tuple[list[dict[str, Any]], int | None]:
    page = extract_page_info(payload, item_key)
    return page.items, page.next_cursor


def validate_page_transition(cursor: int, page: PageInfo) -> None:
    if page.next_cursor is not None and page.next_cursor <= cursor:
        raise PaginationError(f"cursor de paginação não avançou: atual={cursor}, próximo={page.next_cursor}")
    if page.total is not None and page.next_cursor is not None and page.next_cursor >= page.total:
        raise PaginationError(
            f"metadados de paginação inconsistentes: next={page.next_cursor}, total={page.total}"
        )
    if page.next_cursor is None and page.total is not None and cursor + len(page.items) < page.total:
        raise PaginationError(
            f"fim da paginação não comprovado: next ausente em {cursor}, mas total={page.total}"
        )


def paginate(
    client: BitrixClient,
    method: str,
    params: dict[str, Any],
    *,
    item_key: str | None = None,
    start: int | None = 0,
    on_page: Callable[[list[dict[str, Any]], int | None], None] | None = None,
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    cursor = start
    seen: set[int] = set()
    while cursor is not None:
        if cursor in seen:
            raise BitrixError("cursor de paginação repetido")
        seen.add(cursor)
        page_params = dict(params)
        page_params["start"] = cursor
        page = extract_page_info(client.call(method, page_params), item_key)
        validate_page_transition(cursor, page)
        items, next_cursor = page.items, page.next_cursor
        output.extend(items)
        if on_page:
            on_page(items, next_cursor)
        cursor = next_cursor
    return output
