import argparse
import sys

from auto_merge.check_pr import check_pr
from auto_merge.config import load_config

def main():
    parser = argparse.ArgumentParser()
    parser.set_defaults(func="print_usage")
    subparsers = parser.add_subparsers(help='subcommand help')

    parser_check_pr = subparsers.add_parser('check-pr', help='check-pr help')
    parser_check_pr.add_argument('repo_name', type=str, help='Repository name including owner, e.g. flyingcircusio/fc-nixos')
    parser_check_pr.add_argument('pr_id', type=int, help='ID of the pull request, we want to consider')
    parser_check_pr.set_defaults(func=check_pr)

    args = parser.parse_args()
    func = args.func
    if func == "print_usage":
        parser.print_usage()
        sys.exit(1)

    kwargs = dict(args._get_kwargs())
    del kwargs["func"]
    kwargs["config"] = load_config()
    func(**kwargs)


if __name__ == "__main__":
    main()