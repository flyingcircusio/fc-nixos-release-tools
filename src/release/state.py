import datetime
from pathlib import Path
from typing import Optional

from pydantic import BaseModel

from .git import FC_NIXOS

STATE_FILE = Path("state.json")


class HydraReleaseBuild(BaseModel):
    nix_name: str
    eval_id: str


class Release(BaseModel):
    id: str | None = None
    date: datetime.date | None = None
    branches: dict[str, "Branch"] = {}
    steps: set = set()

    @property
    def changelog_url(self):
        if not self.id:
            return None
        return f"https://doc.flyingcircus.io/platform/changes/{self.year}/r{self.release_num}.html"

    @property
    def year(self):
        return self.id.split("_", maxsplit=1)[0]

    @property
    def release_num(self):
        return self.id.split("_", maxsplit=1)[1]

    @property
    def work_branches(self):
        """Branches that are not to be ignored."""
        return {k: v for (k, v) in self.branches.items() if not v.ignored}


class Branch(BaseModel):
    nixos_version: str
    tested: bool = False
    ignored: bool = False
    orig_staging_commit: str = ""
    new_production_commit: str = ""
    hydra_eval_id: str = ""
    changelog: str = ""
    steps: set = set()
    staging_build: Optional[HydraReleaseBuild] = None
    production_build: Optional[HydraReleaseBuild] = None

    @property
    def branch_dev(self):
        return f"fc-{self.nixos_version}-dev"

    @property
    def branch_stag(self):
        return f"fc-{self.nixos_version}-staging"

    @property
    def branch_prod(self):
        return f"fc-{self.nixos_version}-production"

    def has_pending_changes(self):
        return FC_NIXOS.is_ancestor(self.branch_stag, self.branch_prod)

    def prepare(self):
        FC_NIXOS.ensure_repo()
        FC_NIXOS.checkout(self.branch_dev, reset=True, clean=True)
        FC_NIXOS.checkout(self.branch_stag, reset=True, clean=True)
        FC_NIXOS.checkout(self.branch_prod, reset=True, clean=True)

        if not self.orig_staging_commit:
            self.orig_staging_commit = FC_NIXOS.rev_parse(self.branch_stag)


def load():
    if not STATE_FILE.exists():
        return Release()
    return Release.model_validate_json(STATE_FILE.read_text())


def save(release):
    STATE_FILE.write_text(release.model_dump_json())
