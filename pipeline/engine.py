"""Small structural contract shared by engine adapters and scan orchestration."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Iterable, Protocol, TYPE_CHECKING

if TYPE_CHECKING:
    from platform_runner import Runner


@dataclass(frozen=True)
class Checkpoint:
    """Native resume path, bytes used for identity, preview input and optional config."""
    path: Path
    source: Path
    preview: Path
    step: int
    config: Path | None = None

    @property
    def order(self):
        return self.step, self.source.stat().st_mtime_ns if self.source.exists() else 0


@dataclass(frozen=True)
class TrainingOutput:
    ply: Path
    config: Path | None = None


class Engine(Protocol):
    name: str
    checkpoint_suffix: str
    requires_cuda: bool

    def validate_settings(self, settings: dict, cap: int) -> None: ...
    def runtime_versions(self, recorded: dict) -> dict: ...
    def preflight(self) -> list[dict] | None: ...
    def run(self, runner: Runner) -> Path: ...
    def checkpoint(self, path: Path) -> Checkpoint: ...
    def checkpoint_candidates(self, folder: Path) -> Iterable[Checkpoint]: ...
    def valid_checkpoint(self, checkpoint: Checkpoint, cap: int) -> bool: ...
    def training_output(self, folder: Path) -> TrainingOutput: ...
    def preview_ply(self, source: Path, temporary: Path, log: Path) -> Path: ...


def checkpoint_step(path):
    """Extract an iteration from a native file or directory name."""
    numbers = re.findall(r'\d+', path.stem)
    return int(numbers[-1]) if numbers else -1
