import json
import re
from pathlib import Path

from rich import print

from release.utils import execute

from .utils import WORK_DIR


class GitError(RuntimeError):
    cmd_out: str


class GitRepo:
    """A helper class to wrap git interactions.

    All git actions are logged to ensure that in case something goes wrong we
    can inspect what's happened.

    """

    path: Path
    origin: str

    def __init__(self, path, origin):
        self.path = path
        self.origin = origin

    def _git_raw(self, *cmd: str):
        return execute(("git",) + cmd, cwd=self.path)

    def _git(self, *cmd: str, check=True) -> str:
        rc, output = execute(("git",) + cmd, cwd=self.path)

        if check and rc:
            err = GitError(f"{cmd} returned with exit code {rc}.\n")
            # provide command output for further analysis up the callchain
            err.cmd_out = output.joined.getvalue()
            print(err.cmd_out)
            raise err

        return output.stdout.getvalue()

    def branches(self):
        branches = self._git("branch", "--all").splitlines()
        return [b.strip().strip("*").strip() for b in branches]

    def match_branches(self, pattern):
        for branch in self.branches():
            if not (m := re.match(pattern, branch)):
                continue
            yield m

    def current_origin(self):
        out = self._git("remote", "-v")
        return set(re.findall(r"^origin\s(.+?)\s\(.+\)$", out, re.MULTILINE))

    def rev_parse(self, rev: str):
        return self._git("rev-parse", "--verify", rev).strip()

    def show(self, rev: str, obj_path: str):
        return json.loads(self._git("show", rev + ":" + obj_path))

    def ensure_repo(self):
        if not self.path.exists():
            self.path.mkdir(parents=True)
            self._git("init")

        if (current := self.current_origin()) != {self.origin}:
            if current:
                self._git("remote", "rm", "origin", check=False)
            self._git("remote", "add", "origin", self.origin)
        self._git(
            "fetch",
            "origin",
            "--tags",
            "--prune",
            "--prune-tags",
            "--force",
        )

    def pull(self):
        self._git("pull")

    def checkout(self, branch: str, reset: bool = False, clean: bool = False):
        if reset:
            self._git("checkout", "-q", "-f", branch)
            self._git("reset", "-q", "--hard", f"origin/{branch}")
        else:
            self._git("checkout", "-q", branch)
        if clean:
            self._git("clean", "-d", "--force")
        self._git("branch", f"--set-upstream-to=origin/{branch}")

    def is_ancestor(self, a, b):
        code, _ = self._git_raw("merge-base", "--is-ancestor", a, b)
        match code:
            case 0:
                return False
            case 1:
                return True
            case _:
                raise RuntimeError()


FC_DOCS = GitRepo(WORK_DIR / "doc", "git@github.com:flyingcircusio/doc.git")
FC_NIXOS = GitRepo(
    WORK_DIR / "fc-nixos", "git@github.com:flyingcircusio/fc-nixos.git"
)
