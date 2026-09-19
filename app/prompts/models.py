"""The admin-editable override for each piece of the prompt sent to the
model (app/llm/pipeline.py's DEFAULT_SYSTEM_TEMPLATE, DEFAULT_BUILD_EXTRA,
DEFAULT_DEBUG_EXTRA).

Two layers per field, not one:
- `<field>` -- the ACTIVE text actually sent to the model right now. `None`
  means "use this field's default" (see below), not "empty text".
- `<field>_default` -- what "Reset to default" reverts `<field>` to, and
  what a blank/never-touched admin page shows. `None` means "use the
  hardcoded constant in pipeline.py" -- the TRUE original. Set to
  something else only via the admin page's "Set as default" action, which
  promotes whatever text is in the box right now into this slot.

Why two layers: without `<field>_default`, "Reset to default" could only
ever mean "throw away my tuning and go back to the code's original text" --
fine the first time, but useless once a teacher has actually settled on
their own baseline after a few rounds of editing (their real "default" is
now THEIR text, not the original). With it, "reset" means "go back to
whatever I last called my default", and the ORIGINAL hardcoded text is
still recoverable (it's `None` all the way down, i.e. every override AND
default cleared -- or just read straight out of pipeline.py's source).

An admin who has never touched the Prompts page has every field `None` at
both layers, which is exactly the behavior this app had before this
feature existed."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

FIELDS = ("system_template", "build_extra", "debug_extra")


@dataclass
class PromptOverrides:
    system_template: Optional[str] = None
    system_template_default: Optional[str] = None
    build_extra: Optional[str] = None
    build_extra_default: Optional[str] = None
    debug_extra: Optional[str] = None
    debug_extra_default: Optional[str] = None

    def effective_default(self, field: str, hardcoded: str) -> str:
        """What "Reset to default" reverts `field` to -- a teacher's own
        saved default if they've set one, else the true hardcoded text."""
        return getattr(self, f"{field}_default") or hardcoded

    def effective(self, field: str, hardcoded: str) -> str:
        """What actually gets sent to the model for `field` right now."""
        return getattr(self, field) or self.effective_default(field, hardcoded)

    def to_dict(self) -> dict:
        return {f: getattr(self, f) for f in self._all_keys()}

    @classmethod
    def from_dict(cls, data: dict) -> "PromptOverrides":
        return cls(**{f: data.get(f) for f in cls._all_keys()})

    @staticmethod
    def _all_keys() -> list[str]:
        return [k for f in FIELDS for k in (f, f"{f}_default")]
