from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re


@dataclass(frozen=True)
class SpecialistSpec:
    name: str
    description: str
    system_prompt: str
    prompt_file: Path
    when_to_use: str


def _parse_frontmatter(content: str) -> tuple[dict[str, str], str]:
    """Parse YAML-style frontmatter from markdown content."""
    pattern = r"^---\s*\n(.*?)\n---\s*\n(.*)$"
    match = re.match(pattern, content, re.DOTALL)
    if not match:
        return {}, content.strip()

    frontmatter_raw, body = match.groups()
    metadata: dict[str, str] = {}
    try:
        import yaml
        parsed = yaml.safe_load(frontmatter_raw)
        if isinstance(parsed, dict):
            metadata = {str(k): str(v) for k, v in parsed.items()}
    except Exception:
        # Fallback simple key-value parser
        current_key = None
        current_val: list[str] = []
        for line in frontmatter_raw.splitlines():
            if ":" in line and not line.startswith(" "):
                if current_key:
                    metadata[current_key] = " ".join(current_val).strip()
                k, v = line.split(":", 1)
                current_key = k.strip()
                current_val = [v.strip()]
            elif current_key:
                current_val.append(line.strip())
        if current_key:
            metadata[current_key] = " ".join(current_val).strip()

    return metadata, body.strip()


def load_specialists(prompts_dir: Path | None = None) -> dict[str, SpecialistSpec]:
    """Load specialist definitions from markdown files in the prompts directory."""
    if prompts_dir is None:
        prompts_dir = Path(__file__).parent / "prompts"

    if not prompts_dir.exists() or not prompts_dir.is_dir():
        return {}

    specialists: dict[str, SpecialistSpec] = {}
    for path in sorted(prompts_dir.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        meta, body = _parse_frontmatter(text)
        name = meta.get("name", path.stem)
        description = meta.get("description", f"Specialist reviewer for {name}.")
        when_to_use = meta.get("when_to_use", "Always use this specialist.")
        specialists[name] = SpecialistSpec(
            name=name,
            description=description,
            system_prompt=body,
            prompt_file=path,
            when_to_use=when_to_use,
        )

    return specialists


def format_catalog_summary(specialists: dict[str, SpecialistSpec]) -> str:
    """Format the specialist catalog into a human- and LLM-readable summary."""
    if not specialists:
        return "No specialists available in the catalog."
    lines = []
    for name, spec in specialists.items():
        lines.append(f"- '{name}': {spec.when_to_use}\n  ({spec.description})")
    return "\n".join(lines)
