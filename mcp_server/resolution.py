"""Name-or-id matching shared by every backend, so one resolution policy rules them all:
exact id, then case-insensitive exact name, then unique substring — anything else is an
error that tells the model how to recover."""


def pick_by_name(kind: str, items: list[dict], name_or_id: str | int) -> dict:
    """Pick the single item matching ``name_or_id`` or raise a ValueError that lists
    the candidates (ambiguous) or all known names (no match)."""
    text = str(name_or_id).strip()
    if not text:
        raise ValueError(f"{kind} name or id must not be blank")
    for item in items:
        if str(item.get("id")) == text:
            return item
    lowered = text.lower()
    exact = [item for item in items if str(item["name"]).lower() == lowered]
    if len(exact) == 1:
        return exact[0]
    partial = [item for item in items if lowered in str(item["name"]).lower()]
    if len(partial) == 1:
        return partial[0]
    if partial:
        options = "; ".join(f"{item['name']} (id {item['id']})" for item in partial)
        raise ValueError(
            f"Ambiguous {kind} {text!r} — matches: {options}. Retry with the exact name or the id."
        )
    known = ", ".join(str(item["name"]) for item in items)
    raise ValueError(f"No {kind} found matching {text!r}. Known {kind}s: {known}")
