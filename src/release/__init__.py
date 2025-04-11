import argparse
import datetime
import logging
import os
import re
from typing import Optional

from rich import print
from rich.logging import RichHandler
from rich.progress import Progress
from rich.prompt import Confirm, Prompt
from rich.table import Table

from . import state
from .branch import Branch
from .command import Command, step
from .doc import Doc, next_release_id
from .git import FC_NIXOS
from .release import Release
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


def next_monday():
    today = datetime.date.today()
    return today + datetime.timedelta(days=8 - today.isoweekday())


class Start(Command):
    """[bold purple]Start a new release."""

    CHECKLIST_VERSION = """
    ## NixOS {version}

    - [ ] Use `./release branch {version}` to process this branch.
    """

    CHECKLIST_FOOTER = """

    ## Documentation and announcement

    - [ ] `./release doc` to finalize documentation

    - [ ] Announcements

      - [ ] [Create maintenance status page](https://manage.statuspage.io/pages/vk1n4gx65z5k/incidents/new-scheduled-maintenance)
        - [ ] only enable notifications for creating the maintenance, don't remind about progress
        - [ ] don't automate component status

      - [ ] Announce release in Matrix (room General) and link to change log

    ## Shortly before release ({release_date} 20:45 Europe/Berlin)

    - [ ] double-check that production environments are set up correctly as documented in `./release status`

    - [ ] Copy output from `./release status` into a comment of this issue.

    """

    @step(skip_seen=False)
    def choose_release_id_and_date(self):
        """Choose next release ID and date."""

        if not self.release.date:
            release_date = prompt(
                "Next release date",
                default=next_monday(),
                default_display=next_monday().strftime("%A, %Y-%m-%d"),
                conv=release_date_type,
            )
            self.release.date = release_date
        else:
            print(f"Release is scheduled for [bold cyan]{self.release.date}")

        if not self.release.id:
            with Progress(transient=True) as progress:
                t = progress.add_task("Determining next release ID", total=None)
                next_id_suggestion = next_release_id(release_date)
                progress.update(t, total=1, advance=1)
            self.release.id = prompt(
                "Next release ID",
                default=next_id_suggestion,
                conv=release_id_type,
            )
        else:
            print(f"Release ID is [bold cyan]{self.release.id}")

    @step
    def gather_branches(self):
        """Determine branches with changes to release."""
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
                branch = state.Branch(nixos_version=version)
                branch.prepare()
                if branch.has_pending_changes():
                    print(f"Marking [green]{version}[/green] for release.")
                    self.release.branches[version] = branch
                progress.update(task, advance=1)

    @step
    def record_todo_list(self):
        """Record the TODO list in the release ticket."""
        print(
            "Please extend the release ticket's checklist with the following:"
        )
        print()

        print("[purple]" + "=" * 80 + "[/purple]")
        for version in sorted(self.release.branches.keys()):
            print(self.CHECKLIST_VERSION.format(version=version))
        print(
            self.CHECKLIST_FOOTER.format(
                release_date=self.release.date.isoformat()
            )
        )
        print("[purple]" + "=" * 80 + "[/purple]")

        while not Confirm.ask(
            "Have you copied the checklist to the release issue?"
        ):
            pass


class Status(Command):
    def __call__(self):
        table = Table.grid(pad_edge=True)
        table.title = "[b]Release status[/b]"
        table.add_column(justify="right", style="green")
        table.add_column()
        table.add_row("Next release ID: ", self.release.id or N_A)
        table.add_row(
            "Next release date: ",
            self.release.date.isoformat() if self.release.date else N_A,
        )
        table.add_row("Changelog URL: ", self.release.changelog_url or N_A)
        print(table)

        if branches := self.release.branches:
            table = Table()
            table.add_column(
                "NixOS release", justify="right", style="bold cyan"
            )
            table.add_column("Status")
            table.add_column("Production commit ID")
            table.add_column("Hydra Eval")
            for branch in branches.values():
                test_state = (
                    "[green]tested[/green]"
                    if branch.tested
                    else "[red]untested[/red]"
                )
                table.add_row(
                    branch.nixos_version,
                    test_state,
                    branch.new_production_commit,
                    branch.hydra_eval_id,
                )
            print(table)


def main():
    logging.basicConfig(
        level="INFO",
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler()],
    )
    os.environ.pop("PAGER", None)
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    subparser = parser.add_subparsers(dest="command")

    start_parser = subparser.add_parser("start")
    start_parser.set_defaults(command=Start)

    status_parser = subparser.add_parser("status")
    status_parser.set_defaults(command=Status)

    branch_parser = subparser.add_parser("branch")
    branch_parser.add_argument(
        "nixos_version",
        help="Perform the release steps for this specific branch. E.g. `24.11`.",
    )
    branch_parser.set_defaults(command=Branch)

    doc_parser = subparser.add_parser("doc")
    doc_parser.set_defaults(command=Doc)

    args = parser.parse_args()
    if not args.command:
        parser.print_usage()
        return

    factory = args.command
    kwargs = dict(args._get_kwargs())
    del kwargs["command"]

    release = state.load()
    try:
        factory(release, **kwargs)()
        state.save(release)
    except KeyboardInterrupt:
        print("[red]Aborted.[/red]")


if __name__ == "__main__":
    main()
