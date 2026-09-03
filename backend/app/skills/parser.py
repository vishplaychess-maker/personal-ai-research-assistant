import re
import os
from pathlib import Path
from typing import Optional, Dict, Any

class Skill:
    def __init__(self, name: str, description: str, body: str, dir_path: str, source: str, pinned: bool = False):
        self.name = name
        self.description = description
        self.body = body
        self.dir_path = dir_path
        self.source = source
        self.pinned = pinned

def parse_skill_md(file_path: Path, source: str) -> Optional[Skill]:
    try:
        content = file_path.read_text(encoding="utf-8")
    except Exception:
        return None

    # Minimal YAML frontmatter parser
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)", content, re.DOTALL)
    if not match:
        return None

    frontmatter_str, body = match.groups()
    meta = {}
    for line in frontmatter_str.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip()

    name = meta.get("name", file_path.parent.name)
    desc = meta.get("description", "")
    pinned = meta.get("pinned", "false").lower() == "true"

    if not re.match(r"^[a-z0-9]+(-[a-z0-9]+)*$", name):
        # Warn invalid name
        return None

    return Skill(
        name=name,
        description=desc[:250],
        body=body.strip(),
        dir_path=str(file_path.parent.resolve()),
        source=source,
        pinned=pinned
    )
