import datetime

from rich import print
from rich.markdown import Markdown
from rich.prompt import Confirm, Prompt

from .command import Command, step
from .git import FC_DOCS
from .markdown import MarkdownTree
from .utils import trigger_doc_update

FRAGMENTS_DIR = FC_DOCS.path / "changelog.d"

RELEASE_INDEX_TEMPLATE = """\
# {year}

Releases performed in {year}.

```{{toctree}}
:maxdepth: 1

{releases}
```
"""

YEAR_INDEX_TEMPLATE = """\
(changelog)=

# Changelog

Here follows a short description of all user-visible changes made to our
infrastructure in reverse chronological order.

```{{toctree}}
:maxdepth: 1

{years}

```
"""


def update_index(year: str) -> None:
    year_index_file = FC_DOCS.path / "src/changes/index.md"
    years = [
        e.name + "/index"
        for e in FC_DOCS.path.glob("src/changes/*")
        if e.is_dir()
    ]
    year_index_content = YEAR_INDEX_TEMPLATE.format(
        years="\n".join(sorted(years, reverse=True))
    )
    year_index_file.write_text(year_index_content)

    release_index_file = FC_DOCS.path / f"src/changes/{year}/index.md"
    releases = [
        e.name.removesuffix(".md")
        for e in FC_DOCS.path.glob(f"src/changes/{year}/r*.md")
        if e.is_file()
    ]
    release_index_content = RELEASE_INDEX_TEMPLATE.format(
        year=year, releases="\n".join(sorted(releases))
    )
    release_index_file.write_text(release_index_content)

    FC_DOCS._git(
        "add",
        str(release_index_file.relative_to(FC_DOCS.path)),
        str(year_index_file.relative_to(FC_DOCS.path)),
    )


def next_release_id(date: datetime.date) -> str:
    FC_DOCS.ensure_repo()
    FC_DOCS.checkout("master", reset=True, clean=True)

    years = sorted(
        int(e.name)
        for e in FC_DOCS.path.glob("src/changes/*")
        if e.is_dir() and e.name.isdigit()
    )
    if not years or years[-1] != date.year:
        return f"{date.year}_001"

    releases = [
        e.name.removesuffix(".md").removeprefix("r")
        for e in FC_DOCS.path.glob(f"src/changes/{years[-1]}/r*.md")
        if e.is_file()
    ]
    releases = sorted(int(r) for r in releases if r.isdigit())
    if not releases:
        return f"{date.year}_001"
    return f"{years[-1]}_{releases[-1] + 1:03}"


def collect_changelogs(release) -> MarkdownTree:
    changelog = MarkdownTree.from_sections(
        "Impact",
        *(f"NixOS {k} platform" for k in sorted(release.branches)),
        "Documentation",
        "Detailed Changes",
    )
    for _, branch in sorted(release.branches.items()):
        frag = MarkdownTree.from_str(branch.changelog)
        frag["Impact"].add_header(branch.nixos_version)
        frag.rename(
            "NixOS XX.XX platform", f"NixOS {branch.nixos_version} platform"
        )
        if frag["Detailed Changes"].entries:
            frag["Detailed Changes"] = (
                f"- NixOS {branch.nixos_version}: "
                + ", ".join(
                    e.removeprefix("- ")
                    for e in frag["Detailed Changes"].entries
                )
            )
        changelog |= frag
    changelog["Documentation"] += "<!--\nadd entries if necessary\n-->"
    changelog.move_to_end("Detailed Changes")
    changelog.add_header(f"Release {release.id} ({release.date.isoformat()})")
    changelog.entries.insert(
        0, f"---\nPublish Date: '{release.date.isoformat()}'\n---"
    )

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

    return changelog


class Doc(Command):
    """Finalize the documentation for this release"""

    @step(skip_seen=False)
    def docs(self):
        """Verify all branches are marked as tested"""
        branches = list(self.release.branches)
        print(
            "This will release the changelog for the following versions: "
            + ", ".join(branches)
        )

        for branch in self.release.branches.values():
            if not branch.tested:
                print(f"[red]'{branch.nixos_version}' is not tested")
                raise RuntimeError()

    @step(skip_seen=False)
    def fetch_repo(self):
        """Update documentation repository"""
        FC_DOCS.ensure_repo()
        FC_DOCS.checkout("master", reset=True, clean=True)

    @step
    def collect_changelogs(self):
        """Collect changelogs from all branches and update index"""
        changelog = collect_changelogs(self.release)

        new_file = (
            FC_DOCS.path
            / f"src/changes/{self.release.year}/r{self.release.release_num}.md"
        )
        new_file.parent.mkdir(exist_ok=True)
        new_file.write_text(changelog.to_str())

        update_index(self.release.year)

        FC_DOCS._git("add", str(new_file.relative_to(FC_DOCS.path)))
        FC_DOCS._git("commit", "-m", f"add changelog {self.release.id}")

    @step
    def push(self):
        """Push changes"""
        # FC_DOCS._git("push", "origin", "master")
        print("Changes have been pushed.")

    @step
    def trigger_changelog_update(self):
        """Update documentation on website"""
        trigger_doc_update()

        print(
            f" > The changelogs are available at {self.release.changelog_url}"
        )
        print()
        while not Confirm.ask(
            "[purple]Have you spot-checked the changelog for proper rendering?"
        ):
            pass
