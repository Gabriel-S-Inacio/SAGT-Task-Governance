from __future__ import annotations

import json
import os
import re
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .web_models import QuerySpec


class RunStoreError(RuntimeError):
    pass


TEMPLATE_RE = re.compile(r"^[A-Za-z0-9À-ÿ][A-Za-z0-9À-ÿ_.-]{0,79}$")
TRANSIENT_WINDOWS_ERRORS = {5, 32, 33}
IO_RETRY_ATTEMPTS = 6
IO_RETRY_INITIAL_SECONDS = 0.025
IO_RETRY_MAX_SECONDS = 0.4


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class RunStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.runs_dir = self.root / ".state" / "web_runs"
        self.templates_dir = self.root / ".state" / "templates"

    def create(self, spec: QuerySpec) -> dict[str, Any]:
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "_" + uuid.uuid4().hex[:8]
        run_dir = self.run_dir(run_id)
        run_dir.mkdir(parents=True, exist_ok=False)
        state: dict[str, Any] = {
            "schema_version": 1, "id": run_id, "created_at": utc_now(), "updated_at": utc_now(),
            "status": "queued", "partial": False, "spec": spec.to_dict(),
            "progress": {
                "unit_index": 0, "unit_total": 0, "current_unit": "", "next_cursor": 0,
                "pages": 0, "records": 0, "unique_tasks": 0, "links": 0,
                "attempts": 0, "errors": [], "elapsed_seconds": 0.0,
                "completed_units": [], "pagination_proofs": [], "current_scope_index": None,
                "current_user": None, "current_role": None, "current_unit_pages": 0,
                "current_unit_records": 0, "current_api_total": None,
            },
            "result": {},
        }
        self._atomic_json(run_dir / "run.json", state)
        return state

    def run_dir(self, run_id: str) -> Path:
        if not re.fullmatch(r"[0-9]{8}T[0-9]{6}_[a-f0-9]{8}", run_id):
            raise RunStoreError("identificador de auditoria inválido")
        path = (self.runs_dir / run_id).resolve()
        try:
            path.relative_to(self.runs_dir.resolve())
        except ValueError as exc:
            raise RunStoreError("identificador aponta para fora do histórico") from exc
        return path

    def read(self, run_id: str) -> dict[str, Any]:
        path = self.run_dir(run_id) / "run.json"
        with self._file_lock(path):
            return self._read_state_unlocked(run_id, path)

    def _read_state_unlocked(self, run_id: str, path: Path) -> dict[str, Any]:
        try:
            text = self._retry_transient(lambda: path.read_text(encoding="utf-8"))
            value = json.loads(text)
        except (OSError, json.JSONDecodeError) as exc:
            raise RunStoreError(f"não foi possível ler a auditoria {run_id}: {exc}") from exc
        if not isinstance(value, dict) or value.get("id") != run_id:
            raise RunStoreError("estado de auditoria inválido")
        return value

    def write(self, run_id: str, state: dict[str, Any]) -> None:
        state = dict(state)
        state["updated_at"] = utc_now()
        path = self.run_dir(run_id) / "run.json"
        with self._file_lock(path):
            self._write_state_unlocked(run_id, path, state)

    def _write_state_unlocked(self, run_id: str, path: Path, state: dict[str, Any]) -> None:
        try:
            self._atomic_json(path, state)
        except OSError as exc:
            raise RunStoreError(f"não foi possível gravar a auditoria {run_id}: {exc}") from exc

    def update(self, run_id: str, **fields: Any) -> dict[str, Any]:
        path = self.run_dir(run_id) / "run.json"
        with self._file_lock(path):
            state = self._read_state_unlocked(run_id, path)
            state.update(fields)
            state["updated_at"] = utc_now()
            self._write_state_unlocked(run_id, path, state)
            return state

    def append_records(self, run_id: str, filename: str, records: list[dict[str, Any]]) -> None:
        if filename not in {"tasks.ndjson", "links.ndjson", "users.ndjson"}:
            raise RunStoreError("arquivo de spool não permitido")
        if not records:
            return
        path = self.run_dir(run_id) / filename
        with self._file_lock(path):
            needs_separator = path.exists() and path.stat().st_size > 0
            if needs_separator:
                with path.open("rb") as existing:
                    existing.seek(-1, os.SEEK_END)
                    needs_separator = existing.read(1) != b"\n"
            with path.open("a", encoding="utf-8", newline="\n") as stream:
                if needs_separator:
                    stream.write("\n")
                for record in records:
                    stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
                stream.flush()
                os.fsync(stream.fileno())

    def read_records(self, run_id: str, filename: str) -> list[dict[str, Any]]:
        values, issues = self.read_records_report(run_id, filename)
        if issues:
            issue = issues[0]
            raise RunStoreError(f"spool inválido em {filename}: linha {issue['line']} — {issue['error']}")
        return values

    def read_records_report(self, run_id: str, filename: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        if filename not in {"tasks.ndjson", "links.ndjson", "users.ndjson"}:
            raise RunStoreError("arquivo de spool não permitido")
        path = self.run_dir(run_id) / filename
        if not path.exists():
            return [], []
        with self._file_lock(path):
            try:
                lines = path.read_text(encoding="utf-8", errors="strict").splitlines()
            except (OSError, UnicodeDecodeError) as exc:
                return [], [{"line": 0, "error": str(exc), "filename": filename}]
        values: list[dict[str, Any]] = []
        issues: list[dict[str, Any]] = []
        for number, line in enumerate(lines, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError("registro não é um objeto JSON")
                values.append(value)
            except (json.JSONDecodeError, ValueError) as exc:
                issues.append({"line": number, "error": str(exc), "filename": filename})
        return values, issues

    @contextmanager
    def _file_lock(self, data_path: Path, *, timeout: float = 10.0):
        lock_path = data_path.with_name(data_path.name + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock = self._retry_transient(lambda: lock_path.open("a+b"))
        try:
            if lock.tell() == 0:
                lock.write(b"\0")
                lock.flush()
            deadline = time.monotonic() + timeout
            while True:
                try:
                    lock.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise RunStoreError(f"timeout ao bloquear o arquivo {data_path.name}")
                    time.sleep(0.05)
            try:
                yield
            finally:
                lock.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        finally:
            lock.close()

    @staticmethod
    def _is_transient_io_error(exc: OSError) -> bool:
        if isinstance(exc, PermissionError):
            return True
        return getattr(exc, "winerror", None) in TRANSIENT_WINDOWS_ERRORS

    @classmethod
    def _retry_transient(cls, operation):
        delay = IO_RETRY_INITIAL_SECONDS
        for attempt in range(IO_RETRY_ATTEMPTS):
            try:
                return operation()
            except OSError as exc:
                if not cls._is_transient_io_error(exc) or attempt == IO_RETRY_ATTEMPTS - 1:
                    raise
                time.sleep(delay)
                delay = min(delay * 2, IO_RETRY_MAX_SECONDS)
        raise AssertionError("retry de I/O terminou sem resultado")

    def request_cancel(self, run_id: str) -> None:
        (self.run_dir(run_id) / "cancel.request").write_text(utc_now(), encoding="utf-8")
        path = self.run_dir(run_id) / "run.json"
        with self._file_lock(path):
            state = self._read_state_unlocked(run_id, path)
            state["cancel_requested"] = True
            if state.get("status") in {"queued", "starting", "running"}:
                state["status"] = "cancelling"
            state["updated_at"] = utc_now()
            self._write_state_unlocked(run_id, path, state)

    def clear_cancel(self, run_id: str) -> None:
        (self.run_dir(run_id) / "cancel.request").unlink(missing_ok=True)
        path = self.run_dir(run_id) / "run.json"
        with self._file_lock(path):
            state = self._read_state_unlocked(run_id, path)
            state.pop("cancel_requested", None)
            state["updated_at"] = utc_now()
            self._write_state_unlocked(run_id, path, state)

    def cancel_requested(self, run_id: str) -> bool:
        return (self.run_dir(run_id) / "cancel.request").exists()

    def record_worker_failure(self, run_id: str, message: str, *, exit_code: int | None = None) -> dict[str, Any]:
        path = self.run_dir(run_id) / "run.json"
        with self._file_lock(path):
            state = self._read_state_unlocked(run_id, path)
            if state.get("status") in {"completed", "partial", "limited", "cancelled"}:
                return state
            safe_message = str(message).strip()[:2000] or "O worker terminou sem informar a causa."
            state["status"] = "failed"
            state["partial"] = True
            state["last_error"] = safe_message
            state["worker_finished_at"] = utc_now()
            if exit_code is not None:
                state["worker_exit_code"] = int(exit_code)
            progress = state.setdefault("progress", {})
            errors = progress.setdefault("errors", [])
            if safe_message not in errors:
                errors.append(safe_message)
            state["updated_at"] = utc_now()
            self._write_state_unlocked(run_id, path, state)
            return state

    def log_tail(self, run_id: str, *, max_characters: int = 4000) -> str:
        path = self.run_dir(run_id) / "worker.log"
        if not path.exists():
            return ""
        try:
            return path.read_text(encoding="utf-8", errors="replace")[-max_characters:].strip()
        except OSError:
            return ""

    def list_runs(self) -> list[dict[str, Any]]:
        if not self.runs_dir.exists():
            return []
        values = []
        for path in self.runs_dir.glob("*/run.json"):
            values.append(self.read(path.parent.name))
        return sorted(values, key=lambda item: str(item.get("created_at", "")), reverse=True)

    def save_template(self, name: str, spec: QuerySpec) -> Path:
        if not TEMPLATE_RE.fullmatch(name):
            raise RunStoreError("nome de modelo inválido")
        self.templates_dir.mkdir(parents=True, exist_ok=True)
        path = self.templates_dir / f"{name}.json"
        self._atomic_json(path, {"schema_version": 1, "name": name, "saved_at": utc_now(), "spec": spec.to_dict()})
        return path

    def list_templates(self) -> list[str]:
        if not self.templates_dir.exists():
            return []
        return sorted(path.stem for path in self.templates_dir.glob("*.json") if TEMPLATE_RE.fullmatch(path.stem))

    def load_template(self, name: str) -> QuerySpec:
        if not TEMPLATE_RE.fullmatch(name):
            raise RunStoreError("nome de modelo inválido")
        try:
            value = json.loads((self.templates_dir / f"{name}.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RunStoreError(f"não foi possível carregar o modelo: {exc}") from exc
        if not isinstance(value, dict) or not isinstance(value.get("spec"), dict):
            raise RunStoreError("modelo inválido")
        return QuerySpec.from_dict(value["spec"])

    @classmethod
    def _atomic_json(cls, path: Path, value: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            # The descriptor is closed by the context manager before replace.
            # Windows may still briefly deny replacement while another process
            # or a security scanner holds a sharing handle.
            cls._retry_transient(lambda: os.replace(temp_name, path))
        finally:
            if os.path.exists(temp_name):
                try:
                    cls._retry_transient(lambda: os.unlink(temp_name))
                except OSError:
                    # Preserve the original write/replace error; a uniquely named
                    # temp file is safer than masking the failure during cleanup.
                    pass
