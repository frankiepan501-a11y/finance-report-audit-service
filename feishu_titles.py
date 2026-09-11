"""Pure title formatter vendored from scripts/_lib/feishu_title.py.

No credentials, recipient routing or sending helpers are copied into this service.
"""
BIZ_TO_GROUP = dict.fromkeys(("AMZ", "SEO", "KOL", "FIN", "HR", "PAY", "INV", "LOG", "AOS", "AUDIT", "CUS", "TEAM"))
LEVEL_EMOJI = {"P0": "🔴", "P1": "🟠", "P2": "🟡", "P3": "🟢"}


def format_title(biz: str, level: str, title: str, suffix: str = "") -> str:
    if biz not in BIZ_TO_GROUP:
        raise ValueError(f"unknown biz code: {biz!r} (允许: {list(BIZ_TO_GROUP)})")
    if level not in LEVEL_EMOJI:
        raise ValueError(f"unknown level: {level!r} (允许: P0/P1/P2/P3)")
    emoji = LEVEL_EMOJI[level]
    head = f"{emoji} [{biz}·{level}] {title}"
    return f"{head} · {suffix}" if suffix else head
