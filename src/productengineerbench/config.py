"""Configuration loading with Pydantic Settings.

The behavior intentionally mirrors the previous implementation: we first read
YAML files from paths supplied via environment variables and do **not** change
precedence or defaults. Environment variables still control which files are
read; their contents drive the settings values.
"""

from pathlib import Path
from typing import Any, Dict, Optional

import yaml
from pydantic import BaseModel, Field, HttpUrl
from pydantic_settings import BaseSettings, SettingsConfigDict


class RepositorySetup(BaseModel):
    commands: Optional[list[str]] = None
    env: Dict[str, str] = Field(default_factory=dict)


class RepositoryConfig(BaseModel):
    name: str
    url: HttpUrl | str  # allow plain strings for git+ssh urls
    revision: str
    prd: str
    tech_spec: str
    setup: RepositorySetup = Field(default_factory=RepositorySetup)


class RepoSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="allow")

    repository: RepositoryConfig


class StoryMachineConfig(BaseSettings):
    model_config = SettingsConfigDict(extra="allow")

    package: str
    version: str


def _load_yaml(path: Path) -> Dict[str, Any]:
    content = path.read_text()
    parsed = yaml.safe_load(content)
    return parsed if parsed else {}


def load_repo_settings(path: Path) -> RepoSettings:
    data = _load_yaml(path)
    return RepoSettings.model_validate(data)


def load_storymachine_settings(path: Path) -> StoryMachineConfig:
    data = _load_yaml(path)
    return StoryMachineConfig.model_validate(data)
