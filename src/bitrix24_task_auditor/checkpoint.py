from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


class CheckpointError(RuntimeError):
    pass


def query_fingerprint(params: dict[str, Any]) -> str:
    canonical = json.dumps(params, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass
class CheckpointStore:
    path: Path
    spool_path: Path

    def read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": 1}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CheckpointError(f"checkpoint inválido: {exc}") from exc
        if not isinstance(value, dict) or value.get("version") != 1:
            raise CheckpointError("versão de checkpoint incompatível")
        return value

    def begin(self, fingerprint: str, *, incremental: bool, overlap_minutes: int) -> dict[str, Any]:
        state = self.read()
        active = state.get("active")
        if active:
            if active.get("fingerprint") != fingerprint:
                raise CheckpointError("há uma coleta interrompida com configuração diferente; conclua ou use --reset-checkpoint")
            if not self.spool_path.exists():
                raise CheckpointError("checkpoint ativo sem arquivo de spool; use --reset-checkpoint")
            return active
        updated_since = None
        if incremental and state.get("completed_watermark"):
            try:
                watermark = datetime.fromisoformat(state["completed_watermark"])
                updated_since = (watermark - timedelta(minutes=overlap_minutes)).isoformat()
            except (TypeError, ValueError) as exc:
                raise CheckpointError("marca temporal inválida no checkpoint") from exc
        self.spool_path.parent.mkdir(parents=True, exist_ok=True)
        self.spool_path.write_text("", encoding="utf-8")
        active = {
            "fingerprint": fingerprint,
            "next": 0,
            "updated_since": updated_since,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "collection_complete": False,
        }
        state["active"] = active
        self._write(state)
        return active

    def append_page(self, items: list[dict[str, Any]], next_cursor: int | None) -> None:
        state = self.read()
        if not state.get("active"):
            raise CheckpointError("nenhuma coleta ativa")
        with self.spool_path.open("a", encoding="utf-8", newline="\n") as stream:
            for item in items:
                stream.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        state["active"]["next"] = next_cursor
        state["active"]["collection_complete"] = next_cursor is None
        self._write(state)

    def read_spool(self) -> list[dict[str, Any]]:
        try:
            lines = self.spool_path.read_text(encoding="utf-8").splitlines()
            values = [json.loads(line) for line in lines if line.strip()]
        except (OSError, json.JSONDecodeError) as exc:
            raise CheckpointError(f"spool inválido: {exc}") from exc
        if any(not isinstance(item, dict) for item in values):
            raise CheckpointError("spool contém registro inválido")
        return values

    def complete(self, watermark: datetime | None) -> None:
        state = self.read()
        active = state.get("active")
        if not active or not active.get("collection_complete"):
            raise CheckpointError("coleta ainda não foi concluída")
        state.pop("active", None)
        state["last_completed_at"] = datetime.now(timezone.utc).isoformat()
        if watermark:
            state["completed_watermark"] = watermark.isoformat()
        self._write(state)
        self.spool_path.unlink(missing_ok=True)

    def reset(self) -> None:
        self.path.unlink(missing_ok=True)
        self.spool_path.unlink(missing_ok=True)

    def _write(self, value: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temp_name = tempfile.mkstemp(prefix=self.path.name + ".", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, self.path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
