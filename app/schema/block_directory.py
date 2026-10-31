"""Parse the vendored Block Directory into the vocabulary the Answer Contract allows.

The directory is the only thing keeping the assistant honest about R Block: the
model has no pretrained knowledge of it, so every block name it may emit comes
from here. See docs/adr/0003-vendored-copy-of-the-block-directory.md.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

DIRECTORY_PATH = Path(__file__).resolve().parents[2] / "vendor" / "r-block-directory.md"

# Byte-identical copy of the original directory, kept so an admin can always
# get back to the factory version ("Reset factory" in the levels admin page).
# Never written by the app. Must hash to PINNED_SHA256.
FACTORY_PATH = DIRECTORY_PATH.with_name("r-block-directory.factory.md")
# When an admin uses "Set as default", DIRECTORY_PATH is rewritten and its new
# hash is recorded here, so the startup check below still refuses an
# UNRECORDED (hand) edit while accepting the admin-approved one.
SHA_SIDECAR_PATH = DIRECTORY_PATH.with_name("r-block-directory.sha256")

# The version the enum was generated from. A silent upstream edit would change
# what the model is permitted to emit, so we refuse to start on a mismatch.
PINNED_SHA256 = "4347e6e5d4c9789c2d9678f9b154b33809ddca73cb532f7ff80f23d17087d0f0"

_TAB = re.compile(r"^## (?!Slot type legend)(.+?)\s*$", re.MULTILINE)
_BLOCK = re.compile(r"^### (.+?)\s*$", re.MULTILINE)
_EXACT_TEXT = re.compile(r"^\* \*\*Exact Text:\*\*\s*`(.+?)`\s*$", re.MULTILINE)
_REMARKS = re.compile(r"^\* \*\*Remarks:\*\*\s*(.+?)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class Block:
    """One R Block, as the student will see it named in the robot's own app."""

    name: str
    tab: str
    exact_text: str
    remarks: str = ""

    @property
    def slots(self) -> list[str]:
        """The placeholders a student must fill in, e.g. ``[Dropdown]``."""
        return re.findall(r"\[[^\]]+\]", self.exact_text)

    @property
    def slot_kinds(self) -> list[str]:
        """``slots``, classified as ``dropdown`` or ``value`` (``[Icon]`` and
        ``[Condition]``/``[Condition Slot]`` are excluded — the former isn't
        filled in, the latter is a nested block, not a slot).

        Matched by prefix, not exact string: the directory has exactly one
        ``[Value Slot]`` placeholder (Define variable as) alongside 32 plain
        ``[Value]`` ones. An exact ``== "Value"`` check silently dropped that
        slot from the expected shape entirely — validate.py then rejected a
        CORRECT model answer (both a variable name and its value) as
        malformed, because the catalog itself claimed the block only takes
        one slot. Caught live: a real answer flagged as an error that
        wasn't one. Prefix matching is robust to this whole class of
        two-word placeholder variants, not just this one instance.
        """
        kinds = []
        for token in self.slots:
            inner = token[1:-1]
            if inner.startswith("Dropdown"):
                kinds.append("dropdown")
            elif inner.startswith("Value"):
                kinds.append("value")
        return kinds

    @property
    def has_condition(self) -> bool:
        """Whether this block seats a hexagonal condition block (a C-block's
        test, or a logical operator's operand). Detected from ``remarks``
        because the directory encodes it there for most blocks (e.g. ``Repeat
        until``) rather than as an ``[Condition]`` token in Exact Text."""
        return "hexagonal condition slot" in self.remarks.lower() or any(
            token in self.exact_text for token in ("[Condition]", "[Condition Slot]")
        )

    @property
    def condition_count(self) -> int:
        """How many hexagonal condition slots this block seats: 0 for most
        blocks, 1 for the common case (``if then``, ``Repeat until``, ...),
        and 2 for the one exception, ``Logical And / Or`` (a left and a
        right operand). Counted from literal ``[Condition]``/``[Condition
        Slot]`` tokens in Exact Text where present; falls back to 1 when
        ``has_condition`` is true but Exact Text has no token for it at all
        (e.g. ``Repeat until``, which only describes its slot in Remarks)."""
        n = self.exact_text.count("[Condition Slot]") + self.exact_text.count("[Condition]")
        if n:
            return n
        return 1 if self.has_condition else 0

    @property
    def has_children(self) -> bool:
        """Whether this block is a C-shaped wrapper enclosing a block sequence."""
        return "nested block sequence area" in self.remarks.lower()

    @property
    def has_else(self) -> bool:
        """Whether this block can expand an optional else branch (only ``if
        then``, per its remarks: 'The gear icon expands the block to add an
        `else` branch.')."""
        return "else" in self.remarks.lower()


def _sections(text: str, pattern: re.Pattern[str]) -> list[tuple[str, str]]:
    """Split ``text`` on ``pattern``, pairing each heading with the body under it."""
    matches = list(pattern.finditer(text))
    bounds = [m.start() for m in matches] + [len(text)]
    return [
        (m.group(1), text[m.end() : bounds[i + 1]])
        for i, m in enumerate(matches)
    ]


def verify_pinned(path: Path = DIRECTORY_PATH) -> None:
    """Refuse a directory that is not the version the enum was generated from."""
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    approved = {PINNED_SHA256}
    if path == DIRECTORY_PATH and SHA_SIDECAR_PATH.exists():
        approved.add(SHA_SIDECAR_PATH.read_text(encoding="utf-8").strip())
    if actual not in approved:
        raise RuntimeError(
            f"{path.name} has changed since the enum was pinned "
            f"(expected {PINNED_SHA256[:12]}…, found {actual[:12]}…). "
            "Review the upstream diff, then update PINNED_SHA256."
        )


def parse(path: Path = DIRECTORY_PATH) -> list[Block]:
    """Read every block in the directory, in document order."""
    verify_pinned(path)
    return parse_text(path.read_text(encoding="utf-8"))


def parse_text(text: str) -> list[Block]:
    blocks: list[Block] = []
    for tab, tab_body in _sections(text, _TAB):
        for name, body in _sections(tab_body, _BLOCK):
            exact = _EXACT_TEXT.search(body)
            if exact is None:
                raise ValueError(f"block {name!r} in tab {tab!r} has no Exact Text")
            remarks = _REMARKS.search(body)
            blocks.append(
                Block(
                    name=name,
                    tab=tab,
                    exact_text=exact.group(1),
                    remarks=remarks.group(1) if remarks else "",
                )
            )
    return blocks


@lru_cache(maxsize=1)
def load() -> list[Block]:
    return parse()


def names() -> list[str]:
    """The enum the Answer Contract constrains block names to."""
    return [b.name for b in load()]


@lru_cache(maxsize=1)
def by_name() -> dict[str, Block]:
    return {b.name: b for b in load()}


@lru_cache(maxsize=1)
def raw_tab_sections(path: Path = DIRECTORY_PATH) -> dict[str, str]:
    """Each tab's markdown body verbatim, for injecting into a prompt as
    context — real directory text, not a reconstruction of it."""
    verify_pinned(path)
    text = path.read_text(encoding="utf-8")
    return {tab: f"## {tab}\n{body}".rstrip() for tab, body in _sections(text, _TAB)}


@lru_cache(maxsize=1)
def full_text(path: Path = DIRECTORY_PATH) -> str:
    verify_pinned(path)
    return path.read_text(encoding="utf-8")


def tabs() -> list[str]:
    seen: dict[str, None] = {}
    for b in load():
        seen.setdefault(b.tab, None)
    return list(seen)


# -- Admin-managed default copy (levels admin page) -------------------------
# The default copy is DIRECTORY_PATH itself. Entries are the same key (tab
# name) -> value (that tab's markdown) pairs raw_tab_sections() returns, but
# read fresh from disk, not from the process-lifetime caches above.


def entries_from_text(text: str) -> list[dict]:
    return [{"key": tab, "value": f"## {tab}\n{body}".rstrip()} for tab, body in _sections(text, _TAB)]


def read_default_entries() -> list[dict]:
    return entries_from_text(DIRECTORY_PATH.read_text(encoding="utf-8"))


def read_factory_entries() -> list[dict]:
    verify_factory()
    return entries_from_text(FACTORY_PATH.read_text(encoding="utf-8"))


def verify_factory() -> None:
    if hashlib.sha256(FACTORY_PATH.read_bytes()).hexdigest() != PINNED_SHA256:
        raise RuntimeError(f"{FACTORY_PATH.name} does not match the pinned factory hash")


def build_directory_text(entries: list[dict]) -> str:
    """The directory file rebuilt from key/value entries: the current file's
    own header (intro + slot legend) followed by every entry. Raises
    ValueError rather than return text the app could not start with."""
    base = DIRECTORY_PATH.read_text(encoding="utf-8")
    first = _TAB.search(base)
    header = base[: first.start()] if first else base
    parts = []
    for e in entries:
        key, value = e["key"].strip(), e["value"].strip()
        if "\n" in key:
            raise ValueError(f"key {key!r} must be a single line")
        parts.append(value if value.startswith(f"## {key}") else f"## {key}\n{value}")
    text = header + "\n\n".join(parts) + "\n"

    if [t for t, _ in _sections(text, _TAB)] != [e["key"].strip() for e in entries]:
        raise ValueError("an entry's text contains its own '## ' heading, which would split it into extra tabs")
    if not parse_text(text):  # also raises if a block has no Exact Text
        raise ValueError("no blocks left -- the AI would have no block names to use")
    return text


def write_default(entries: list[dict]) -> None:
    """Make `entries` the new default copy on disk (Set as default). Takes
    full effect after a restart: the block-name enum is built at import."""
    text = build_directory_text(entries)
    DIRECTORY_PATH.write_text(text, encoding="utf-8")
    SHA_SIDECAR_PATH.write_text(hashlib.sha256(text.encode("utf-8")).hexdigest() + "\n", encoding="utf-8")


def restore_factory() -> None:
    verify_factory()
    DIRECTORY_PATH.write_bytes(FACTORY_PATH.read_bytes())
    SHA_SIDECAR_PATH.unlink(missing_ok=True)
