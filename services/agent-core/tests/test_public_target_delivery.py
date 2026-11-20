import asyncio
from types import SimpleNamespace

import pytest

from agent import document_review as review

def test_requested_audit_table_maps_complete_rows_to_position_column_only():
    request = '读取附件的正文和表格，再按「审查项｜附件原文及位置｜依据｜判断｜修改建议」输出。'
    messages, _ = review.review_request(dict(freeform_review=True, request=request, fields=[]), {'evidence':[]})
    assert 'one concise Markdown table' in messages[0]['content']
    assert 'first target heading' not in messages[0]['content']
    header = '|审查项|附件原文及位置|依据|判断|修改建议|\n|---|---|---|---|---|\n'
    good = '|温变|p-1 工作温度|原文提到 p-2|待补充|明确计时起点|\n'
    bad = '|湿热|p-3|依据|'
    body = header + good + bad
    results = review._public_target_delivery(body, ['p-1','p-2','p-3'], 'truncated')
    assert [r['status'] for r in results] == ['delivered','pending','pending']
    assert results[0]['body_ranges'][0]['text'] == good


def test_explicit_blocks_map_exact_saved_text_and_truncated_tail_stays_pending():
    body = "Overview p-9 is not a result.\n## p-0\nA real finding.\n## p-1\nunfinished"
    rows = review._public_target_delivery(body, ["p-0", "p-1", "p-9"], "truncated")
    assert [row["status"] for row in rows] == ["delivered", "pending", "pending"]
    span = rows[0]["body_ranges"][0]
    assert body[span["start"]:span["end"]] == "## p-0\nA real finding.\n"
    assert all(row["technical_status"] == "unverified" for row in rows)


def test_unmarked_or_foreign_positions_never_infer_delivery():
    assert review._public_target_delivery("A complete-looking paragraph.", ["p-0"], "present")[0]["status"] == "pending"
    assert review._public_target_delivery("## p-99\nForeign.", ["p-0"], "present")[0]["status"] == "pending"


@pytest.mark.parametrize('closed', [False, True])
def test_outer_markdown_wrap_keeps_original_offsets_and_ignores_inner_examples(closed):
    body = "````markdown\n# Report\n### p-0\nEvidence.\n```text\n## p-9\nExample.\n```\n### p-1\nTail."
    if closed:
        body += "\n````\n"
    rows = review._public_target_delivery(body, ['p-0', 'p-9','p-1'], 'present' if closed else 'truncated')
    assert [r['status'] for r in rows] == ['delivered','pending','delivered' if closed else 'pending']
    span = rows[0]['body_ranges'][0]
    assert body[span['start']:span['end']] == span['text']
    assert span['text'].startswith('### p-0\n')


def test_supplements_split_using_actual_evidence_and_request(monkeypatch):
    base=dict(stage='review', request='补充', token_budget=12000, freeform_review=True)
    fields=[dict(id=f'p-{i}',chapter_path=[],section_id='s') for i in range(7)]
    context={'evidence':[{'id':'retained'}]}
    monkeypatch.setattr(review, '_fits', lambda batch,sources,budget: sources is not context or len(batch['fields'])<=2)
    batch=review._supplement_page(base,fields,0,0,4096,context)
    assert batch['target_ids']==['p-0','p-1']


def test_primary_length_stop_shrinks_next_main_page_after_successful_supplement(monkeypatch):
    import copy
    class Activity:
        task_id='task'
        revision=attempt=1
        stage='document_review'
        def __init__(self): self.saved={}
        def load_checkpoint(self,key,identity): return copy.deepcopy(self.saved.get(key))
        def checkpoint(self,key,identity,value):
            self.saved[key]=copy.deepcopy(value)
            return True
        def progress(self,**details): return True
    plans=SimpleNamespace(goals=SimpleNamespace(get=lambda _:{}),record_results=lambda *args,**kwargs:None)
    monkeypatch.setattr("agent.execution.task_plans.TaskPlans",lambda _:plans)
    async def validate(pending,*args): return pending['results']
    monkeypatch.setattr(review,'_validate',validate)
    fields=[dict(id=f'p-{i}',text='a',field_type='text',chapter_path=[],section_id='s',position={}) for i in range(8)]
    calls=[]
    async def generate(batch,context):
        ids=batch['target_ids']
        calls.append(list(ids))
        body='\n'.join(f'## {target}\nFinding.' for target in ids)
        return {'public_review_body':body,'public_output_state':'truncated' if len(calls)==1 else 'present'}
    async def evidence(batch):return {'evidence':[]}
    activity=Activity()
    async def run():
        return await review._freeform_review(SimpleNamespace(repository=None,path_for=lambda *args:None),
            'run',dict(id='template',sha256='h',project_id='project',manifest={}), 'review',fields,generate,evidence,
            activity=activity,identity={'batch_output_tokens':512},token_allowance=12000)
    report=asyncio.run(run())['report']
    assert calls==[['p-0','p-1','p-2','p-3'],['p-3'],['p-4','p-5'],['p-6','p-7']]
    assert report['coverage']['prose_processed']==8
    asyncio.run(run())
    assert len(calls)==4


@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("capacity", [False, True])
def test_only_unresolved_targets_supplement_once_and_resume_preserves_original(monkeypatch, cancel, capacity):
    async def exercise():
        cache = {}
        class Activity:
            task_id = "task"
            revision = attempt = 1
            stage = "document_review"
            def load_checkpoint(self, key, identity):
                return cache.get((key, review._hash(identity)))
            def checkpoint(self, key, identity, value):
                import copy
                cache[key, review._hash(identity)] = copy.deepcopy(value)
                return True
            def progress(self, **kwargs):
                return True
        class Plans:
            def __init__(self, repository):
                self.goals = SimpleNamespace(get=lambda _: {})
            def record_results(self, *args, **kwargs):
                pass
        monkeypatch.setattr("agent.execution.task_plans.TaskPlans", Plans)
        async def validate(pending, fields, template, resolver):
            return pending["results"]
        monkeypatch.setattr(review, "_validate", validate)
        if capacity:
            monkeypatch.setattr(review, "_fits", lambda batch, context, limit: "仅补充" not in batch["request"])
        workflow = SimpleNamespace(repository=None, path_for=lambda *args: None)
        fields = [dict(id=f"p-{i}", text="actual source", field_type="text", section_id="s", chapter_path=[], position={}) for i in range(2)]
        template = dict(id="template", sha256="sha", project_id="project", manifest={})
        calls = []
        async def generate(batch, sources):
            calls.append(batch["target_ids"])
            if len(calls) == 1:
                return {"public_review_body": "Original overview.\n## p-0\nSaved finding.", "public_output_state": "present"}
            assert batch["target_ids"] == ["p-1"]
            if cancel:
                raise asyncio.CancelledError()
            return {"public_review_body": "## p-1\nNew finding."}
        async def evidence(batch):
            return {"evidence": [], "generation": None}
        commits = []
        def on_commit(batch, section):
            assert any(stored.get('presented_body') == section['presented_body'] for stored in cache.values())
            commits.append(section['presented_body'])
        async def run():
            return await review._freeform_review(workflow, "run", template, "Review", fields, generate, evidence,
                activity=Activity(), identity={"batch_output_tokens": 4096}, token_allowance=12000, on_public_commit=on_commit)
        if cancel and not capacity:
            with pytest.raises(asyncio.CancelledError):
                await run()
        result = await run()
        assert calls == ([["p-0", "p-1"]] if capacity else [["p-0", "p-1"], ["p-1"]])
        assert result['report']['public_review_sections'][0]['body'] == "Original overview.\n## p-0\nSaved finding."
        assert all(body in result["markdown"] for body in commits)
        assert "Original overview.\n## actual source\nSaved finding." in result["markdown"]
        assert result['report']['public_review_sections'][0]['review_items'][0]['verdict_status'] == 'unmapped'
        states = {row["target_id"]: row["status"] for row in result["report"]["public_target_delivery"]}
        assert states == {"p-0": "delivered", "p-1": "pending" if cancel or capacity else "delivered"}
        assert result["report"]["coverage"]["reviewed"] == 0
        await run()
        assert len(calls) == (1 if capacity else 2)
        assert len(commits) == (1 if capacity or cancel else 2)
    asyncio.run(exercise())


def test_story_ids_labels_and_code_examples_use_exact_actual_targets():
    ids = ["word/header1.xml#p-4", "p-4", "t-0-r0-c0"]
    body = ("```markdown\n## p-4\nExample only.\n```\n"
            "位置：word/header1.xml#p-4\nHeader finding.\n"
            "## p-4 and p-999\nAmbiguous unknown target.\n"
            "### t-0-r0-c0\nReal cell finding.")
    rows = review._public_target_delivery(body, ids, "present")
    assert [row["status"] for row in rows] == ["delivered", "pending", "delivered"]
    assert rows[0]["body_ranges"][0]["text"].startswith("位置：word/header1.xml#p-4\n")
    assert rows[2]["body_ranges"][0]["text"].endswith("Real cell finding.")
