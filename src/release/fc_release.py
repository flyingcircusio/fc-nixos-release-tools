#!/usr/bin/env python3

import argparse
import json
import os
import re
from pathlib import Path
from subprocess import CalledProcessError, check_output, run

from release.nixpkgs_changelog import (
    filter_and_merge_commit_msgs,
    get_interesting_commit_msgs,
    version_diff_lines,
)

DEFAULT_NIXOS_VERSIONS = ["21.05", "23.05", "23.11", "24.05"]
STEPS = [
    "prepare",
    "skip_no_change",
    "diff_release",
    "collect_changelog",
    "merge",
    "backmerge",
    "add_detailed_changelog",
    "push",
]

EDITOR = os.environ.get("EDITOR", "nano")
WORK_DIR = Path("work")
FC_NIXOS = WORK_DIR / "fc-nixos"
FC_DOCS = WORK_DIR / "doc"
FC_NIXPKGS = WORK_DIR / "nixpkgs"
CHANGELOG = FC_NIXOS / "changelog.d" / "CHANGELOG.md"
TEMP_CHANGELOG = CHANGELOG.with_suffix(CHANGELOG.suffix + ".tmp")

SKIP_BRANCH = object()

NIXPKGS_CHANGELOG_TEMPLATE = """\
< !--
Generated Nixpkgs Changelog. Adjust as necessary.
-->

## NixOS {nixos_version} platform
- Pull upstream NixOS changes, security fixes and package updates:
{nixpkgs_changelog}

## Detailed Changes
- NixOS {nixos_version}: [platform code](https://github.com/flyingcircusio/fc-nixos/compare/{old_rev}...{new_rev}),
 [nixpkgs/upstream changes](https://github.com/flyingcircusio/nixpkgs/compare/{old_nixpkgs_rev}...{new_nixpkgs_rev})
"""

SHORT_CHANGELOG_TEMPLATE = """\
## Detailed Changes
- NixOS {nixos_version}: [platform code](https://github.com/flyingcircusio/fc-nixos/compare/{old_rev}...{new_rev})
"""


def release_id_type(arg_value):
    if not re.compile("^[0-9]{4}_[0-9]{3}$").match(arg_value):
        raise argparse.ArgumentTypeError(
            "invalid release id format. Expected: YYYY_NNN"
        )
    return arg_value


def git(path: Path, *cmd: str, check=True, **kw):
    return run(["git"] + list(cmd), cwd=path, check=check, **kw)


def git_stdout(path: Path, *cmd: str, **kw):
    return check_output(["git"] + list(cmd), cwd=path, text=True, **kw)


def rev_parse(path: Path, rev: str):
    return git_stdout(path, "rev-parse", "--verify", rev).strip()


def load_json(path: Path, rev: str, obj_path: str):
    return json.loads(git_stdout(path, "show", rev + ":" + obj_path))


def git_remote(path: Path):
    out = git_stdout(path, "remote", "-v")
    return re.findall(r"^origin\s(.+?)\s\(.+\)$", out, re.MULTILINE)


def ensure_repo(path: Path, url: str, *fetch_args: str):
    if not path.exists():
        path.mkdir(parents=True)
        git(path, "init")
    if set(git_remote(path)) != {url}:
        git(path, "remote", "rm", "origin", check=False)
        git(path, "remote", "add", "origin", url)
    git(
        path,
        "fetch",
        "origin",
        "--tags",
        "--prune",
        "--prune-tags",
        "--force",
        *fetch_args,
    )


def checkout(path: Path, branch: str, reset: bool = False, clean: bool = False):
    git(path, "checkout", "-q", branch)
    if reset:
        git(path, "reset", "-q", "--hard", f"origin/{branch}")
        # git(path, "merge", "--ff-only")  # expected to fail on unclean/unpushed workdirs
    if clean:
        git(path, "clean", "-d", "--force")


class Release:
    def __init__(self, release_id: str, nixos_version: str):
        self.release_id = release_id
        self.nixos_version = nixos_version

        self.branch_dev = f"fc-{self.nixos_version}-dev"
        self.branch_stag = f"fc-{self.nixos_version}-staging"
        self.branch_prod = f"fc-{self.nixos_version}-production"
        self.branch_doc = "master"

    @property
    def doc_fragment_path(self):
        return FC_DOCS / "changelog.d" / f"{self.nixos_version}.md"

    @property
    def doc_fragment_detailed_path(self):
        return self.doc_fragment_path.with_name(
            f"{self.nixos_version}_detailed.md"
        )

    def prepare(self):
        ensure_repo(FC_DOCS, "git@github.com:flyingcircusio/doc.git")
        ensure_repo(FC_NIXOS, "git@github.com:flyingcircusio/fc-nixos.git")
        ensure_repo(
            FC_NIXPKGS,
            "git@github.com:flyingcircusio/nixpkgs.git",
            "--filter=tree:0",
        )

        checkout(FC_NIXOS, self.branch_dev, reset=True, clean=True)
        checkout(FC_NIXOS, self.branch_stag, reset=True, clean=True)
        checkout(FC_NIXOS, self.branch_prod, reset=True, clean=True)
        checkout(FC_DOCS, self.branch_doc, reset=True, clean=True)

    def skip_no_change(self):
        try:
            git(
                FC_NIXOS,
                "merge-base",
                "--is-ancestor",
                self.branch_stag,
                self.branch_prod,
            )
            print(f"No changes for {self.nixos_version} detected")
            return SKIP_BRANCH
        except CalledProcessError as e:
            if e.returncode != 1:
                raise

    def diff_release(self):
        def cherry(upstream: str, head: str):
            res = git_stdout(FC_NIXOS, "cherry", upstream, head, "-v")
            print(
                f"Commits in {head}, not in {upstream} ({len(res.splitlines())}):"
            )
            print()
            print(f"git cherry '{upstream}' '{head}' -v")
            print(res)
            print()

        dev_rev = rev_parse(FC_NIXOS, self.branch_dev)
        stag_rev = rev_parse(FC_NIXOS, self.branch_stag)
        prod_rev = rev_parse(FC_NIXOS, self.branch_prod)

        print(
            f"Comparing {self.branch_dev} to {self.branch_prod} {prod_rev}..{dev_rev}"
        )
        print(f"{self.branch_stag} is at {stag_rev}")

        print("")
        print("Merged PRs:")
        print()
        print(f"gh pr list --state=merged -B '{self.branch_dev}'")
        try:
            run(
                ["gh", "pr", "list", "--state=merged", "-B", self.branch_dev],
                cwd=FC_NIXOS,
            )
        except FileNotFoundError:
            print("'gh' is not available. Please check merged PRs manually")
        print()

        cherry(self.branch_stag, self.branch_dev)
        cherry(self.branch_prod, self.branch_dev)

        print(f"git diff '{self.branch_prod}' '{self.branch_dev}'")
        print("Press Enter to show full diff")

        input()

        git(FC_NIXOS, "diff", self.branch_prod, self.branch_dev)

        print("Press enter to continue")
        input()

    def collect_changelog(self):
        checkout(FC_NIXOS, self.branch_stag)
        if not CHANGELOG.parent.exists():
            print(
                f"Could not find '{str(CHANGELOG.parent)}'. Skipping changelog generation..."
            )
            # TODO: offer manual edit?
            return
        if self.doc_fragment_path.exists():
            print(
                f"The changelog fragment '{self.doc_fragment_path}' already exists"
            )
            print("Remove it (commit & push) or skip changelog generation")
            raise RuntimeError()

        TEMP_CHANGELOG.open("w").close()  # truncate
        try:
            run(["scriv", "collect", "--add"], cwd=FC_NIXOS, check=True)
        except CalledProcessError:
            print("'scriv' failed. Continuing without changelog...")
            return
        except FileNotFoundError:
            print("'scriv' is not available. Continuing without changelog...")
            return
        finally:
            new_fragment = TEMP_CHANGELOG.read_text()
            TEMP_CHANGELOG.unlink()

        # TODO: remove empty sections

        doc_fragment = new_fragment.replace(
            "\n## Impact", f"\n## Impact\n### {self.nixos_version}"
        )
        doc_fragment = doc_fragment.replace(
            "\n## NixOS XX.XX platform",
            f"\n## NixOS {self.nixos_version} platform",
        )
        self.doc_fragment_path.write_text(doc_fragment)

        new_changelog = f"# Release {self.release_id}\n\n" + new_fragment
        if CHANGELOG.exists():
            new_changelog += "\n" + CHANGELOG.read_text()
        CHANGELOG.write_text(new_changelog)

        git(FC_DOCS, "add", str(self.doc_fragment_path.relative_to(FC_DOCS)))
        git(FC_DOCS, "commit", "-m", f"Add fragment for {self.nixos_version}")
        try:
            git(
                FC_NIXOS,
                "add",
                str(TEMP_CHANGELOG.relative_to(FC_NIXOS)),
                str(CHANGELOG.relative_to(FC_NIXOS)),
            )
            git(FC_NIXOS, "commit", "-m", "Collect changelog fragments")
        except CalledProcessError:
            print(
                "Failed to commit Changelog. Commit it manually and continue after the `collect_changelog` stage"
            )
            raise

    def merge(self):
        checkout(FC_NIXOS, self.branch_prod)
        msg = (
            f"Merge branch '{self.branch_stag}' into "
            f"'{self.branch_prod}' for release {self.release_id}"
        )
        git(FC_NIXOS, "merge", "-m", msg, self.branch_stag)

    def backmerge(self):
        checkout(FC_NIXOS, self.branch_dev)
        msg = f"Backmerge branch '{self.branch_prod}' into '{self.branch_dev}'' for release {self.release_id}"
        git(FC_NIXOS, "merge", "-m", msg, self.branch_prod)

    def add_detailed_changelog(self):
        old_rev = rev_parse(FC_NIXOS, "origin/" + self.branch_prod)
        new_rev = rev_parse(FC_NIXOS, self.branch_prod)
        nixpkgs_changelog = self.generate_nixpkgs_changelog(old_rev, new_rev)
        if nixpkgs_changelog:
            new_fragment = NIXPKGS_CHANGELOG_TEMPLATE.format(
                **nixpkgs_changelog
            )
        else:
            new_fragment = SHORT_CHANGELOG_TEMPLATE.format(
                nixos_version=self.nixos_version,
                old_rev=old_rev,
                new_rev=new_rev,
            )
        self.doc_fragment_detailed_path.write_text(new_fragment)

        print("Press enter to open generated changelog fragment")
        input()
        run([EDITOR, str(self.doc_fragment_detailed_path)])
        git(
            FC_DOCS,
            "add",
            str(self.doc_fragment_detailed_path.relative_to(FC_DOCS)),
        )
        git(
            FC_DOCS,
            "commit",
            "-m",
            f"Add detailed fragment for {self.nixos_version}",
        )

    def generate_nixpkgs_changelog(
        self, old_rev: str, new_rev: str
    ) -> dict | None:
        versions_path = "release/versions.json"
        pversions_path = "release/package-versions.json"
        try:
            old_pversions = load_json(FC_NIXOS, old_rev, pversions_path)
            new_pversions = load_json(FC_NIXOS, new_rev, pversions_path)
            old_versions = load_json(FC_NIXOS, old_rev, versions_path)
            new_versions = load_json(FC_NIXOS, new_rev, versions_path)
            old_nixpkgs_rev = old_versions["nixpkgs"]["rev"]
            new_nixpkgs_rev = new_versions["nixpkgs"]["rev"]
        except CalledProcessError:
            print(
                "Could not find relevant version file. Continuing without nixpkgs changelog..."
            )
            return None
        if old_nixpkgs_rev != new_nixpkgs_rev:
            # The commits might be orphaned
            git(
                FC_NIXPKGS,
                "fetch",
                "origin",
                old_nixpkgs_rev,
                new_nixpkgs_rev,
            )
            nixpkgs_changes = filter_and_merge_commit_msgs(
                get_interesting_commit_msgs(
                    new_pversions, FC_NIXPKGS, old_nixpkgs_rev, new_nixpkgs_rev
                )
                + version_diff_lines(old_pversions, new_pversions)
            )

            return dict(
                nixpkgs_changelog="\n".join(
                    "    - " + m for m in nixpkgs_changes
                ),
                nixos_version=self.nixos_version,
                old_rev=old_rev,
                new_rev=new_rev,
                old_nixpkgs_rev=old_nixpkgs_rev,
                new_nixpkgs_rev=new_nixpkgs_rev,
            )

    def push(self):
        print(f"Committed changes ({self.nixos_version}):")
        print("fc-nixos:")
        git(FC_NIXOS, "log", "--graph", "--decorate", "--format=short", "-n3")
        print("doc:")
        git(
            FC_DOCS,
            "log",
            "--graph",
            "--decorate",
            "--format=short",
            f"origin/{self.branch_doc}..{self.branch_doc}",
        )
        print()
        print(
            "If this looks correct, press Enter to push (or use ^C to abort all releases and ^D to abort this release)."
        )
        try:
            input()
        except EOFError:
            return SKIP_BRANCH
        git(
            FC_NIXOS,
            "push",
            "origin",
            self.branch_dev,
            self.branch_stag,
            self.branch_prod,
        )
        git(FC_DOCS, "push", "origin", self.branch_doc)


def main():
    os.environ["PAGER"] = ""
    parser = argparse.ArgumentParser()
    parser.add_argument("release_id", type=release_id_type)
    parser.add_argument(
        "--nixos_versions",
        default=DEFAULT_NIXOS_VERSIONS,
        nargs="*",
        help="(default: %(default)s)",
    )
    parser.add_argument(
        "--steps",
        choices=["all"] + STEPS,
        default="all",
        nargs="*",
        help="(default: %(default)s)",
    )
    args = parser.parse_args()
    if args.steps == "all" or "all" in args.steps:
        args.steps = STEPS

    for nixos_version in args.nixos_versions:
        release = Release(args.release_id, nixos_version)
        print(f"\nPerforming release for {nixos_version} ({args.release_id})")
        for step_name in args.steps:
            print(f"\nRelease step: {step_name}")
            rc = getattr(release, step_name)()
            if rc == SKIP_BRANCH:
                print(f"Aborted release for {nixos_version}")
                break


if __name__ == "__main__":
    main()
