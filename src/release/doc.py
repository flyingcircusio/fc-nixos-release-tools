import datetime
import logging
import subprocess

from rich import print

from .markdown import MarkdownTree
from .state import STAGE, State
from .utils import FC_DOCS, checkout, ensure_repo, git

FRAGMENTS_DIR = FC_DOCS / "changelog.d"

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
    year_index_file = FC_DOCS / "src/changes/index.md"
    years = [
        e.name + "/index" for e in FC_DOCS.glob("src/changes/*") if e.is_dir()
    ]
    year_index_content = YEAR_INDEX_TEMPLATE.format(
        years="\n".join(sorted(years, reverse=True))
    )
    year_index_file.write_text(year_index_content)

    release_index_file = FC_DOCS / f"src/changes/{year}/index.md"
    releases = [
        e.name.removesuffix(".md")
        for e in FC_DOCS.glob(f"src/changes/{year}/r*.md")
        if e.is_file()
    ]
    release_index_content = RELEASE_INDEX_TEMPLATE.format(
        year=year, releases="\n".join(sorted(releases))
    )
    release_index_file.write_text(release_index_content)

    git(
        FC_DOCS,
        "add",
        str(release_index_file.relative_to(FC_DOCS)),
        str(year_index_file.relative_to(FC_DOCS)),
    )


def next_release_id(date: datetime.date) -> str:
    ensure_repo(FC_DOCS, "git@github.com:flyingcircusio/doc.git")
    checkout(FC_DOCS, "master", reset=True, clean=True)

    years = sorted(
        int(e.name)
        for e in FC_DOCS.glob("src/changes/*")
        if e.is_dir() and e.name.isdigit()
    )
    if not years or years[-1] != date.year:
        return f"{date.year}_001"

    releases = [
        e.name.removesuffix(".md").removeprefix("r")
        for e in FC_DOCS.glob(f"src/changes/{years[-1]}/r*.md")
        if e.is_file()
    ]
    releases = sorted(int(r) for r in releases if r.isdigit())
    if not releases:
        return f"{date.year}_001"
    return f"{years[-1]}_{releases[-1] + 1:03}"


def collect_changelogs(state: State) -> MarkdownTree:
    changelog = MarkdownTree.from_sections(
        "Impact",
        *(f"NixOS {k} platform" for k in sorted(state["branches"])),
        "Documentation",
        "Detailed Changes",
    )
    for k, v in sorted(state["branches"].items()):
        frag = MarkdownTree.from_str(v.get("changelog", ""))
        frag["Impact"].add_header(k)
        frag.rename("NixOS XX.XX platform", f"NixOS {k} platform")
        if frag["Detailed Changes"].entries:
            frag["Detailed Changes"] = f"- NixOS {k}: " + ", ".join(
                e.removeprefix("- ") for e in frag["Detailed Changes"].entries
            )
        changelog |= frag
    changelog["Documentation"] += "<!--\nadd entries if necessary\n-->"
    changelog.move_to_end("Detailed Changes")
    changelog.add_header(
        f"Release {state['release_id']} ({state['release_date']})"
    )
    changelog.entries.insert(
        0, f"---\nPublish Date: '{state['release_date']}'\n---"
    )
    input("Press enter to open the new changelog")
    changelog.open_in_editor()
    changelog.strip()
    return changelog


def main(state: State):
    for k, v in state["branches"].items():
        if "tested" not in v:
            logging.error(f"'{k}' is not tested")
            return
    branches = state["branches"].keys()
    logging.info(
        "This will release the changelog for the following versions: "
        + ", ".join(branches)
    )
    ensure_repo(FC_DOCS, "git@github.com:flyingcircusio/doc.git")
    checkout(FC_DOCS, "master", reset=True, clean=True)

    print("Review open/merged PRs:")
    try:
        subprocess.run(
            ["gh", "pr", "list", "--state=all", "-B", "master"],
            cwd=FC_DOCS,
        )
    except FileNotFoundError:
        logging.error("'gh' is not available. Please check PRs manually")

    year, release_num = state["release_id"].split("_", maxsplit=1)
    new_file = FC_DOCS.joinpath(f"src/changes/{year}/r{release_num}.md")

    changelog = collect_changelogs(state)
    new_file.parent.mkdir(exist_ok=True)
    new_file.write_text(changelog.to_str())

    update_index(year)

    logging.info("Committing changes")
    git(FC_DOCS, "add", str(new_file.relative_to(FC_DOCS)))
    git(FC_DOCS, "commit", "-m", f"add changelog {state['release_id']}")

    input("Press enter to push")
    git(FC_DOCS, "push", "origin", "master")

    state["changelog_url"] = (
        f"https://doc.flyingcircus.io/platform/changes/{year}/r{release_num}.html"
    )
    state["stage"] = STAGE.TAG
