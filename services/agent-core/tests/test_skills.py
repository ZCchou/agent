from pathlib import Path

import pytest

from agent.storage import CoreDataError


def registry(tmp_path):
    from agent.skills import SkillRegistry
    return SkillRegistry(tmp_path / 'data' / 'skills.json')


def test_disabled_migration_conflict_preserves_enabled_skill_and_rag_requirement(tmp_path):
    r = registry(tmp_path)
    preferred = package(tmp_path)
    text = (preferred / 'SKILL.md').read_text(encoding='utf8').replace('description: >', 'rag:\n  required: true\ndescription: >')
    (preferred / 'SKILL.md').write_text(text, encoding='utf8')
    first = r.add(str(preferred))
    incoming = package(tmp_path, name='incoming')
    (incoming / 'SKILL.md').write_text(text, encoding='utf8')
    r._save([*r._entries(), {'id': 'incoming', 'name': 'lesson', 'path': str(incoming), 'enabled': False}])
    assert r.load('lesson')['id'] == first['id']
    assert r.inspect('lesson')['id'] == first['id']
    assert r.requires_rag('$lesson 请依据条款回答')
    with pytest.raises(CoreDataError, match='enabled'):
        r.set_enabled('incoming', True)
    assert not next(item for item in r.list() if item['id'] == 'incoming')['enabled']
    r.set_enabled(first['id'], False)
    r.set_enabled('incoming', True)
    assert r.load('lesson')['id'] == 'incoming'


def package(tmp_path, name='lesson', policy=True):
    root = tmp_path / name
    root.mkdir()
    (root / 'SKILL.md').write_text('---\nname: ' + name + '\ndescription: >\n  Teach concepts\n  with examples\n---\nExplain using a small worked example.\n', encoding='utf-8')
    (root / 'references').mkdir()
    (root / 'references' / 'example.md').write_text('abcdef', encoding='utf-8')
    (root / 'agents').mkdir()
    (root / 'agents' / 'openai.yaml').write_text('policy:\n  allow_implicit_invocation: ' + str(policy).lower(), encoding='utf-8')
    return root


def test_import_refresh_persist_disable_and_forget(tmp_path):
    r = registry(tmp_path)
    root = package(tmp_path)
    item = r.add(str(root))
    assert item['name'] == 'lesson'
    assert item['description'] == 'Teach concepts with examples'
    assert registry(tmp_path).list()[0]['enabled']
    assert 'worked example' in r.load('lesson')['content']
    r.set_enabled(item['id'], False)
    with pytest.raises(CoreDataError, match='disabled'):
        r.load('lesson')
    r.set_enabled(item['id'], True)
    (root / 'SKILL.md').write_text('broken', encoding='utf-8')
    assert r.list()[0]['error']
    with pytest.raises(CoreDataError):
        r.load('lesson')
    r.remove(item['id'])
    assert r.list() == []
    assert root.exists()


def test_resources_are_paged_and_confined(tmp_path):
    r = registry(tmp_path)
    r.add(str(package(tmp_path)))
    result = r.read_resource('lesson', 'references/example.md', offset=2, length=2)
    assert result['content'] == 'cd'
    assert result['next_offset'] == 4
    for path in ['../secret', '/secret', 'C:\\secret', '..\\secret']:
        with pytest.raises(CoreDataError):
            r.read_resource('lesson', path)
    outside = tmp_path / 'outside.md'
    outside.write_text('secret', encoding='utf-8')
    link = tmp_path / 'lesson' / 'escape.md'
    try:
        link.symlink_to(outside)
    except OSError:
        return
    with pytest.raises(CoreDataError):
        r.read_resource('lesson', 'escape.md')


def test_metadata_only_implicit_policy_and_explicit_text_fallback(tmp_path):
    r = registry(tmp_path)
    r.add(str(package(tmp_path, policy=False)))
    assert r.catalog() == []
    assert 'worked example' not in r.render('teach me', tools_available=True)
    explicit = r.render('$lesson explain', tools_available=False)
    assert 'worked example' in explicit
    assert 'terminal' in explicit
    with pytest.raises(CoreDataError):
        r.load('lesson', explicit=False)


def test_bad_import_and_duplicate_names_leave_registry_usable(tmp_path):
    r = registry(tmp_path)
    bad = tmp_path / 'bad'
    bad.mkdir()
    (bad / 'SKILL.md').write_text('---\nname: [broken\n---\ntext', encoding='utf-8')
    with pytest.raises(CoreDataError):
        r.add(str(bad))
    r.add(str(package(tmp_path)))
    other = tmp_path / 'other'
    other.mkdir()
    (other / 'SKILL.md').write_text((tmp_path / 'lesson' / 'SKILL.md').read_text(), encoding='utf-8')
    with pytest.raises(CoreDataError):
        r.add(str(other))
    assert len(r.list()) == 1


def test_instruction_text_with_horizontal_rule_and_large_body_is_bounded(tmp_path):
    r = registry(tmp_path)
    root = package(tmp_path)
    source = (root / 'SKILL.md').read_text(encoding='utf-8')
    (root / 'SKILL.md').write_text(source + '\n---\n' + 'A' * 30000, encoding='utf-8')
    r.add(str(root))
    rendered = r.render('$lesson explain', tools_available=False, max_characters=4000)
    assert len(rendered) <= 4000
    assert 'truncated' in rendered


def test_frontmatter_accepts_dashes_in_description(tmp_path):
    r = registry(tmp_path)
    root = package(tmp_path)
    (root / 'SKILL.md').write_text('---\nname: lesson\ndescription: "Explain a---b"\n---\nBody', encoding='utf-8')
    r.add(str(root))
    assert r.load('lesson')['description'] == 'Explain a---b'


def test_disabled_skill_remains_inspectable(tmp_path):
    r = registry(tmp_path)
    item = r.add(str(package(tmp_path)))
    r.set_enabled(item['id'], False)
    assert 'worked example' in r.inspect('lesson')['content']


@pytest.mark.parametrize('file', ['SKILL.md', 'agents/openai.yaml'])
@pytest.mark.parametrize('bad_yaml', ['unused: ' + '[' * 1100 + '0' + ']' * 1100, 'unused: ' + '7' * 5000])
def test_external_yaml_damage_does_not_break_catalog_or_answering(tmp_path, file, bad_yaml):
    r = registry(tmp_path)
    root = package(tmp_path)
    content = '---\nname: lesson\ndescription: Lesson\n' + bad_yaml + '\n---\nBody' if file == 'SKILL.md' else bad_yaml
    r.add(str(root))
    (root / file).write_text(content, encoding='utf-8')
    assert r.list()[0]['error']
    assert r.render('hello', tools_available=True) == ''


def test_create_edit_conflict_and_backup(tmp_path):
    r = registry(tmp_path)
    created = r.save_skill('custom', 'Teach neurons', 'Start with weights and bias.')
    assert r.load('custom')['content'] == 'Start with weights and bias.'
    updated = r.save_skill('custom', 'Teach neurons well', 'Use an example.', expected_revision=created['revision'])
    assert updated['revision'] != created['revision']
    with pytest.raises(CoreDataError, match='changed'):
        r.save_skill('custom', 'Stale', 'Do not overwrite', expected_revision=created['revision'])
    assert r.load('custom')['content'] == 'Use an example.'
    assert any('Start with weights' in p.read_text(encoding='utf-8') for p in (r.path.parent / 'skill-backups').rglob('*.md'))


def test_external_edit_between_version_inspect_and_read_is_not_overwritten(tmp_path, monkeypatch):
    r = registry(tmp_path)
    original = r.save_skill('custom', 'Teach', 'Original')
    inspect = r.inspect
    def change_after_inspect(name):
        result = inspect(name)
        Path(result['path'], 'SKILL.md').write_text('---\nname: custom\ndescription: Teach\n---\nExternal update\n', encoding='utf-8')
        return result
    monkeypatch.setattr(r, 'inspect', change_after_inspect)
    with pytest.raises(CoreDataError, match='changed'):
        r.save_skill('custom', 'Teach', 'Stale model update', expected_revision=original['revision'])
    assert 'External update' in Path(original['path'], 'SKILL.md').read_text(encoding='utf-8')


def test_model_save_admission_serializes_with_disable(tmp_path, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    r = registry(tmp_path)
    item = r.save_skill('custom', 'Teach', 'Original')
    loaded, attempted, revoked = threading.Event(), threading.Event(), threading.Event()
    original_load = r.load
    def load(name, **kwargs):
        result = original_load(name, **kwargs)
        loaded.set()
        assert attempted.wait(2)
        assert not revoked.wait(0.05), 'Disable interleaved with already admitted model save'
        return result
    monkeypatch.setattr(r, 'load', load)
    def revoke():
        assert loaded.wait(2)
        attempted.set()
        r.set_enabled(item['id'], False)
        revoked.set()
    with ThreadPoolExecutor(max_workers=1) as pool:
        task = pool.submit(revoke)
        r.execute('skills.save', {'name': 'custom', 'description': 'Teach', 'content': 'Updated', 'expected_revision': item['revision']}, '')
        task.result(timeout=2)
    assert revoked.is_set()
    assert r.inspect('custom')['content'] == 'Updated'
    assert r.list()[0]['enabled'] is False
