import argparse
import os
import sys
from logging import INFO, basicConfig

FC_NIXOS_REPO = "flyingcircusio/fc-nixos"
NIXPKGS_REPO = "flyingcircusio/nixpkgs"


def main():
    import update_nixpkgs.cleanup
    import update_nixpkgs.update

    basicConfig(level=INFO)
    try:
        github_access_token = os.environ["GH_TOKEN"]
    except KeyError:
        raise Exception("Missing `GH_TOKEN` environment variable.")
    try:
        matrix_hookshot_url = os.environ["MATRIX_HOOKSHOT_URL"]
    except KeyError:
        raise Exception("Missing `MATRIX_HOOKSHOT_URL` environment variable.")

    parser = argparse.ArgumentParser("nixpkgs updater for fc-nixos")
    parser.set_defaults(func="print_usage")
    subparsers = parser.add_subparsers()

    parser_update = subparsers.add_parser(
        "update", help="run nixpkgs update workflow"
    )
    parser_update.add_argument(
        "--fc-nixos-dir",
        help="Directory where the fc-nixos git checkout is in",
        required=True,
    )
    parser_update.add_argument(
        "--nixpkgs-dir",
        help="Directory where the nixpkgs git checkout is in",
        required=True,
    )
    parser_update.add_argument(
        "--nixpkgs-upstream-url",
        help="URL to the upstream nixpkgs repository",
        required=True,
    )
    parser_update.add_argument(
        "--nixpkgs-origin-url",
        help="URL to push the nixpkgs updates to",
        required=True,
    )
    parser_update.add_argument(
        "--platform-versions",
        help="Platform versions",
        required=True,
        nargs="+",
    )
    parser_update.add_argument(
        "--force",
        help="Create new PR, even if no changes are detected",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    parser_update.set_defaults(func=update_nixpkgs.update.run)

    parser_cleanup = subparsers.add_parser(
        "cleanup", help="run nixpkgs update cleanup"
    )
    parser_cleanup.add_argument(
        "--merged-pr-id", help="merged fc-nixos PR ID", required=True
    )
    parser_cleanup.add_argument(
        "--fc-nixos-dir",
        help="Directory where the fc-nixos git checkout is in",
        required=True,
    )
    parser_cleanup.add_argument(
        "--nixpkgs-dir",
        help="Directory where the nixpkgs git checkout is in",
        required=True,
    )
    parser_cleanup.add_argument(
        "--nixpkgs-origin-url",
        help="URL to push the nixpkgs updates to",
        required=True,
    )
    parser_cleanup.set_defaults(func=update_nixpkgs.cleanup.run)

    args = parser.parse_args()
    func = args.func
    if func == "print_usage":
        parser.print_usage()
        sys.exit(1)

    kwargs = dict(args._get_kwargs())
    del kwargs["func"]
    kwargs["github_access_token"] = github_access_token
    kwargs["matrix_hookshot_url"] = matrix_hookshot_url
    func(**kwargs)


if __name__ == "__main__":
    main()
