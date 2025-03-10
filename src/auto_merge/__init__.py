import argparse
import logging
import os
import sys

from auto_merge import check_pr, merge
from auto_merge.config import load_config


def main():
    parser = argparse.ArgumentParser()
    parser.set_defaults(func="print_usage")
    subparsers = parser.add_subparsers(help="subcommand help")

    parser_check_pr = subparsers.add_parser("check-pr", help="check-pr help")
    parser_check_pr.add_argument(
        "pr_id", type=int, help="ID of the pull request, we want to consider"
    )
    parser_check_pr.set_defaults(func=check_pr.check_pr)

    parser_merge = subparsers.add_parser("merge", help="merge help")
    parser_merge.add_argument(
        "--action-run-repo-name",
        type=str,
        help="Repository name including owner, e.g. flyingcircusio/fc-nixos-release-tooling containing this workflow",
        required=True,
        default="",
    )
    parser_merge.add_argument(
        "--fc-nixos-dir",
        help="Directory where the fc-nixos git checkout is in",
        required=True,
    )
    parser_merge.set_defaults(func=merge.run)

    args = parser.parse_args()
    func = args.func
    if func == "print_usage":
        parser.print_usage()
        sys.exit(1)

    kwargs = dict(args._get_kwargs())
    del kwargs["func"]
    kwargs["config"] = load_config()
    try:
        kwargs["github_access_token"] = os.environ["GH_TOKEN"]
    except KeyError:
        raise Exception("Missing `GH_TOKEN` environment variable.")
    if func == merge.run:
        try:
            kwargs["monitoring_review_url"] = os.environ[
                "MONITORING_REVIEW_URL"
            ]
        except KeyError:
            raise Exception(
                "Missing `MONITORING_REVIEW_URL` environment variable."
            )
        try:
            kwargs["matrix_hookshot_url"] = os.environ["MATRIX_HOOKSHOT_URL"]
        except KeyError:
            raise Exception(
                "Missing `MATRIX_HOOKSHOT_URL` environment variable."
            )

    logging.basicConfig(level=logging.INFO)
    func(**kwargs)


if __name__ == "__main__":
    main()
