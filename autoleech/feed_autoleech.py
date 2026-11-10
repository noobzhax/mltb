#!/usr/bin/env python3
"""Append grabbed links to the mltb autoleech drop-box.

Usage:
  feed_autoleech.py links.txt [more.txt ...]   # one URL per line
  feed_autoleech.py -                            # URLs from stdin
  cat links.txt | feed_autoleech.py

Skips blanks, comments and anything already queued/done/dead, so re-running
after a crash is safe.
"""
import re
import sys
from pathlib import Path

BOX = Path("/home/ubuntu/dev/mltb/autoleech")
_VID_RE = re.compile(
    r"^https?://(?:www\.)?vidara\.[a-z]+/(?:v/|e/)?([A-Za-z0-9]{6,})/?$", re.I
)


def link_key(link):
    """Must match bot/helper/ext_utils/leech_history.link_key."""
    link = link.strip()
    m = _VID_RE.match(link)
    return f"vidara:{m.group(1)}" if m else link


def read(path):
    p = Path(path)
    if not p.exists():
        return []
    if p.suffix == ".json":
        import json

        data = json.loads(p.read_text())
        return [d if isinstance(d, str) else d.get("url", "") for d in data]
    return p.read_text().splitlines()


def main(argv):
    src = [l for a in argv for l in (sys.stdin.read().splitlines() if a == "-" else read(a))]
    seen = set()
    for f in ("queue", "done", "dead", "leech_history"):
        seen.update(link_key(x) for x in read(BOX / f"{f}.txt"))
    new = [l.strip() for l in src if l.strip() and not l.strip().startswith("#")]
    new = [l for l in dict.fromkeys(new) if link_key(l) not in seen]
    if not new:
        print("nothing new")
        return 0
    BOX.mkdir(parents=True, exist_ok=True)
    with (BOX / "queue.txt").open("a") as f:
        f.writelines(f"{l}\n" for l in new)
    print(f"queued {len(new)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
