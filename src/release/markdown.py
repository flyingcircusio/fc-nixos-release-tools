import re
import subprocess
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional, Self

from .utils import EDITOR, TEMP_CHANGELOG, git

comment_re = re.compile(r"^\s*<!--.*?-->$", flags=re.DOTALL | re.MULTILINE)
section_re = re.compile(
    r"^(?P<indent>#+) (?P<title>[^\n]+)\n(?P<body>.*?)(?=^#+ |\Z)(?!(?P=indent)#)",
    flags=re.DOTALL | re.MULTILINE,
)
entry_re = re.compile(
    r"^(.+?)(?=\n\n+\S|\n- |\Z)", flags=re.DOTALL | re.MULTILINE
)


@dataclass()
class MarkdownTree:
    entries: list[str] = field(default_factory=list)
    subtrees: dict[str, Self] = field(default_factory=dict)

    def get(self, key: str) -> Self:
        return self.subtrees.get(key, MarkdownTree())

    def __getitem__(self, item: str) -> Self:
        return self.subtrees.setdefault(item, MarkdownTree())

    def __setitem__(self, key: str, value: Self | list | str) -> None:
        if isinstance(value, list):
            self.subtrees[key].entries = value
        elif isinstance(value, str):
            self.subtrees[key].entries = [value]
        else:
            self.subtrees[key] = value

    def __iadd__(self, other: str) -> Self:
        self.entries.append(other)
        return self

    def clone(self) -> Self:
        return self | MarkdownTree()

    def copy_from(self, other: Self) -> None:
        self.entries = other.entries
        self.subtrees = other.subtrees

    def __or__(self, other: Self) -> Self:
        entries = self.entries + other.entries
        keys = list(self.subtrees)
        keys += [k for k in other.subtrees if k not in keys]
        subtrees = {k: self.get(k) | other.get(k) for k in keys}
        return MarkdownTree(entries, subtrees)

    @classmethod
    def from_sections(cls, *sections: str):
        r = cls()
        for s in sections:
            r[s] = cls()
        return r

    @classmethod
    def from_str(cls, text: str) -> Self:
        text = comment_re.sub("", text)
        subtrees = defaultdict(cls)
        section = section_re.search(text)
        entries = entry_re.split(text[: section.start()] if section else text)
        entries = [e.strip() for e in entries if e.strip()]
        end = len(text)
        while section:
            subtrees[section["title"].strip()] |= cls.from_str(section["body"])
            end = section.end()
            section = section_re.match(text, pos=end)
        assert end == len(text)

        return cls(entries, subtrees)

    def to_str(self, indent=1) -> str:
        res = "".join(f"{e}\n\n" for e in self.entries)
        if res:
            res += "\n"
        for title, body in self.subtrees.items():
            res += "#" * indent + " " + title + "\n\n"
            res += body.to_str(indent + 1)

        return res

    @classmethod
    def collect(
        cls, files: Iterable[Path], git_repo: Optional[Path] = None
    ) -> Self:
        res = cls()
        for f in files:
            if not f.is_file():
                continue
            res |= cls.from_str(f.read_text())
            f.unlink()
            if git_repo:
                git(git_repo, "add", str(f.relative_to(git_repo)))
        return res

    def strip(self) -> None:
        for k, v in dict(self.subtrees).items():
            v.strip()
            if not v.entries and not v.subtrees:
                del self.subtrees[k]

    def rename(self, old: str, new: str) -> None:
        # preserve order
        self.subtrees = {
            (k if k != old else new): v for k, v in self.subtrees.items()
        }

    def add_header(self, header: str) -> None:
        self.copy_from(MarkdownTree([], {header: self.clone()}))

    def move_to_end(self, s: str):
        if s in self.subtrees:
            self[s] = self.subtrees.pop(s)

    def open_in_editor(self) -> None:
        TEMP_CHANGELOG.write_text(
            "<!-- Generated Changelog. Adjust as necessary. -->\n\n"
            + self.to_str()
            + "\n\n<!-- vim: set spell spelllang=en: -->"
        )
        subprocess.run([EDITOR, str(TEMP_CHANGELOG)])
        self.copy_from(MarkdownTree.from_str(TEMP_CHANGELOG.read_text()))
        TEMP_CHANGELOG.unlink()
