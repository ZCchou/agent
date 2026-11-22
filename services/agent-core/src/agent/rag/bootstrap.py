"""Install the versioned built-in skill once without overwriting user edits."""
from hashlib import sha256
from pathlib import Path
import logging
import os
import shutil
from uuid import uuid4

from agent.skills import _LOCK


# SHA-256 of the shipped v1 SKILL.md in both official Git checkout line endings.
_GROUNDED_REVIEW_V1 = {
    '6213004447ea1089d17abbc4920b6d432a68e9b127905f7c079deb62edb70f9c',
    '9ac52782dc48fc40e2fae8eb7e24418444ab80baaeffd44b827fb59ab391ade4',
}


def install_builtin_skill(registry):
    _install(registry, 'airworthiness-clause-qa', 'builtin-rag-skill-v1.installed')
    _install(registry, 'local-document-work', 'builtin-local-document-work-v1.installed')
    with _LOCK:
        _install(registry, 'grounded-document-review', 'builtin-grounded-document-review-v1.installed')
        try:
            _upgrade_review_skill(registry)
        except OSError:
            logging.getLogger(__name__).warning('Could not upgrade the built-in grounded review skill.', exc_info=True)


def _upgrade_review_skill(registry):
    name = 'grounded-document-review'
    root = registry.path.parent.resolve() / 'skills' / name
    target = root / 'SKILL.md'
    if target.resolve() != target or not target.is_file():
        return
    # Removal keeps the package on disk. Only a still-registered built-in may be updated.
    if not any(entry.get('name') == name and Path(entry['path']) == root for entry in registry._entries()):
        return
    with target.open('rb') as existing:
        previous = existing.read(64_001)
    if sha256(previous).hexdigest() not in _GROUNDED_REVIEW_V1:
        return
    current = (Path(__file__).parent.parent / 'builtin_skills' / name / 'SKILL.md').read_bytes()
    if current == previous:
        return
    temporary = root / f'.skill-upgrade-{uuid4().hex}.tmp'
    try:
        with temporary.open('xb') as handle:
            handle.write(current)
        if target.resolve() == target and target.is_file() and target.read_bytes() == previous:
            os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


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
