"""Rendering a brew into shareable formats.

Shared by the management command and the API so the paste-ready decklist can
never drift between them.
"""


def as_text(result):
    """Paste-ready decklist: `1 Card Name` per line.

    Imports directly into Moxfield and Archidekt. Basics are emitted with their
    quantity, which is the one place a Commander list legitimately exceeds one
    copy.
    """
    lines = [f"1 {result.commander.name}"]
    if getattr(result, "partner", None):
        lines.append(f"1 {result.partner.name}")
    lines += [f"1 {c.name}" for c in sorted(result.spells, key=lambda c: c.name)]
    lines += [
        f"{qty} {cand.name}"
        for cand, qty in sorted(result.lands, key=lambda lq: lq[0].name)
    ]
    return "\n".join(lines)
