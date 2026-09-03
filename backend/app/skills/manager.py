import os
from pathlib import Path
from typing import List, Optional
from .parser import parse_skill_md, Skill

class SkillManager:
    def __init__(self, config):
        self.config = config
        self._cache = {} # path+mtime -> Skill
    
    def discover_skills(self) -> List[Skill]:
        # Bundled skills live at <backend-root>/skills (e.g. /app/skills in the
        # Docker image). Resolved from this file so it is independent of the
        # process working directory.
        bundled_skills = Path(__file__).resolve().parent.parent.parent / "skills"
        paths = [
            bundled_skills,
            Path.home() / ".thunder" / "skills", # User global
            Path(".thunder/skills/"), # Project local
        ]
        paths.extend(Path(p) for p in self.config.extra_paths)

        skills = {}
        for root in paths:
            if not root.exists() or not root.is_dir():
                continue
            
            # Safety: resolve symlinks
            root = root.resolve()

            for item in root.iterdir():
                if item.is_dir() and not item.name.startswith('.'):
                    skill_file = item / "SKILL.md"
                    if skill_file.exists():
                        # Check symlink escape
                        if not str(skill_file.resolve()).startswith(str(root)):
                            continue
                        
                        skill = parse_skill_md(skill_file, source=str(root))
                        if skill:
                            skills[skill.name] = skill # Later wins
        
        return list(skills.values())

    def get_skill_index(self, user_message: str = "") -> str:
        skills = self.discover_skills()
        if not skills:
            return ""

        # TODO: Implement token budget and keyword ranking here
        # For now, simple list
        lines = ["\n\n=== Available Skills ==="]
        for s in skills[:10]:
            lines.append(f"- {s.name}: {s.description}")
        lines.append("\nTo use a skill, output <skill>skill-name</skill> or call the skill tool.")
        return "\n".join(lines)

    def get_skill_body(self, name: str) -> Optional[Skill]:
        skills = self.discover_skills()
        for s in skills:
            if s.name == name:
                return s
        return None
