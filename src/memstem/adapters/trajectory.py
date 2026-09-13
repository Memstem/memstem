"""Conversation retention helpers shared by OpenClaw readers and the pipeline."""

from __future__ import annotations

import re
from difflib import SequenceMatcher


def merge_transcripts(previous: str, incoming: str) -> str:
    """Ordered union of turns; never discard an already captured turn.

    Matching blocks align overlapping rolling windows. Unmatched old turns stay
    before unmatched new turns at the same boundary. Repeated identical turns
    retain their multiplicity; a repeated snapshot does not append duplicates.
    Disjoint windows append. This is retention, not an editorial text merge.
    """
    if not previous:
        return incoming
    if not incoming or previous == incoming:
        return previous
    split = r"\n\n(?=\*\*(?:User|Assistant):\*\* )"
    old = re.split(split, previous)
    new = re.split(split, incoming)
    merged: list[str] = []
    for op, a, b, c, d in SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
        if op in ("equal", "delete", "replace"):
            merged.extend(old[a:b])
        if op in ("insert", "replace"):
            merged.extend(new[c:d])
    return "\n\n".join(merged)
