import json
import os
import shutil
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


def _iso_now() -> str:
    """Return an ISO-8601 timestamp in UTC without microseconds."""
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    """Write JSON atomically using a temp file and rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, path)
    _fsync_dir(path.parent)


def _atomic_write_text(path: Path, content: str) -> None:
    """Write a UTF-8 text file atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, path)
    _fsync_dir(path.parent)


def _fsync_dir(directory: Path) -> None:
    """Ensure directory metadata hits disk."""
    try:
        dir_fd = os.open(str(directory), os.O_RDONLY)
    except FileNotFoundError:
        return
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


class RunState:
    """State manager responsible for durable run metadata and logs."""

    SCHEMA_VERSION = 1

    def __init__(
        self,
        run_root: Path,
        repo_config: Dict[str, Any],
        storymachine_config: Dict[str, Any],
        *,
        resume: bool = False,
        force_resume: bool = False,
    ) -> None:
        self.run_root = run_root
        self._repo_config = repo_config
        self._storymachine_config = storymachine_config
        self.resume = resume
        self.force_resume = force_resume

        self._manifest_cache: Optional[Dict[str, Any]] = None
        self._story_index_cache: Optional[List[str]] = None
        self._sessions: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self._lock = threading.Lock()

    @property
    def repo_dir(self) -> Path:
        return self.run_root / "repo"

    @property
    def stories_dir(self) -> Path:
        return self.run_root / "stories"

    @property
    def configs_dir(self) -> Path:
        return self.run_root / "configs"

    @property
    def manifest_path(self) -> Path:
        return self.run_root / "manifest.json"

    @property
    def stories_index_path(self) -> Path:
        return self.run_root / "stories_index.json"

    @property
    def checkpoints_path(self) -> Path:
        return self.run_root / "checkpoints.jsonl"

    def story_dir(self, story_file: str) -> Path:
        return self.stories_dir / Path(story_file).stem

    def status_path(self, story_file: str) -> Path:
        return self.story_dir(story_file) / "status.json"

    def log_path(self, story_file: str, phase: str) -> Path:
        return self.story_dir(story_file) / f"{phase}.jsonl"

    def result_path(self, story_file: str) -> Path:
        return self.story_dir(story_file) / "result.md"

    def prepare_directories(self) -> None:
        self.run_root.mkdir(parents=True, exist_ok=True)
        self.configs_dir.mkdir(parents=True, exist_ok=True)
        self.stories_dir.mkdir(parents=True, exist_ok=True)

    def copy_configs(self, repo_config_path: Path, story_config_path: Path) -> None:
        if repo_config_path.exists():
            target = self.configs_dir / repo_config_path.name
            if not target.exists():
                shutil.copy2(repo_config_path, target)
        if story_config_path.exists():
            target = self.configs_dir / story_config_path.name
            if not target.exists():
                shutil.copy2(story_config_path, target)

    def load_manifest(self) -> Optional[Dict[str, Any]]:
        if self._manifest_cache is not None:
            return self._manifest_cache
        if not self.manifest_path.exists():
            return None
        data = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        if data.get("schema_version") != self.SCHEMA_VERSION:
            raise RuntimeError(
                f"Unsupported manifest schema_version {data.get('schema_version')}"
            )
        self._manifest_cache = data
        return data

    def initialize_manifest(
        self,
        *,
        run_id: str,
        image: str,
        tools: Dict[str, Any],
        models: Dict[str, Any],
        repo_meta: Dict[str, Any],
    ) -> Dict[str, Any]:
        existing = self.load_manifest()
        if existing:
            return existing
        now = _iso_now()
        manifest = {
            "schema_version": self.SCHEMA_VERSION,
            "run_id": run_id,
            "created_at": now,
            "updated_at": now,
            "completed_at": None,
            "status": "pending",
            "image": image,
            "tools": tools,
            "models": models,
            "repo": repo_meta,
        }
        _atomic_write_json(self.manifest_path, manifest)
        self._manifest_cache = manifest
        return manifest

    def flush(self) -> None:
        """Flush cached manifest/story index to disk if present.

        This keeps checkpoint syncing predictable if callers mutated caches
        directly (unlikely today) but want to guarantee durability before
        triggering a remote sync.
        """
        if self._manifest_cache is not None:
            _atomic_write_json(self.manifest_path, self._manifest_cache)
        if self._story_index_cache is not None:
            payload = {"order": list(self._story_index_cache)}
            _atomic_write_json(self.stories_index_path, payload)

    def update_manifest_models(self, models: Dict[str, Any]) -> None:
        manifest = self.load_manifest()
        if not manifest:
            return
        manifest["models"] = models
        _atomic_write_json(self.manifest_path, manifest)
        self._manifest_cache = manifest

    def update_manifest_repo(self, repo_meta: Dict[str, Any]) -> None:
        manifest = self.load_manifest()
        if not manifest:
            return
        manifest["repo"] = repo_meta
        _atomic_write_json(self.manifest_path, manifest)
        self._manifest_cache = manifest

    def update_manifest_status(self, status: str) -> None:
        manifest = self.load_manifest()
        if not manifest:
            return
        timestamp = _iso_now()
        manifest["status"] = status
        manifest["updated_at"] = timestamp
        if status in {"success", "failure", "cancelled"}:
            manifest["completed_at"] = timestamp
        else:
            manifest["completed_at"] = None
        _atomic_write_json(self.manifest_path, manifest)
        self._manifest_cache = manifest

    def validate_resume_compatibility(self) -> None:
        if not self.resume:
            return
        manifest = self.load_manifest()
        if not manifest:
            return
        repo_manifest = manifest.get("repo", {})
        repo_cfg = self._repo_config.get("repository", {})
        errors = []
        if repo_manifest.get("url") and repo_manifest.get("url") != repo_cfg.get("url"):
            errors.append(
                f"Repo URL mismatch (manifest={repo_manifest.get('url')} "
                f"cfg={repo_cfg.get('url')})"
            )
        if repo_manifest.get("base_revision") and repo_manifest.get(
            "base_revision"
        ) != repo_cfg.get("revision"):
            errors.append(
                f"Repo revision mismatch (manifest={repo_manifest.get('base_revision')} "
                f"cfg={repo_cfg.get('revision')})"
            )
        sm_manifest = manifest.get("tools", {}).get("storymachine", {})
        sm_cfg = self._storymachine_config
        if sm_manifest:
            if (
                sm_manifest.get("package")
                and sm_manifest.get("package") != sm_cfg.get("package")
            ) or (
                sm_manifest.get("version")
                and sm_manifest.get("version") != sm_cfg.get("version")
            ):
                errors.append(
                    "Storymachine package/version mismatch between manifest and config"
                )
        if errors and not self.force_resume:
            raise RuntimeError(
                "Resume compatibility check failed:\n  - " + "\n  - ".join(errors)
            )

    def get_story_order(self) -> List[str]:
        if self._story_index_cache is not None:
            return list(self._story_index_cache)
        if self.stories_index_path.exists():
            data = json.loads(self.stories_index_path.read_text(encoding="utf-8"))
            order = data.get("order", [])
            self._story_index_cache = list(order)
            return list(order)
        return []

    def set_story_order(self, filenames: Iterable[str]) -> None:
        ordered = list(filenames)
        payload = {"order": ordered}
        _atomic_write_json(self.stories_index_path, payload)
        self._story_index_cache = ordered

    def rebuild_story_index_from_files(self) -> List[str]:
        files = sorted(
            [path.name for path in self.stories_dir.glob("*.md") if path.is_file()]
        )
        self.set_story_order(files)
        return files

    def ensure_story_status(self, story_file: str) -> Dict[str, Any]:
        path = self.status_path(story_file)
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            return data
        status = {
            "schema_version": self.SCHEMA_VERSION,
            "story_file": story_file,
            "phase": "pending",
            "started_at": None,
            "updated_at": None,
            "completed_at": None,
            "commits": {"before": None, "after_implement": None},
            "error": None,
        }
        self.story_dir(story_file).mkdir(parents=True, exist_ok=True)
        _atomic_write_json(path, status)
        return status

    def load_status(self, story_file: str) -> Dict[str, Any]:
        return self.ensure_story_status(story_file)

    def update_status(self, story_file: str, **updates: Any) -> Dict[str, Any]:
        status = self.ensure_story_status(story_file)
        commits = updates.pop("commits", None)
        timestamp = _iso_now()
        if "started_at" in updates:
            if status.get("started_at") is None and updates["started_at"]:
                status["started_at"] = updates["started_at"]
        if "completed_at" in updates:
            status["completed_at"] = updates["completed_at"]
        if "phase" in updates:
            status["phase"] = updates["phase"]
        if "error" in updates:
            status["error"] = updates["error"]
        if commits:
            status.setdefault("commits", {"before": None, "after_implement": None})
            for key, value in commits.items():
                if value is not None:
                    status["commits"][key] = value
        for key, value in updates.items():
            if key in {"started_at", "phase", "error", "completed_at"}:
                continue
            status[key] = value
        status["updated_at"] = timestamp
        _atomic_write_json(self.status_path(story_file), status)
        return status

    def mark_implementing(self, story_file: str) -> Dict[str, Any]:
        status = self.ensure_story_status(story_file)
        updates: Dict[str, Any] = {"phase": "implementing", "error": None}
        if status.get("started_at") is None:
            updates["started_at"] = _iso_now()
        return self.update_status(story_file, **updates)

    def mark_evaluating(self, story_file: str) -> Dict[str, Any]:
        return self.update_status(story_file, phase="evaluating", error=None)

    def mark_failed(self, story_file: str, error: str) -> Dict[str, Any]:
        return self.update_status(story_file, phase="failed", error=error)

    def mark_completed(self, story_file: str) -> Dict[str, Any]:
        return self.update_status(
            story_file, phase="completed", error=None, completed_at=_iso_now()
        )

    def record_commits(
        self,
        story_file: str,
        *,
        before: Optional[str] = None,
        after: Optional[str] = None,
    ) -> Dict[str, Any]:
        commits: Dict[str, Optional[str]] = {}
        if before is not None:
            commits["before"] = before
        if after is not None:
            commits["after_implement"] = after
        return self.update_status(story_file, commits=commits)

    def start_session(self, story_file: str, phase: str) -> str:
        story_dir = self.story_dir(story_file)
        story_dir.mkdir(parents=True, exist_ok=True)
        session_id = str(uuid.uuid4())
        with self._lock:
            self._sessions[(story_file, phase)] = {"session_id": session_id, "seq": 0}
        return session_id

    def log_event(
        self,
        story_file: str,
        phase: str,
        event_type: str,
        content: Any,
    ) -> None:
        with self._lock:
            session = self._sessions.get((story_file, phase))
            if not session:
                session_id = str(uuid.uuid4())
                session = {"session_id": session_id, "seq": 0}
                self._sessions[(story_file, phase)] = session
            log_path = self.log_path(story_file, phase)
            entry = {
                "session_id": session["session_id"],
                "seq": session["seq"],
                "timestamp": time.time(),
                "type": event_type,
                "content": content,
            }
            session["seq"] += 1
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def write_result(self, story_file: str, content: str) -> None:
        _atomic_write_text(self.result_path(story_file), content)

    def determine_resume_sha(self) -> Optional[str]:
        order = self.get_story_order()
        if not order:
            return None
        candidate: Optional[str] = None
        for story_file in order:
            status = self.ensure_story_status(story_file)
            commits = status.get("commits") or {}
            after = commits.get("after_implement")
            before = commits.get("before")
            if after:
                candidate = after
            elif before:
                candidate = before
        return candidate

    def story_result_exists(self, story_file: str) -> bool:
        return self.result_path(story_file).exists()

    def story_artifacts_dir(self, story_file: str) -> Path:
        path = self.story_dir(story_file) / "artifacts"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def record_checkpoint(
        self, reason: str, *, extra: Optional[Dict[str, Any]] = None
    ) -> None:
        """Append a checkpoint marker for auditing sync attempts.

        The marker is intentionally lightweight; it should never throw
        unless the filesystem itself is failing.
        """
        payload: Dict[str, Any] = {
            "timestamp": _iso_now(),
            "reason": reason,
        }
        if extra:
            payload["extra"] = extra

        self.checkpoints_path.parent.mkdir(parents=True, exist_ok=True)
        with self.checkpoints_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        _fsync_dir(self.checkpoints_path.parent)
