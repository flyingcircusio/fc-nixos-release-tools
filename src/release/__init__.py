import argparse
import datetime
import logging
import os
import re
from functools import partial
from typing import Optional

from rich import print
from rich.logging import RichHandler
from rich.markdown import Markdown
from rich.progress import Progress
from rich.prompt import Confirm
from rich.table import Table

from . import branch, doc
from .branch import Release
from .git import FC_NIXOS
from .state import STAGE, State, load_state, new_state, store_state
from .utils import prompt

N_A = "[i]n/a[/i]"

AVAILABLE_CMDS = {
    STAGE.START: ["start", "status"],
    STAGE.MERGE: ["merge-production", "release-production", "doc", "status"],
    STAGE.TAG: ["tag", "status"],
    STAGE.DONE: ["start", "status"],
}


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

- [ ] Announce
  - [ ] [Create maintenance status page](https://manage.statuspage.io/pages/vk1n4gx65z5k/incidents/new-scheduled-maintenance)
    - [ ] only notifications now
- [ ] Announce release in Matrix (room General) and link to change log

## Shortly before release ({release_date} 20:45 Europe/Berlin)

- [ ] double-check that production environments are set up correctly as documented in `./release status`

## Around the announced release time (shortly before or after):

- [ ] Tag released commits in production branches: `./release tag`

## After release

- [ ] Copy output from `./release status` into a comment of this issue.

"""


def start(
    state: State,
    release_id: Optional[str],
    release_date: Optional[datetime.date],
):
    state.clear()
    state.update(new_state())
    print("Starting a new release cycle.")
    print()
    if not release_date:
        today = datetime.date.today()
        # next monday
        default = today + datetime.timedelta(days=8 - today.isoweekday())

        release_date = prompt(
            "Next release date",
            default=default,
            default_display=default.strftime("%A, %Y-%m-%d"),
            conv=release_date_type,
        )
    if not release_id:
        with Progress(transient=True) as progress:
            t = progress.add_task("Determining next release ID", total=None)
            next_id_suggestion = doc.next_release_id(release_date)
            progress.update(t, total=1, advance=1)
        release_id = prompt(
            "Next release ID",
            default=next_id_suggestion,
            conv=release_id_type,
        )
    state["release_id"] = release_id
    state["release_date"] = release_date.isoformat()
    state["stage"] = STAGE.MERGE

    # Gather releases with changes
    print()
    with Progress(transient=True) as progress:
        # First, determine all relevant branches that might want to be released
        task = progress.add_task("Determining platform versions", total=None)
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


def status(state: State, header: bool = True):
    if header:
        table = Table.grid(padding=1, pad_edge=True, expand=True)
        table.title = "[b]Release status[/b]"
        table.add_column(justify="right", style="bold green")
        table.add_column()
        table.add_row("Stage", state.get("stage"))
        table.add_row("Next release ID", state.get("release_id", N_A))
        table.add_row("Next release date", state.get("release_date", N_A))
        table.add_row("Changelog URL", state.get("changelog_url", N_A))
        table.add_row(
            "Available commands",
            Markdown(
                "\n".join(
                    f"* `{cmd}`" for cmd in AVAILABLE_CMDS[state.get("stage")]
                )
            ),
        )
        print(table)

        if branches := state.get("branches"):
            table = Table()
            table.add_column(
                "NixOS release", justify="right", style="bold cyan"
            )
            table.add_column("Status")
            for k, v in branches.items():
                test_state = (
                    "[green]tested[/green]"
                    if "tested" in v
                    else "[red]untested[/red]"
                )
                table.add_row(k, test_state)
            print(table)
    match state["stage"]:
        case STAGE.TAG:
            print("Remember to do the following tasks:")
            print("Now:")
            print("check rendered changelog")
            print("statuspage: create maintenance (only notifications now)")
            print(
                f"Announce release in Matrix (room General) and link to change log ({state['changelog_url']})"
            )
            print()
            print(
                f"Shortly Before Release ({state['release_date']} 20:45 Europe/Berlin)"
            )
            print(
                "double-check that production environments are set up correctly:"
            )
            for k, v in state["branches"].items():
                print(
                    f"release '{state['release_id']}' for {k}-production using hydra eval ID {v['hydra_eval_id']} (commit {v['new_production_commit']}), valid from {state['release_date']} 21:00"
                )
            print()
            print(
                f"Around the announced release time ({state['release_date']} 21:00 Europe/Berlin) (shortly before or after):"
            )
            print("Call tag")
        case STAGE.DONE:
            for k, v in state["branches"].items():
                print()
                print(f"## NixOS {k}")
                print("New production commit: " + v["new_production_commit"])
                print("Old staging commit: " + v["orig_staging_commit"])
                print("Hydra eval id: " + v["hydra_eval_id"])

            print()
            print("Call `./release start` to start a new release cycle.")


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
    start_parser.add_argument(
        "release_id",
        nargs="?",
        type=release_id_type,
        help="Release ID in the form YYYY_NNN",
    )
    start_parser.add_argument(
        "release_date",
        nargs="?",
        type=release_date_type,
        help="Targeted roll-out date",
    )
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

    state = load_state()

    if args.command not in AVAILABLE_CMDS[state["stage"]]:
        print(f"{args.command} is not available in stage '{state['stage']}'")
        print()
        status(state)
        return

    func = args.func
    kwargs = dict(args._get_kwargs())
    del kwargs["func"]
    del kwargs["command"]
    func(state, **kwargs)
    if func != status:
        print()
        status(state, header=False)

    store_state(state)


if __name__ == "__main__":
    main()
