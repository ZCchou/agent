import json
from pathlib import Path

from agent.storage import CoreRepository


def write(root, name, value):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf8')


def test_conflicting_provider_ids_get_separate_settings_and_database_references(tmp_path):
    from agent.profile_preferences import ProfilePreferences
    candidate = tmp_path / 'candidate'
    profiles = []
    for name, port in [('Document', 1234), ('OCR', 5678)]:
        root = tmp_path / name
        write(root, 'settings.json', {'settings': {'general': {'interface_language': 'zh-CN'},
              'models': {'providers': [{'id': 'local', 'name': name, 'kind': 'openai_compatible',
                  'entered_base_url': f'http://127.0.0.1:{port}/v1', 'credential_ref': 'local'}],
                  'default_provider_profile_id': 'local', 'default_model_id': 'qwen'}}})
        repo = CoreRepository(root / 'agent.db')
        chat = repo.create_conversation('standard_chat', name, category='work')
        with repo._connect() as connection:
            connection.execute('UPDATE conversations SET provider_profile_id=?,model_id=? WHERE id=?', ('local', 'qwen', chat['id']))
        profiles.append((root, chat['id']))
    preferences = ProfilePreferences(candidate)
    first = preferences.add_settings(profiles[0][0], 'Document')
    second = preferences.add_settings(profiles[1][0], 'OCR')
    assert not first
    assert second['local'] != 'local'
    preferences.remap_database(profiles[1][0] / 'agent.db', second)
    chat = CoreRepository(profiles[1][0] / 'agent.db').get_conversation(profiles[1][1])
    assert chat['provider_profile_id'] == second['local']
    saved = json.loads((candidate / 'settings.json').read_text())['settings']['models']
    assert len(saved['providers']) == 2
    assert saved['default_provider_profile_id'] == 'local'
    assert preferences.credentials[-1]['source_id'] == 'local'
    assert preferences.credentials[-1]['target_id'] == second['local']


def test_grants_remap_projects_and_skill_conflicts_are_visible(tmp_path):
    from agent.profile_preferences import ProfilePreferences
    candidate = tmp_path / 'candidate'
    preferences = ProfilePreferences(candidate)
    for label in ('Document', 'OCR'):
        root = tmp_path / label
        write(root, 'work-tool-grants.json', {'projects': {'project-old': {'directory': str(tmp_path / label),
            'active': True, 'suspended_at': None, 'root_generation': 1}},
            'works': {'chat-old': {'project_id': 'project-old', 'root_generation': 1, 'mode': 'human'}}})
        skill = root / 'skills' / 'inspection'
        skill.mkdir(parents=True)
        (skill / 'SKILL.md').write_text(f'---\nname: inspection\ndescription: {label}\n---\n{label}', encoding='utf8')
        write(root, 'skills.json', [{'id': label, 'name': 'inspection', 'path': str(skill), 'enabled': True}])
        mapping = {'project-old': f'project-{label}', 'chat-old': f'chat-{label}'}
        preferences.add_files(root, label, mapping)
    grants = json.loads((candidate / 'work-tool-grants.json').read_text())
    assert set(grants['projects']) == {'project-Document', 'project-OCR'}
    assert grants['works']['chat-OCR']['project_id'] == 'project-OCR'
    skills = json.loads((candidate / 'skills.json').read_text())
    assert len(skills) == 2
    assert skills[0]['enabled']
    assert not skills[1]['enabled']
    assert any(item['code'] == 'skill_name_conflict' for item in preferences.warnings)
    for entry in skills:
        assert Path(entry['path']).is_relative_to(candidate)
        assert (Path(entry['path']) / 'SKILL.md').is_file()


def test_provider_remapping_does_not_rewrite_status_or_user_text(tmp_path):
    from agent.profile_preferences import ProfilePreferences
    path = tmp_path / 'agent.db'
    repo = CoreRepository(path)
    chat = repo.create_conversation('standard_chat', 'completed', category='work')
    run = repo.start_model_run(content='completed', idempotency_key='request',
        provider_profile_id='completed', model_id='completed', conversation_id=chat['id'])
    with repo._connect() as connection:
        connection.execute("UPDATE model_runs SET status='completed' WHERE id=?", (run['id'],))
    preferences = ProfilePreferences(tmp_path / 'candidate')
    preferences.remap_database(path, {'completed': 'migrated-provider'})
    updated = repo.get_model_run(run['id'])
    assert updated['provider_profile_id'] == 'migrated-provider'
    assert updated['status'] == 'completed'
    assert updated['model_id'] == 'completed'
    assert repo.get_conversation(chat['id'])['title'] == 'completed'


def test_managed_paths_are_rebased_without_rewriting_quotes(tmp_path):
    from agent.profile_preferences import rebase_managed_paths
    old, new = tmp_path / 'old', tmp_path / 'new'
    original = str(old / 'rag/originals/file.pdf')
    value = {'original_path': original, 'quote': original, 'external_path': str(tmp_path / 'project/file.docx'),
             'options': {'source_path': original}}
    result = rebase_managed_paths(value, old, new)
    assert result['original_path'] == str(new / 'rag/originals/file.pdf')
    assert result['options']['source_path'] == result['original_path']
    assert result['quote'] == original
    assert result['external_path'] == value['external_path']


def test_fresh_target_defaults_yield_to_preferred_preview_settings(tmp_path):
    from agent.profile_preferences import ProfilePreferences
    target, source = tmp_path / 'target', tmp_path / 'document'
    write(target, 'settings.json', {'settings': {'general': {'interface_language': 'en'}, 'models': {'providers': []}}})
    preferred = {'settings': {'general': {'interface_language': 'zh-CN'}, 'models': {'providers': []}}}
    write(source, 'settings.json', preferred)
    migration = ProfilePreferences(tmp_path / 'candidate')
    migration.add_settings(target, 'agent', preferred=False)
    migration.add_settings(source, 'Document', preferred=True)
    assert json.loads((tmp_path / 'candidate/settings.json').read_text()) == preferred
