import argparse
import datetime
import logging
import os
import re
from functools import partial
from typing import Optional

from rich import print
from rich.logging import RichHandler
from rich.progress import Progress
from rich.table import Table

from . import branch, doc
from .branch import Branch
from .release import STAGE, Release
from .utils import prompt

N_A = "[i]n/a[/i]"


def release_id_type(arg_value):
    if not re.compile("^[0-9]{4}_[0-9]{3}$").match(arg_value):
        raise argparse.ArgumentTypeError(
            "Release ID must be formatted as YYYY_NNN"
        )
    return arg_value


def release_date_type(arg_value):
    if not re.match(r"\d{4}-\d{2}-\d{2}$", arg_value):
        raise argparse.ArgumentTypeError(
            "Release date must be formatted as YYYY-MM-DD"
        )
    return datetime.date.fromisoformat(arg_value)


def comma_separated_list(arg_value: str, choices=None):
    separated = arg_value.split(",")
    for e in separated:
        if choices and e not in choices:
            raise argparse.ArgumentTypeError(
                f"invalid element '{e}'. Must be one of '{','.join(choices)}'"
            )
    return separated


CHECKLIST_VERSION = """
## NixOS {version}

- [ ] `./release merge-production {version}`
- [ ] `./release release-production {version}`
"""

CHECKLIST_FOOTER = """

## Documentation and announcement

- [ ] `./release doc`
  - [ ] check rendered changelog
    - can be sped up by `ssh doc.flyingcircus.io sudo systemctl start update-platformdoc.service`
  - [ ] activate "keep" for the Hydra job flyingcircus:fc-*-production:release

- [ ] Announcements

  - [ ] [Create maintenance status page](https://manage.statuspage.io/pages/vk1n4gx65z5k/incidents/new-scheduled-maintenance)
    - [ ] only enable notifications for creating the maintenance, don't remind about progress
    - [ ] don't automate component status

  - [ ] Announce release in Matrix (room General) and link to change log

## Shortly before release ({release_date} 20:45 Europe/Berlin)

- [ ] double-check that production environments are set up correctly as documented in `./release status`

- [ ] Copy output from `./release status` into a comment of this issue.

"""


def start():
    release = Release.unreleased()
    if release:
        print("[red]Release {release['id']} already in progress.[/red]")

    print("Starting a new release cycle.")
    print()

    today = datetime.date.today()
    # next monday
    default = today + datetime.timedelta(days=8 - today.isoweekday())

    release_date = prompt(
        "Next release date",
        default=default,
        default_display=default.strftime("%A, %Y-%m-%d"),
        conv=release_date_type,
    )
    release["date"] = release_date.isoformat()

    with Progress(transient=True) as progress:
        t = progress.add_task("Determining next release ID", total=None)
        next_id_suggestion = doc.next_release_id(release_date)
        progress.update(t, total=1, advance=1)
    release["id"] = prompt(
        "Next release ID",
        default=next_id_suggestion,
        conv=release_id_type,
    )
    release.save
    release.gather_branches()


def status(release: Optional[Release]):
    release = Release.head()

    if not release:
        print("[red]No known release.[/red]")

    table = Table.grid(padding=1, pad_edge=True, expand=True)
    table.title = "[b]Release status[/b]"
    table.add_column(justify="right", style="bold green")
    table.add_column()
    table.add_row("Stage", release.get("stage"))
    table.add_row("Next release ID", release.get("id", N_A))
    table.add_row("Next release date", release.get("date", N_A))
    table.add_row("Changelog URL", release.get("changelog_url", N_A))
    print(table)

    if branches := release.get("branches"):
        table = Table()
        table.add_column("NixOS release", justify="right", style="bold cyan")
        table.add_column("Status")
        table.add_column("Production commit ID")
        table.add_column("Hydra Eval")
        for k, branch in branches.items():
            test_state = (
                "[green]tested[/green]"
                if "tested" in branch
                else "[red]untested[/red]"
            )
            table.add_row(
                k,
                test_state,
                branch.get("new_production_commit"),
                branch.get("hydra_eval_id"),
            )
        print(table)


def main():
    logging.basicConfig(
        level="INFO",
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler()],
    )
    os.environ["PAGER"] = ""
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    subparser = parser.add_subparsers(dest="command")

    start_parser = subparser.add_parser("start")
    start_parser.set_defaults(func=start)

    status_parser = subparser.add_parser("status")
    status_parser.set_defaults(func=status)

    merge_production_parser = subparser.add_parser("merge-production")
    merge_production_parser.add_argument(
        "nixos_version",
        help="NixOS versions to add.",
    )
    merge_production_parser.add_argument(
        "--steps",
        default=",".join(branch.STEPS),
        nargs="?",
        type=partial(comma_separated_list, choices=branch.STEPS),
        help="Comma-separated list of steps to execute.",
    )
    merge_production_parser.set_defaults(func=branch.merge_production)

    release_production_parser = subparser.add_parser("release-production")
    release_production_parser.add_argument(
        "nixos_version",
        help="NixOS versions to test.",
    )
    release_production_parser.set_defaults(func=branch.release_production)

    doc_parser = subparser.add_parser("doc")
    doc_parser.set_defaults(func=doc.main)

    tag_parser = subparser.add_parser("tag")
    tag_parser.set_defaults(func=branch.tag_branch)

    args = parser.parse_args()
    if not hasattr(args, "func"):
        parser.print_usage()
        return

    func = args.func
    kwargs = dict(args._get_kwargs())
    del kwargs["func"]
    del kwargs["command"]
    func(state, **kwargs)


if __name__ == "__main__":
    main()
