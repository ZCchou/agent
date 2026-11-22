import asyncio

from agent.rag.grounding import RagGrounding


def test_document_review_admits_project_scope_before_targeted_search():
    class Sources:
        def project_state(self, project_id):
            return {'enabled': True, 'generation': 3}

        async def search(self, *args, **kwargs):
            raise AssertionError('A generic initial search must not block per-target review')

    grounding = RagGrounding(Sources())
    run = {'id': 'run', 'user_message_id': 'user', '_template_review_task': True,
           'rag_decision': {'required': True}}
    conversation = {'project_id': 'project', 'messages': [
        {'id': 'user', 'role': 'user', 'content': '检查这份模板'}]}
    asyncio.run(grounding.prepare(run, conversation))
    assert grounding.runs['run']['generation'] == 3
    assert grounding.runs['run']['evidence'] == {}
