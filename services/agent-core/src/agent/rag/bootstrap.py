"""Install the versioned built-in skill once without overwriting user edits."""
from pathlib import Path
import shutil


def install_builtin_skill(registry):
    _install(registry, 'airworthiness-clause-qa', 'builtin-rag-skill-v1.installed')
    _install(registry, 'local-document-work', 'builtin-local-document-work-v1.installed')


def _install(registry, name, marker_name):
    marker = registry.path.parent / marker_name
    if marker.exists():
        return
    if any(item.get('name') == name for item in registry.list()):
        marker.write_text('1', encoding='utf-8')
        return
    source = Path(__file__).parent.parent / 'builtin_skills' / name
    target = registry.path.parent / 'skills' / name
    if not target.exists():
        shutil.copytree(source, target)
    registry.add(str(target))
    marker.write_text('1', encoding='utf-8')
