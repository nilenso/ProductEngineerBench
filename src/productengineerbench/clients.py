"""Infrastructure helpers abstracted behind protocols for easy testing."""

from __future__ import annotations

import os
import subprocess
import tarfile
import tempfile
from enum import Enum
from pathlib import Path
from typing import Protocol

import s3fs


class Phase(Enum):
    PENDING = "pending"
    IMPLEMENTING = "implementing"
    EVALUATING = "evaluating"
    COMPLETED = "completed"
    FAILED = "failed"


class SyncClient(Protocol):
    def sync_directory(
        self, source: Path, destination: str, delete: bool = False
    ) -> None: ...

    def upload_file(self, source: Path, destination: str) -> None: ...

    def download_file(self, source: str, destination: Path) -> None: ...


class GitClient(Protocol):
    def fetch_all(self) -> None: ...

    def checkout(self, branch: str, ref: str | None = None) -> None: ...

    def reset_hard(self, ref: str) -> None: ...

    def clone(self, url: str, target: Path) -> None: ...

    def rev_parse(self, ref: str = "HEAD") -> str | None: ...

    def has_changes(self) -> bool: ...

    def commit_all(self, message: str) -> str | None: ...


class TarArchiver:
    """Stateless tar helpers used by sync clients."""

    @staticmethod
    def create_archive(source: Path) -> Path:
        fd, tmp_path = tempfile.mkstemp(prefix=f"{source.name}_", suffix=".tar.gz")
        os.close(fd)
        archive_path = Path(tmp_path)
        with tarfile.open(archive_path, mode="w:gz") as archive:
            archive.add(source, arcname=".")
        return archive_path

    @staticmethod
    def extract_archive(archive: Path, destination: Path) -> None:
        destination.mkdir(parents=True, exist_ok=True)
        with tarfile.open(archive, mode="r:gz") as tar:
            tar.extractall(destination)


class S3SyncClient:
    """Minimal S3 sync built on s3fs; focuses on our upload/download needs."""

    def __init__(self, bucket: str) -> None:
        self.bucket = bucket
        self.fs = s3fs.S3FileSystem(anon=False)

    @staticmethod
    def _dest_path(bucket: str, prefix: str) -> str:
        clean = prefix.lstrip("/")
        return f"{bucket}/{clean}" if clean else bucket

    def sync_directory(
        self, source: Path, destination: str, delete: bool = False
    ) -> None:
        dest = self._dest_path(self.bucket, destination)
        # s3fs put is recursive when given a directory.
        self.fs.put(str(source), dest, recursive=True)
        if delete:
            self._delete_extraneous(source, dest)

    def _delete_extraneous(self, local_root: Path, remote_root: str) -> None:
        # Best-effort deletion to mimic prior --delete semantics.
        local_files = {
            str(p.relative_to(local_root)) for p in local_root.rglob("*") if p.is_file()
        }
        remote_files = set(self.fs.find(remote_root))
        for remote_file in remote_files:
            try:
                rel = Path(remote_file).relative_to(remote_root)
            except ValueError:
                continue
            if str(rel) not in local_files:
                self.fs.rm(remote_file)

    def upload_file(self, source: Path, destination: str) -> None:
        dest = self._dest_path(self.bucket, destination)
        self.fs.put(str(source), dest)

    def download_file(self, source: str, destination: Path) -> None:
        src = self._dest_path(self.bucket, source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        self.fs.get(src, str(destination))


class SubprocessGitClient:
    def __init__(self, repo_dir: Path) -> None:
        self.repo_dir = repo_dir

    @staticmethod
    def _run_git(
        repo_dir: Path, *args: str, check: bool = True
    ) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args],
            cwd=repo_dir,
            check=check,
            capture_output=False,
            text=True,
        )

    def fetch_all(self) -> None:
        self._run_git(self.repo_dir, "fetch", "--all", "--tags", "--prune", check=False)

    def checkout(self, branch: str, ref: str | None = None) -> None:
        if ref:
            self._run_git(self.repo_dir, "checkout", "-B", branch, ref)
        else:
            self._run_git(self.repo_dir, "checkout", branch)

    def reset_hard(self, ref: str) -> None:
        self._run_git(self.repo_dir, "reset", "--hard", ref)

    def clone(self, url: str, target: Path) -> None:
        subprocess.run(["git", "clone", url, str(target)], check=True)

    def rev_parse(self, ref: str = "HEAD") -> str | None:
        try:
            result = subprocess.run(
                ["git", "rev-parse", ref],
                cwd=self.repo_dir,
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError:
            return None
        return result.stdout.strip()

    def has_changes(self) -> bool:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=self.repo_dir,
            check=True,
            capture_output=True,
            text=True,
        )
        return bool(result.stdout.strip())

    def commit_all(self, message: str) -> str | None:
        if not self.has_changes():
            return self.rev_parse("HEAD")
        try:
            self._run_git(self.repo_dir, "add", "--all")
            self._run_git(self.repo_dir, "commit", "-m", message)
        except subprocess.CalledProcessError:
            return self.rev_parse("HEAD")
        return self.rev_parse("HEAD")
