from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .client import BitrixClient
from .config import AppConfig, ConfigurationError, webhook_from_environment
from .run_store import RunStore, RunStoreError
from .web_models import QuerySpec


ACTIVE_STATUSES = {"queued", "starting", "running", "cancelling"}
TERMINAL_STATUSES = {"completed", "partial", "limited", "cancelled", "failed"}


def worker_interpreter(root: Path) -> Path:
    relative = Path(".venv/Scripts/python.exe") if os.name == "nt" else Path(".venv/bin/python")
    interpreter = (root.resolve() / relative).resolve()
    if not interpreter.is_file():
        raise RunStoreError(f"interpretador do worker não encontrado no ambiente virtual local: {relative}")
    return interpreter


def _sanitized(text: str, environment: dict[str, str] | None = None) -> str:
    safe = str(text)
    for key, value in (environment or os.environ).items():
        upper = key.upper()
        if value and len(value) >= 5 and any(marker in upper for marker in ("WEBHOOK", "TOKEN", "SECRET")):
            safe = safe.replace(value, "***")
    return safe[:4000]


def _process_health(pid: int) -> tuple[bool | None, int | None]:
    if pid <= 0:
        return False, None
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return (None, None) if ctypes.get_last_error() == 5 else (False, None)
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return None, None
            return (True, None) if code.value == still_active else (False, int(code.value))
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True, None
    except ProcessLookupError:
        return False, None
    except PermissionError:
        return None, None


def reconcile_worker_state(store: RunStore, run_id: str, *, process_probe=None) -> dict[str, Any]:
    state = store.read(run_id)
    if state.get("status") not in ACTIVE_STATUSES:
        return state
    pid = state.get("pid")
    if not isinstance(pid, int):
        try:
            updated = datetime.fromisoformat(str(state.get("updated_at")))
            age = (datetime.now(timezone.utc) - updated).total_seconds()
        except (TypeError, ValueError):
            age = 999
        if age < 5:
            return state
        return store.record_worker_failure(run_id, "O worker não informou um PID válido durante a inicialização.")
    alive, exit_code = (process_probe or _process_health)(pid)
    if alive is not False:
        return state
    # Releitura evita sobrescrever uma conclusão gravada entre a sondagem e este ponto.
    state = store.read(run_id)
    if state.get("status") not in ACTIVE_STATUSES:
        return state
    tail = _sanitized(store.log_tail(run_id))
    detail = f"O worker terminou inesperadamente (código {exit_code if exit_code is not None else 'desconhecido'})."
    if tail:
        detail += f" Último registro: {tail[-1500:]}"
    return store.record_worker_failure(run_id, detail, exit_code=exit_code)


def connection_status(config: AppConfig) -> dict[str, Any]:
    try:
        webhook = webhook_from_environment(config.bitrix)
    except ConfigurationError as exc:
        return {"configured": False, "host": None, "message": str(exc)}
    return {"configured": True, "host": urlsplit(webhook).hostname, "message": "Webhook configurado e validado localmente"}


def test_connection(config: AppConfig, *, client: BitrixClient | None = None) -> dict[str, Any]:
    webhook = webhook_from_environment(config.bitrix)
    active_client = client or BitrixClient(webhook, config.bitrix)
    payload = active_client.call("user.current", {})
    result = payload.get("result", {})
    if not isinstance(result, dict):
        result = {}
    return {
        "ok": True,
        "user_id": str(result.get("ID", result.get("id", ""))),
        "name": " ".join(str(result.get(key, "") or "").strip() for key in ("NAME", "LAST_NAME")).strip(),
        "message": "Conectividade, autenticação e permissão de leitura confirmadas",
    }


def catalog_path(root: Path) -> Path:
    return root.resolve() / ".state" / "custom_fields.json"


def load_field_catalog(root: Path, config: AppConfig) -> dict[str, str]:
    catalog = {item.id: item.label for item in config.custom_fields}
    path = catalog_path(root)
    if path.exists():
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            value = {}
        if isinstance(value, dict):
            fields = value.get("fields", {})
            if isinstance(fields, dict):
                catalog.update({str(key): str(label) for key, label in fields.items()})
    return dict(sorted(catalog.items()))


def refresh_field_catalog(root: Path, config: AppConfig, *, client: BitrixClient | None = None) -> dict[str, str]:
    webhook = webhook_from_environment(config.bitrix)
    active_client = client or BitrixClient(webhook, config.bitrix)
    payload = active_client.call("task.item.userfield.getlist", {"PARAMS": {"order": {"ID": "ASC"}}})
    result = payload.get("result", [])
    if isinstance(result, dict):
        result = result.get("items", result.get("fields", []))
    if not isinstance(result, list):
        raise ValueError("formato inesperado no catálogo de campos personalizados")
    fields = load_field_catalog(root, config)
    for item in result:
        if not isinstance(item, dict):
            continue
        field_id = str(item.get("FIELD_NAME", item.get("fieldName", item.get("ID", item.get("id", ""))))).strip()
        if not field_id:
            continue
        label = str(item.get("EDIT_FORM_LABEL", item.get("editFormLabel", item.get("TITLE", item.get("title", field_id)))) or field_id).strip()
        fields[field_id] = label
    path = catalog_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps({"schema_version": 1, "fields": fields}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)
    return dict(sorted(fields.items()))


def start_worker(root: Path, config_path: Path, store: RunStore, run_id: str, *, startup_timeout: float = 3.0) -> int:
    root = root.resolve()
    config_path = config_path.resolve()
    try:
        config_argument = str(config_path.relative_to(root))
    except ValueError as exc:
        raise RunStoreError("a configuração do worker deve permanecer dentro do projeto") from exc
    state = store.read(run_id)
    if state.get("status") in {"running", "starting", "cancelling"}:
        raise RunStoreError("a auditoria já está em execução")
    if state.get("status") in {"completed", "partial"}:
        raise RunStoreError("a auditoria já encerrou todas as consultas; a cobertura pode ser consultada nos resultados")
    store.clear_cancel(run_id)
    run_dir = store.run_dir(run_id)
    log_path = run_dir / "worker.log"
    interpreter = worker_interpreter(root)
    environment = os.environ.copy()
    source_path = str((root / "src").resolve())
    existing_pythonpath = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = source_path + (os.pathsep + existing_pythonpath if existing_pythonpath else "")
    environment["PYTHONUTF8"] = "1"
    environment["PYTHONUNBUFFERED"] = "1"
    command = [str(interpreter), "-m", "bitrix24_task_auditor.worker", "--config", config_argument, "--run-id", run_id]
    store.update(run_id, status="starting", worker_python=str(interpreter), worker_log=str(log_path.relative_to(root)))
    kwargs: dict[str, Any] = {
        "cwd": str(root), "env": environment, "stdin": subprocess.DEVNULL, "close_fds": True,
    }
    if os.name == "nt":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 0)
        startupinfo.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
        kwargs["startupinfo"] = startupinfo
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        kwargs["start_new_session"] = True
    try:
        with log_path.open("a", encoding="utf-8") as log:
            process = subprocess.Popen(command, stdout=log, stderr=log, **kwargs)
    except (OSError, subprocess.SubprocessError) as exc:
        message = _sanitized(f"Falha ao iniciar o worker com o Python local: {exc}", environment)
        store.record_worker_failure(run_id, message)
        with log_path.open("a", encoding="utf-8") as log:
            log.write(message + "\n")
        raise RunStoreError(message) from exc
    store.update(run_id, pid=process.pid)
    deadline = time.monotonic() + max(startup_timeout, 0.0)
    while True:
        current = store.read(run_id)
        if current.get("status") == "failed":
            raise RunStoreError(str(current.get("last_error") or "O worker falhou durante a inicialização."))
        if current.get("worker_started_at") or current.get("status") in (TERMINAL_STATUSES - {"failed"}) | {"running"}:
            return process.pid
        exit_code = process.poll()
        if exit_code is not None:
            tail = _sanitized(store.log_tail(run_id), environment)
            message = f"O worker encerrou durante a inicialização (código {exit_code})."
            if tail:
                message += f" Detalhe: {tail[-1500:]}"
            store.record_worker_failure(run_id, message, exit_code=exit_code)
            raise RunStoreError(message)
        if time.monotonic() >= deadline:
            break
        time.sleep(0.05)
    return process.pid


def create_and_start(root: Path, config_path: Path, store: RunStore, spec: QuerySpec, *, confirmed_fields: list[str]) -> dict[str, Any]:
    spec.validate(confirmed_custom_fields=confirmed_fields, project_root=root)
    for item in store.list_runs():
        if item.get("status") in ACTIVE_STATUSES:
            reconcile_worker_state(store, item["id"])
    if any(item.get("status") in ACTIVE_STATUSES for item in store.list_runs()):
        raise RunStoreError("já existe uma auditoria ativa; aguarde ou solicite o cancelamento antes de iniciar outra")
    state = store.create(spec)
    start_worker(root, config_path, store, state["id"])
    return store.read(state["id"])
