"""The connector interface: where requirements come from, and where results are reported back to.

The orchestrator talks to a project's source only through these four calls:

    items = trigger.fetch()                    # what is waiting, without taking it
    if trigger.claim(item, run_dir):           # take one item; writes run_dir/requirements.md
        ...create the run, then trigger.ack(item, run)
    trigger.report(run, summary_markdown)      # once the run is done, failed or blocked

A connector keeps whatever it needs to find the item again in `item.ref`, which the orchestrator stores
in the run's state.json as run["origin"], so report() still works after a restart or days later.

How other sources would plug in (not implemented yet):

- Jira: fetch() polls a JQL query such as
  `project = APP AND status = "Ready for Katana" ORDER BY priority DESC`, mapping each issue to an Item
  (key = issue key, title = summary, ref = {"key": ..., "url": ...}). claim() transitions the issue to
  "In Progress" and only counts the claim if the transition succeeds, so two factories never take the same
  issue; it writes the summary, description and acceptance criteria as requirements.md. ack() adds a
  comment with the run id. report() comments SUMMARY.md and transitions to "Done" or, for a failed or
  blocked run, to a status such as "Needs input". Credentials come from the macOS keychain, never from
  settings.json.
- Confluence: fetch() runs a CQL search for pages carrying a label such as `katana-ready`. claim() swaps
  the label for `katana-claimed` and exports the page body as markdown. report() adds a page comment and
  swaps the label for `katana-done` or `katana-failed`.

Register a new connector in factory/triggers/__init__.py and add its settings to settings.validate().
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Item:
    key: str               # unique within the source: a file name, an issue key, a page id
    title: str
    ref: dict = field(default_factory=dict)  # what report() needs later; stored as run["origin"]["ref"]
    waiting: bool = False  # seen but not ready to claim yet (for example, a file still being written)


class Trigger:
    """Base class for a requirement source. Subclasses override fetch() and claim(); ack() and report()
    are optional because some sources have nowhere to report to."""

    name = "base"

    def __init__(self, project: Path, conf: dict, log=print):
        self.project, self.conf, self.log = Path(project), conf, log

    def fetch(self) -> list:
        """Items waiting to be processed, oldest first. Must not change the source."""
        raise NotImplementedError

    def claim(self, item: Item, run_dir: Path) -> bool:
        """Takes the item so no other scan processes it, and writes run_dir/requirements.md.
        Returns False if the item is gone or was taken by someone else."""
        raise NotImplementedError

    def ack(self, item: Item, run: dict) -> None:
        """Called once the run exists: confirm to the source that work has started."""

    def report(self, run: dict, summary: str) -> None:
        """Called when the run is done, failed or blocked: send the outcome back to the source."""
