"""Install the versioned built-in skill once without overwriting user edits."""
from pathlib import Path
import shutil


def install_builtin_skill(registry):
    marker = registry.path.parent / 'builtin-rag-skill-v1.installed'
    if marker.exists():
        return
    name = 'airworthiness-clause-qa'
    if any(item.get('name') == name for item in registry.list()):
        marker.write_text('1', encoding='utf-8')
        return
    source = Path(__file__).parent.parent / 'builtin_skills' / name
    target = registry.path.parent / 'skills' / name
    if not target.exists():
        shutil.copytree(source, target)
    registry.add(str(target))
    marker.write_text('1', encoding='utf-8')
