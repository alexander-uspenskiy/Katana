"""FolderTrigger: one requirements .md file per feature in <project>/input/."""
from __future__ import annotations

import shutil
import time
from pathlib import Path

from .base import Item, Trigger

SETTLE_SECONDS = 60  # a scheduled scan ignores files modified more recently (still being written)


class FolderTrigger(Trigger):
    name = "folder"

    def __init__(self, project: Path, conf: dict, log=print, settle: int = SETTLE_SECONDS):
        super().__init__(project, conf, log)
        self.input_dir, self.settle = self.project / "input", settle

    def files(self) -> list:
        """Requirement files in input/: not README.md, not drafts starting with . or _."""
        return [p for p in sorted(self.input_dir.glob("*.md"))
                if p.name.lower() != "readme.md" and not p.name.startswith((".", "_"))]

    def fetch(self) -> list:
        items = []
        for src in self.files():
            try:
                age = time.time() - src.stat().st_mtime
            except OSError:
                continue  # removed since the glob
            items.append(Item(key=src.name, title=src.stem, ref={"file": src.name}, waiting=age < self.settle))
        return items

    def claim(self, item: Item, run_dir: Path) -> bool:
        src = self.input_dir / item.key
        try:
            shutil.move(str(src), run_dir / "requirements.md")
        except FileNotFoundError:
            return False
        return True

    def report(self, run: dict, summary: str) -> None:
        pass  # the outcome is in runs/<id>/SUMMARY.md, next to the requirements it came from
