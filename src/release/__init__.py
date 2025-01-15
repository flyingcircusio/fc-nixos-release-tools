import argparse
import datetime
import logging
import os
import re
from functools import partial
from typing import Optional

from rich import print
from rich.logging import RichHandler

from . import branch, doc
from .state import STAGE, State, load_state, new_state, store_state
from .utils import prompt

AVAILABLE_CMDS = {
    STAGE.INIT: ["init", "status"],
    STAGE.BRANCH: ["add-branch", "test-branch", "doc", "status"],
    STAGE.TAG: ["tag", "status"],
    STAGE.DONE: ["init", "status"],
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


def init(
    state: State,
    release_id: Optional[str],
    release_date: Optional[datetime.date],
):
    state.clear()
    state.update(new_state())
    if not release_date:
        today = datetime.date.today()
        # next monday
        default = today + datetime.timedelta(days=8 - today.isoweekday())
        release_date = prompt(
            "Release date?", default=default, conv=release_date_type
        )
    if not release_id:
        release_id = prompt(
            "Release id?",
            default=doc.next_release_id(release_date),
            conv=release_id_type,
        )
    state["release_id"] = release_id
    state["release_date"] = release_date.isoformat()
    state["stage"] = STAGE.BRANCH


def status(state: State, header: bool = True):
    if header:
        if "release_id" in state and "release_date" in state:
            print(
                f"Current release: {state['release_id']} ({state['release_date']})"
            )
        print(f"You are in stage: {state['stage']}")
        print(
            "The following subcommands are available: "
            + ", ".join(AVAILABLE_CMDS[state["stage"]])
        )
        print()

        print("This release contains the following nixos versions:")
        for k, v in state["branches"].items():
            test_state = "tested" if "tested" in v else "untested"
            print(f"{k}: {test_state}")
        print()
    match state["stage"]:
        case STAGE.INIT:
            print("Call init to create a new release")
        case STAGE.BRANCH:
            print("Call add-branch to add a new nixos version to this release")
            print("Call test-branch to test an existing nixos version")
            print("Call doc to finish")
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
            print("Summary of this release:")
            print(f"Release at: {state['release_date']} 21:00")
            print("Release ID: " + state["release_id"])
            print("Changelog url: " + state["changelog_url"])
            for k, v in state["branches"].items():
                print()
                print(f"## NixOS {k}")
                print("New production commit: " + v["new_production_commit"])
                print("Old staging commit: " + v["orig_staging_commit"])
                print("Hydra eval id: " + v["hydra_eval_id"])

            print()
            print("Call init to create a new release")


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

    init_parser = subparser.add_parser("init")
    init_parser.add_argument(
        "release_id",
        nargs="?",
        type=release_id_type,
        help="Release id in the form YYYY_NNN",
    )
    init_parser.add_argument(
        "release_date",
        nargs="?",
        type=release_date_type,
        help="set planned roll-out date",
    )
    init_parser.set_defaults(func=init)

    status_parser = subparser.add_parser("status")
    status_parser.set_defaults(func=status)

    add_branch_parser = subparser.add_parser("add-branch")
    add_branch_parser.add_argument(
        "nixos_version",
        help="NixOS versions to add.",
    )
    add_branch_parser.add_argument(
        "--steps",
        default=",".join(branch.STEPS),
        nargs="?",
        type=partial(comma_separated_list, choices=branch.STEPS),
        help="Comma-separated list of steps to execute.",
    )
    add_branch_parser.set_defaults(func=branch.add_branch)

    test_branch_parser = subparser.add_parser("test-branch")
    test_branch_parser.add_argument(
        "nixos_version",
        help="NixOS versions to test.",
    )
    test_branch_parser.set_defaults(func=branch.test_branch)

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
