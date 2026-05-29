from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

PRESET_DIR = Path(__file__).parent / "presets"


@dataclass
class Preset:
    name: str
    title: str
    description: str
    tags: list[str]
    prompt: str


def _parse_preset(path: Path) -> Preset:
    text = path.read_text(encoding="utf-8")

    def extract_section(heading: str) -> str:
        m = re.search(rf"## {heading}\n(.*?)(?=\n## |\Z)", text, re.DOTALL)
        return m.group(1).strip() if m else ""

    title_m = re.search(r"^# (.+)$", text, re.MULTILINE)
    title = title_m.group(1).strip() if title_m else path.stem

    description = extract_section("Description")
    tags_raw = extract_section("Tags")
    tags = [t.strip() for t in tags_raw.split(",") if t.strip()]
    prompt = extract_section("Prompt")

    return Preset(name=path.stem, title=title, description=description, tags=tags, prompt=prompt)


def list_presets() -> list[Preset]:
    if not PRESET_DIR.exists():
        return []
    return [_parse_preset(p) for p in sorted(PRESET_DIR.glob("*.md"))]


def load_preset(name: str) -> Preset | None:
    path = PRESET_DIR / f"{name}.md"
    if not path.exists():
        return None
    return _parse_preset(path)

