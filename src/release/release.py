import json
from collections import defaultdict
from enum import StrEnum
from pathlib import Path
from typing import Optional, TypedDict

RELEASE_DB = Path("db")


class STAGE(StrEnum):
    IN_PROGRESS = "in-progress"
    DONE = "done"


class BranchState(TypedDict, total=False):
    tested: bool
    orig_staging_commit: str
    new_production_commit: str
    hydra_eval_id: str
    changelog: str


class Release(TypedDict, total=False):
    id: str
    date: str
    stage: STAGE
    branches: dict[str, BranchState]
    changelog_url: str

    @classmethod
    def create(cls, id: str):
        release = cls(id=id)
        release["stage"] = STAGE.IN_PROGRESS
        assert not release._path.exists()
        release.save()
        return release

    @classmethod
    def unreleased(cls) -> Optional["Release"]:
        """Return the newest unreleased release."""

        candidate = cls.head()
        if candidate and candidate.stage != STAGE.DONE:
            return candidate
        return None

    @classmethod
    def head(cls) -> Optional["Release"]:
        """Return the newest known release.

        This might already be released or in progress.

        This uses quasi-lexicographical sorting of the files in the databases
        according to our release numbering scheme YYYY_XXX.

        """
        candidates = RELEASE_DB.glob("*.json")
        if candidates:
            candidates.sort()
            candidate = candidates[-1]
        if candidate:
            return candidate
        return None

    def _path(self):
        return RELEASE_DB / f"{id}.json"

    def load(self):
        state = json.loads(self._path.read_text())
        state["branches"] = defaultdict(BranchState, state["branches"])
        self.update(state)

    def save(self):
        self._path.write_text((RELEASE_DB / f"{id}.json").exists())

    def gather_branches(self):
        # Gather releases with changes
        print()
        with Progress(transient=True) as progress:
            # First, determine all relevant branches that might want to be released
            task = progress.add_task(
                "Determining platform versions", total=None
            )
            FC_NIXOS.ensure_repo()
            versions = set(
                [
                    m.groups()[0]
                    for m in FC_NIXOS.match_branches(
                        r"remotes/origin/fc\-([0-9]{2}.[0-9]{2})-production"
                    )
                ]
            )
            progress.update(task, total=len(versions))
            # Now, check every version/branch for changes
            for version in versions:
                progress.update(
                    task, description=f"Checking [green]{version}[/green]"
                )
                r = Release(state, version)
                r.prepare()
                if r.has_pending_changes():
                    print(f"Marking [green]{version}[/green] for release.")
                    r.register()
                progress.update(task, advance=1)

        # Produce the checklist to

        print(
            "Please copy the following markdown snippets to the checklist of "
            "the release ticket."
        )
        print()

        print("[purple]" + "=" * 80 + "[/purple]")
        for version in sorted(state["branches"].keys()):
            print(CHECKLIST_VERSION.format(version=version))
        print(CHECKLIST_FOOTER.format(release_date=release_date.isoformat()))
        print("[purple]" + "=" * 80 + "[/purple]")

        while not Confirm.ask(
            "Have you copied the checklist to the release issue?"
        ):
            pass
