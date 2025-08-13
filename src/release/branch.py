"""Manage the release workflow for a single branch."""

import logging
import subprocess

import requests
from rich import print
from rich.markdown import Markdown
from rich.prompt import Confirm, Prompt

from .command import Command, step
from .git import FC_NIXOS
from .markdown import MarkdownTree
from .state import Release
from .utils import (
    machine_prefix,
    trigger_rolling_release_update,
    verify_machines_are_current,
    wait_for_successful_hydra_release_build,
)

CHANGELOG = FC_NIXOS.path / "changelog.d" / "CHANGELOG.md"


def generate_nixpkgs_changelog(old_rev: str, new_rev: str) -> MarkdownTree:
    res = MarkdownTree()
    res["Detailed Changes"] += (
        f"- [platform code](https://github.com/flyingcircusio/fc-nixos/"
        f"compare/{old_rev}...{new_rev})"
    )

    pversions_path = "release/package-versions.json"
    try:
        old_pversions = FC_NIXOS.show(old_rev, pversions_path)
        new_pversions = FC_NIXOS.show(new_rev, pversions_path)

        lines = []
        for pkg_name in old_pversions:
            old = old_pversions.get(pkg_name, {}).get("version")
            new = new_pversions.get(pkg_name, {}).get("version")

            if not old and new:
                lines.append(f"{pkg_name}: (old version missing)")
            elif old and not new:
                lines.append(f"{pkg_name}: (new version missing)")
            elif old != new:
                lines.append(f"{pkg_name}: {old} -> {new}")

        if lines:
            res["NixOS XX.XX platform"] += (
                "- Pull upstream NixOS changes, security fixes, and "
                "package updates:" + "".join("\n    - " + m for m in lines)
            )
    except subprocess.CalledProcessError:
        logging.warning(
            f"Could not find '{pversions_path}'. Continuing without package "
            f"versions diff..."
        )

    versions_path = "release/versions.json"
    try:
        old_versions = FC_NIXOS.show(old_rev, versions_path)
        new_versions = FC_NIXOS.show(new_rev, versions_path)
        old_nixpkgs_rev = old_versions["nixpkgs"]["rev"]
        new_nixpkgs_rev = new_versions["nixpkgs"]["rev"]
        if old_nixpkgs_rev != new_nixpkgs_rev:
            res["Detailed Changes"] += (
                f"- [nixpkgs/upstream changes](https://github.com/"
                f"flyingcircusio/nixpkgs/compare/"
                f"{old_nixpkgs_rev}...{new_nixpkgs_rev})"
            )
    except subprocess.CalledProcessError:
        logging.warning(
            f"Could not find '{versions_path}' file. Continuing without "
            f"nixpkgs changelog..."
        )

    return res


class Branch(Command):
    """Perform release actions for [cyan]{self.branch.nixos_version}"""

    track_steps_on_attr = "branch"

    def __init__(self, release: Release, nixos_version: str):
        self.release = release
        self.branch = release.branches[nixos_version]

    @step
    def review_staging_prod_changes(self):
        """Spot-check changes"""
        FC_NIXOS.pull()
        print(
            f"The following commits will be merged from "
            f"[cyan]{self.branch.branch_stag}[/cyan] to "
            f"[cyan]{self.branch.branch_prod}[/cyan]:"
        )
        print()
        print(
            FC_NIXOS._git(
                "cherry", self.branch.branch_prod, self.branch.branch_stag, "-v"
            )
        )

        while not Confirm.ask(
            "[purple]Have you spot-checked the commits for general sanity?[/purple]"
        ):
            pass

    @step(skip_seen=False)
    def check_hydra(self):
        """Wait for clean build for [cyan]{self.branch.branch_stag}[/cyan] on Hydra"""
        orig_stag_rev = self.branch.orig_staging_commit
        self.staging_build = wait_for_successful_hydra_release_build(
            self.branch.branch_stag, orig_stag_rev
        )
        print(
            f"[green]Detected green build [cyan]{self.staging_build.eval_id}[/cyan] on Hydra.[/green]"
        )

    @step(skip_seen=False)
    def check_releasetest_machines(self):
        """Verify release test staging machines are up to date."""
        # Trigger rolling release update as here to prevent getting stuck here when the directory hasn't yet
        # updated its view on the staging env
        trigger_rolling_release_update()
        prefix = machine_prefix(self.branch.nixos_version)
        verify_machines_are_current(
            f"{prefix}stag", self.staging_build.nix_name
        )

    @step
    def collect_changelog(self):
        """Collect the changelog."""
        FC_NIXOS.checkout(self.branch.branch_stag)
        if not CHANGELOG.parent.exists():
            logging.warning(
                f"Could not find '{str(CHANGELOG.parent)}'. Skipping changelog generation..."
            )
            return

        new_fragment = MarkdownTree.collect(
            filter(CHANGELOG.__ne__, CHANGELOG.parent.rglob("*.md")), FC_NIXOS
        )

        old_changelog = MarkdownTree.from_str(self.branch.changelog)
        old_changelog["Detailed Changes"] = ""
        self.branch.changelog = (old_changelog | new_fragment).to_str()

        new_fragment.strip()
        new_fragment.add_header(f"Release {self.release.id}")
        new_changelog = new_fragment.to_str()
        if CHANGELOG.exists():
            new_changelog += "\n" + CHANGELOG.read_text()
        CHANGELOG.write_text(new_changelog)

        try:
            FC_NIXOS._git("add", str(CHANGELOG.relative_to(FC_NIXOS.path)))
            FC_NIXOS._git("commit", "-m", "Collect changelog fragments")
        except subprocess.CalledProcessError:
            logging.error(
                "Failed to commit Changelog. Commit it manually and continue after the `collect_changelog` stage"
            )
            raise

    @step(skip_seen=False)
    def check_sensu(self):
        """Verify Sensu is green."""
        prefix = machine_prefix(self.branch.nixos_version)
        print(
            f"Staging: releasetest sensu checks green? Look at https://sensu.rzob.gocept.net/#/clients?q={prefix}"
        )
        while not Confirm.ask("Is sensu green?"):
            pass

    @step
    def merge(self):
        """Merge staging into production."""
        FC_NIXOS.checkout(self.branch.branch_prod)
        msg = (
            f"Merge branch '{self.branch.branch_stag}' into "
            f"'{self.branch.branch_prod}' for release {self.release.id}"
        )
        FC_NIXOS._git("merge", "-m", msg, self.branch.branch_stag)
        self.branch.new_production_commit = FC_NIXOS.rev_parse(
            self.branch.branch_prod
        )

    @step(skip_seen=False)
    def backmerge(self):
        """Backmerge production to dev."""
        FC_NIXOS.checkout(self.branch.branch_dev)
        msg = f"Backmerge branch '{self.branch.branch_prod}' into '{self.branch.branch_dev}'' for release {self.release.id}"
        FC_NIXOS._git("merge", "-m", msg, self.branch.branch_prod)

    @step
    def add_nixpkgs_changelog(self):
        """Add nixpkgs changelog."""
        old_rev = FC_NIXOS.rev_parse("origin/" + self.branch.branch_prod)
        new_rev = FC_NIXOS.rev_parse(self.branch.branch_prod)

        new_fragment = MarkdownTree.from_str(self.branch.changelog)
        new_fragment |= generate_nixpkgs_changelog(old_rev, new_rev)

        print(Markdown(new_fragment.to_str()))
        print()

        while (
            Prompt.ask(
                "Do you want to [green]edit[/green] the fragment or [green]continue[/green]?",
                choices=["edit", "continue"],
            )
            == "edit"
        ):
            new_fragment.open_in_editor()
            print(Markdown(new_fragment.to_str()))
            print()

        self.branch.changelog = new_fragment.to_str()

    @step(skip_seen=False)
    def push(self):
        """Push repository."""
        remote = FC_NIXOS._git("remote", "get-url", "--push", "origin").strip()
        print(f"[bold purple]Pushing changes to [green]{remote}[/green] ...")

        FC_NIXOS._git(
            "push",
            "origin",
            self.branch.branch_dev,
            self.branch.branch_stag,
            self.branch.branch_prod,
        )

    # Hydra now starts building the production branch

    @step(skip_seen=False)
    def check_hydra_production(self):
        """Verify Hydra build for [cyan]{self.branch.branch_prod}[/cyan]."""
        prod_rev = self.branch.new_production_commit
        self.production_build = wait_for_successful_hydra_release_build(
            self.branch.branch_prod, prod_rev
        )
        self.branch.hydra_eval_id = str(self.production_build.eval_id)
        print(
            f"[green]Detected green build [cyan]{self.branch.hydra_eval_id}[/cyan] on Hydra.[/green]"
        )

    @step
    def create_directory_release(self):
        """Create directory release."""
        print(f"Create directory release for {self.branch.branch_prod}")
        print()
        print(" > https://directory.fcio.net/environments")

        print()
        print(f"Release name: [cyan]{self.release.id}[/cyan]")
        print(f"  Hydra eval: [cyan]{self.branch.hydra_eval_id}[/cyan]")
        print(f"  Valid from: [cyan]{self.release.date} 7:00 PM UTC[/cyan]")
        print()

        while not Confirm.ask("Did you add the release?"):
            pass

    @step(skip_seen=False)
    def verify_production_machines(self):
        """Verify production release test machines."""
        prefix = machine_prefix(self.branch.nixos_version)
        verify_machines_are_current(
            f"{prefix}prod", self.production_build.nix_name
        )
        print(
            "Check maintenance log, check switch output for unexpected service restarts, compare with changelog, impact properly documented? You can edit the changelog in the next step."
        )
        while not Confirm.ask("Ready to continue?"):
            pass

    @step
    def update_changelog_with_urls(self):
        """Update global changelog."""
        metadata_url = f"https://my.flyingcircus.io/releases/metadata/fc-{self.branch.nixos_version}-production/{self.release.id}"
        changelog = MarkdownTree.from_str(self.branch.changelog)
        changelog["Detailed Changes"] += f"- [metadata]({metadata_url})"
        try:
            r = requests.get(metadata_url, timeout=5)
            r.raise_for_status()
            channel_url = r.json()["channel_url"]
            changelog["Detailed Changes"] += f"- [channel url]({channel_url})"
            logging.info("Added channel url fragment")
        except (requests.RequestException, KeyError):
            logging.warning(
                "Failed to retrieve channel url. Please add it manually in the next step"
            )
        changelog.open_in_editor()
        self.branch.changelog = changelog.to_str()

        print(Markdown(changelog.to_str()))
        print()

        while (
            Prompt.ask(
                "Do you want to [green]edit[/green] the changelog or [green]continue[/green]?",
                choices=["edit", "continue"],
            )
            == "edit"
        ):
            changelog.open_in_editor()
            print(Markdown(changelog.to_str()))
            print()

    @step
    def mark_keep(self):
        """Mark [cyan]release[/cyan] job to keep indefinitely."""

        print(
            f" > The job is reachable here: https://hydra.flyingcircus.io/eval/{self.branch.hydra_eval_id}?filter=release"
        )
        print()
        while not Confirm.ask(
            "[purple]Have you set the `keep` flag for this job?"
        ):
            pass

    @step
    def mark_as_tested(self):
        """Mark the branch as [green]tested[/green]."""
        self.branch.tested = True
        print("All good, the release for this branch is now done.")


class Ignore(Command):
    def __init__(self, release: Release, nixos_version: str):
        self.release = release
        self.nixos_version = nixos_version

    def __call__(self):
        try:
            branch = self.release.branches[self.nixos_version]
        except KeyError:
            print(
                f"[red]'branch {self.nixos_version}' was not scheduled for release or is unknown"
            )
            raise RuntimeError()
        else:
            branch.ignored = True
            print(
                f"[cyan]Ignoring [bold]{self.nixos_version}[/bold] during this release cycle."
            )
