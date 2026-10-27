"""Checkpoints of a KiCad project's design files in a shadow git repo.

The repo lives in <project>/.copilot/git and only ever tracks KiCad design
files, so it never touches the user's own git repo (if any). Every restore
first saves the current state as an "[auto]" checkpoint, so nothing is lost
and an undo can itself be undone.
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .kicad import Project
from .netlist import Netlist

AUTO = "[auto] "


class CheckpointError(RuntimeError):
    pass


@dataclass
class Checkpoint:
    id: str
    label: str
    time: str
    tree: str

    @property
    def auto(self) -> bool:
        return self.label.startswith(AUTO)

    def to_dict(self) -> dict:
        return {"id": self.id, "label": self.label, "time": self.time}


class CheckpointStore:
    def __init__(self, project: Project):
        self.project = project
        self.git_dir = project.state_dir / "git"
        self.cache_dir = project.state_dir / "cache"
        if not (self.git_dir / "HEAD").exists():
            self._git("init", "-q", "-b", "main", bare_init=True)
            self._git("config", "user.name", "AI PCB Copilot")
            self._git("config", "user.email", "copilot@localhost")
            self._git("config", "core.autocrlf", "false")
            self._git("config", "core.longpaths", "true")
            self._git("config", "core.bare", "false")

    # ------------------------------------------------------------- plumbing
    def _git(self, *args: str, bare_init: bool = False, binary: bool = False, check: bool = True):
        if bare_init:
            cmd = ["git", "init", "-q", "--bare", "-b", "main", str(self.git_dir)]
            subprocess.run(cmd, check=True, capture_output=True)
            return ""
        cmd = ["git", f"--git-dir={self.git_dir}", f"--work-tree={self.project.dir}", *args]
        proc = subprocess.run(cmd, capture_output=True, text=not binary)
        if check and proc.returncode != 0:
            err = proc.stderr if not binary else proc.stderr.decode(errors="replace")
            raise CheckpointError(f"git {' '.join(args)} failed: {err.strip()}")
        return proc.stdout if binary else proc.stdout.strip()

    def _stage(self) -> None:
        files = [str(p.relative_to(self.project.dir)) for p in self.project.tracked_files()]
        # Drop index entries for files that no longer exist, then add current ones.
        self._git("rm", "-r", "-q", "--cached", "--ignore-unmatch", ".", check=False)
        if files:
            self._git("add", "-f", "--", *files)

    def _head(self) -> str | None:
        out = self._git("rev-parse", "-q", "--verify", "HEAD", check=False)
        return out or None

    def _current_tree(self) -> str:
        self._stage()
        return self._git("write-tree")

    # ------------------------------------------------------------- public API
    def checkpoint(self, label: str) -> tuple[Checkpoint, bool]:
        """Snapshot the design files. Returns (checkpoint, created); not created if nothing changed."""
        tree = self._current_tree()
        head = self._head()
        if head and self._git("rev-parse", f"{head}^{{tree}}") == tree:
            return self.get(head), False
        args = ["commit-tree", tree, "-m", label or "checkpoint"]
        if head:
            args[2:2] = ["-p", head]
        commit = self._git(*args)
        self._git("update-ref", "HEAD", commit)
        return self.get(commit), True

    def get(self, rev: str) -> Checkpoint:
        out = self._git("log", "-1", "--format=%H%x1f%s%x1f%cI%x1f%T", rev)
        h, label, time, tree = out.split("\x1f")
        return Checkpoint(h[:10], label, time, tree)

    def list(self, limit: int = 50, include_auto: bool = True) -> list[Checkpoint]:
        if not self._head():
            return []
        out = self._git("log", f"-{limit * 3}", "--format=%H%x1f%s%x1f%cI%x1f%T")
        cps = []
        for line in out.splitlines():
            h, label, time, tree = line.split("\x1f")
            cp = Checkpoint(h[:10], label, time, tree)
            if include_auto or not cp.auto:
                cps.append(cp)
        return cps[:limit]

    def dirty(self) -> bool:
        head = self._head()
        return head is None or self._git("rev-parse", f"{head}^{{tree}}") != self._current_tree()

    def restore(self, rev: str) -> dict:
        """Make the design files match a checkpoint. Saves the current state first."""
        target = self.get(rev)
        saved, created = self.checkpoint(f"{AUTO}before restoring {target.id} ({target.label})")
        target_files = set(self._git("ls-tree", "-r", "--name-only", target.id).splitlines())
        current_files = {str(p.relative_to(self.project.dir)).replace(os.sep, "/") for p in self.project.tracked_files()}
        for rel in current_files - target_files:
            (self.project.dir / rel).unlink(missing_ok=True)
        self._extract(target.id, self.project.dir)
        # Record the restored state so HEAD tracks the working files again.
        after, _ = self.checkpoint(f"{AUTO}restored {target.id} ({target.label})")
        return {
            "restored": target.to_dict(),
            "saved_previous_state_as": saved.id,
            "files": sorted(target_files),
        }

    def undo(self) -> dict:
        """Go back to the newest manual checkpoint whose content differs from the current files."""
        current = self._current_tree()
        for cp in self.list(limit=200, include_auto=False):
            if cp.tree != current:
                return self.restore(cp.id)
        raise CheckpointError("Nothing to undo: no earlier checkpoint differs from the current files.")

    # ----------------------------------------------------------- snapshots
    def _extract(self, rev: str, dest: Path) -> None:
        data = self._git("archive", "--format=tar", rev, binary=True)
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            tar.extractall(dest, filter="data")

    def netlist(self, rev: str | None) -> Netlist:
        """Netlist of a checkpoint (or of the current files when rev is None), cached by tree hash."""
        if rev is None:
            return Netlist.from_schematic(self.project.sch)
        cp = self.get(rev)
        cached = self.cache_dir / f"{cp.tree}.net.xml"
        if cached.exists():
            return Netlist.from_xml(cached)
        with tempfile.TemporaryDirectory(prefix="copilot-") as tmp:
            self._extract(cp.id, Path(tmp))
            sch = Path(tmp) / self.project.sch.name
            if not sch.exists():
                raise CheckpointError(f"Checkpoint {cp.id} has no {sch.name}")
            nl = Netlist.from_schematic(sch, Path(tmp) / "out")
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy(Path(tmp) / "out" / "netlist.xml", cached)
            return nl

    def erc(self, rev: str | None) -> list:
        """ERC violations of a checkpoint (or the current files), cached by tree hash."""
        from . import checks

        if rev is None:
            return checks.run_erc(self.project.sch, self.project.state_dir / "erc-current")
        cp = self.get(rev)
        cached = self.cache_dir / f"{cp.tree}.erc.json"
        if not cached.exists():
            with tempfile.TemporaryDirectory(prefix="copilot-") as tmp:
                self._extract(cp.id, Path(tmp))
                checks.run_erc(Path(tmp) / self.project.sch.name, Path(tmp) / "out")
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy(Path(tmp) / "out" / "erc.json", cached)
        import json

        return checks.parse_erc(json.loads(cached.read_text(encoding="utf-8")))

    def file_at(self, rev: str, rel_path: str) -> bytes:
        return self._git("show", f"{self.get(rev).id}:{rel_path}", binary=True)


def now_label(prefix: str) -> str:
    return f"{prefix} {datetime.now().strftime('%H:%M:%S')}"
