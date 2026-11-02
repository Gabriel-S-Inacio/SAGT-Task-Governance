from __future__ import annotations

import json
import os
import re
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .migration_models import MigrationPlan, canonical_digest, utc_now


class MigrationStoreError(RuntimeError):
    pass


PLAN_ID_RE = re.compile(r"^[0-9]{8}T[0-9]{6}_[a-f0-9]{8}$")


class MigrationStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.base = self.root / ".state" / "migrations"

    def plan_dir(self, plan_id: str) -> Path:
        if not PLAN_ID_RE.fullmatch(plan_id):
            raise MigrationStoreError("Identificador de migração inválido.")
        path = (self.base / plan_id).resolve()
        try:
            path.relative_to(self.base.resolve())
        except ValueError as exc:
            raise MigrationStoreError("Identificador de migração fora do diretório permitido.") from exc
        return path

    def create(self, plan: MigrationPlan) -> Path:
        directory = self.plan_dir(plan.plan_id)
        directory.mkdir(parents=True, exist_ok=False)
        self._atomic_json(directory / "plan.json", plan.to_dict())
        self._atomic_json(directory / "state.json", {
            "schema_version": 2,
            "plan_id": plan.plan_id,
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "status": "PLANEJADA",
            "dry_run": True,
            "cancel_requested": False,
            "results": {},
            "dry_run_results": {},
            "live_results": {},
            "dry_run_status": "PENDENTE",
            "live_status": "PENDENTE",
        })
        return directory

    def save_plan(self, plan: MigrationPlan) -> None:
        plan.refresh_digest()
        self._atomic_json(self.plan_dir(plan.plan_id) / "plan.json", plan.to_dict())
        approval = self.read_optional(plan.plan_id, "approval.json")
        if approval and approval.get("plan_digest") != plan.digest:
            self._atomic_json(self.plan_dir(plan.plan_id) / "approval_revoked.json", {
                "revoked_at": utc_now(),
                "reason": "O plano ou a prévia foi alterado após a aprovação.",
                "previous_approval": approval,
            })

    def read_plan(self, plan_id: str) -> MigrationPlan:
        return MigrationPlan.from_dict(self._read_json(self.plan_dir(plan_id) / "plan.json"))

    def read_state(self, plan_id: str) -> dict[str, Any]:
        return self._read_json(self.plan_dir(plan_id) / "state.json")

    @staticmethod
    def results_for_mode(state: dict[str, Any], dry_run: bool) -> dict[str, Any]:
        """Return one execution checkpoint without mutating legacy state files."""
        key = "dry_run_results" if dry_run else "live_results"
        current = state.get(key)
        if isinstance(current, dict):
            return dict(current)
        legacy = state.get("results", {})
        if not isinstance(legacy, dict):
            return {}
        return {
            str(task_id): dict(result)
            for task_id, result in legacy.items()
            if isinstance(result, dict) and bool(result.get("dry_run")) is dry_run
        }

    def update_state(self, plan_id: str, **changes: Any) -> dict[str, Any]:
        path = self.plan_dir(plan_id) / "state.json"
        with self._file_lock(path.with_name("state.json.lock"), timeout=10.0):
            state = self._read_json(path)
            state.update(changes)
            state["updated_at"] = utc_now()
            self._atomic_json(path, state)
            return state

    @staticmethod
    def confirmation_phrase(plan: MigrationPlan) -> str:
        source = plan.source_user_id or "MULTIPLAS_ORIGENS"
        destination = plan.target_user_id or "CONFORME_REGRAS"
        return f"MIGRAR {source} PARA {destination} - {plan.digest[:12]}"

    @staticmethod
    def _authorized_patches(plan: MigrationPlan) -> dict[str, dict[str, Any]]:
        authorized = set(plan.task_ids)
        return {
            item.task_id: item.patch
            for item in plan.operations
            if item.task_id in authorized and item.eligibility == "ELEGIVEL" and item.patch
        }

    @staticmethod
    def _authorized_corrections(plan: MigrationPlan) -> dict[str, dict[str, list[str]]]:
        authorized = set(plan.task_ids)
        return {
            item.task_id: item.authorized_side_effect_corrections
            for item in plan.operations
            if item.task_id in authorized
            and item.eligibility == "ELEGIVEL"
            and item.patch
            and item.authorized_side_effect_corrections
        }

    def approve(self, plan_id: str, phrase: str, *, actor: str = "operador_local") -> dict[str, Any]:
        plan = self.read_plan(plan_id)
        if plan.logic_version < 5:
            raise MigrationStoreError("O plano usa lógica anterior ao hotfix de integridade; gere uma nova prévia.")
        if plan.digest != canonical_digest(plan.binding()):
            raise MigrationStoreError("Digest do plano inválido; gere novamente a prévia.")
        if phrase.strip() != self.confirmation_phrase(plan):
            raise MigrationStoreError("Frase de confirmação não corresponde ao plano exato.")
        patches = self._authorized_patches(plan)
        if plan.import_blockers or not plan.task_ids or set(patches) != set(plan.task_ids):
            raise MigrationStoreError("Plano bloqueado ou sem operações elegíveis após a prévia.")
        approval = {
            "schema_version": 1,
            "plan_id": plan.plan_id,
            "approved_at": utc_now(),
            "actor": actor,
            "plan_digest": plan.digest,
            "spreadsheet_sha256": plan.spreadsheet_sha256,
            "source_user_id": plan.source_user_id,
            "target_user_id": plan.target_user_id,
            "migration_mode": plan.migration_mode,
            "owner_field_id": plan.owner_field_id,
            "owner_rules": [item.__dict__ for item in plan.owner_rules],
            "rule_snapshots": plan.rule_snapshots,
            "correction_evidence": plan.correction_evidence,
            "operation_type": plan.operation_type,
            "logic_version": plan.logic_version,
            "selected_roles": plan.selected_roles,
            "authorized_task_ids": plan.task_ids,
            "authorized_patches": patches,
            "authorized_side_effect_corrections": self._authorized_corrections(plan),
        }
        self._atomic_json(self.plan_dir(plan_id) / "approval.json", approval)
        self.update_state(plan_id, status="APROVADA")
        return approval

    def valid_approval(self, plan_id: str) -> dict[str, Any]:
        plan = self.read_plan(plan_id)
        approval = self.read_optional(plan_id, "approval.json")
        if not approval or approval.get("plan_digest") != plan.digest:
            raise MigrationStoreError("Aprovação ausente ou invalidada pela alteração do plano.")
        expected = {
            "spreadsheet_sha256": plan.spreadsheet_sha256,
            "source_user_id": plan.source_user_id,
            "target_user_id": plan.target_user_id,
            "migration_mode": plan.migration_mode,
            "owner_field_id": plan.owner_field_id,
            "owner_rules": [item.__dict__ for item in plan.owner_rules],
            "rule_snapshots": plan.rule_snapshots,
            "correction_evidence": plan.correction_evidence,
            "operation_type": plan.operation_type,
            "logic_version": plan.logic_version,
            "selected_roles": plan.selected_roles,
            "authorized_task_ids": plan.task_ids,
            "authorized_patches": self._authorized_patches(plan),
            "authorized_side_effect_corrections": self._authorized_corrections(plan),
        }
        if not expected["authorized_task_ids"] or set(expected["authorized_patches"]) != set(expected["authorized_task_ids"]):
            raise MigrationStoreError("O plano não possui operações elegíveis para sustentar a aprovação.")
        if any(approval.get(key, {} if key == "authorized_side_effect_corrections" else None) != value for key, value in expected.items()):
            raise MigrationStoreError("Aprovação não corresponde ao conjunto exato de operações.")
        return approval

    def append_attempt(self, plan_id: str, value: dict[str, Any]) -> None:
        path = self.plan_dir(plan_id) / "attempts.ndjson"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def read_attempts(self, plan_id: str) -> list[dict[str, Any]]:
        path = self.plan_dir(plan_id) / "attempts.ndjson"
        if not path.exists():
            return []
        values: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                values.append(json.loads(line))
        return values

    def request_cancel(self, plan_id: str) -> None:
        self.update_state(plan_id, cancel_requested=True, status="CANCELAMENTO_SOLICITADO")

    def clear_cancel(self, plan_id: str) -> None:
        self.update_state(plan_id, cancel_requested=False)

    def list_plans(self) -> list[dict[str, Any]]:
        if not self.base.exists():
            return []
        output = []
        for directory in self.base.iterdir():
            if directory.is_dir() and PLAN_ID_RE.fullmatch(directory.name):
                try:
                    state = self.read_state(directory.name)
                    plan = self.read_plan(directory.name)
                    output.append({**state, "digest": plan.digest, "source_user_id": plan.source_user_id, "target_user_id": plan.target_user_id})
                except (OSError, ValueError, MigrationStoreError, json.JSONDecodeError):
                    continue
        return sorted(output, key=lambda item: item.get("created_at", ""), reverse=True)

    def read_optional(self, plan_id: str, filename: str) -> dict[str, Any] | None:
        if filename not in {"approval.json", "approval_revoked.json", "final.json"}:
            raise MigrationStoreError("Arquivo de migração não permitido.")
        path = self.plan_dir(plan_id) / filename
        return self._read_json(path) if path.exists() else None

    @contextmanager
    def execution_lock(self, plan_id: str, *, timeout: float = 0.1) -> Iterator[None]:
        path = self.plan_dir(plan_id) / "execution.lock"
        with self._file_lock(path, timeout=timeout):
            yield

    @contextmanager
    def _file_lock(self, path: Path, *, timeout: float) -> Iterator[None]:
        handle = path.open("a+b")
        try:
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            deadline = time.monotonic() + timeout
            while True:
                try:
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if time.monotonic() >= deadline:
                        raise MigrationStoreError(f"Timeout ao obter o bloqueio {path.name}.") from exc
                    time.sleep(0.02)
            try:
                yield
            finally:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise MigrationStoreError(f"Não foi possível ler {path.name}: {exc}") from exc
        if not isinstance(value, dict):
            raise MigrationStoreError(f"Conteúdo inválido em {path.name}.")
        return value

    @staticmethod
    def _atomic_json(path: Path, value: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
