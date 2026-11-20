import json,pytest
from test_rag_grounding import grounding,prepare
from agent.storage import CoreDataError

def setup():
 g=grounding();prepare(g);g.admit('run-1',['evidence-1']);return g

def good(g):return {'text':'应提交报告','evidence':[{'id':'evidence-1','quote_id':next(iter(g.runs['run-1']['quote_options']['evidence-1']))}]}

def test_explicit_gap_without_evidence_does_not_render_unverified_assertion():
 g=setup();g.runs['run-1']['admitted']={}
 out=g.finalize('run-1',json.dumps({'claims':[{'text':'必须是999小时','evidence':[],'evidence_gap':'缺少本项目适用试验时长条款。'}]}))
 assert '999' not in out and '证据缺口' in out
 assert g.answer_schema('run-1')

def test_mixed_answer_preserves_verified_claim_but_not_forged_claim():
 g=setup();bad={'text':'必须是999小时','evidence':[{'id':'evidence-1','quote_id':'fake'}]}
 out=g.finalize('run-1',json.dumps({'claims':[good(g),bad]}))
 assert '应提交报告' in out and 'rag-evidence:evidence-1' in out
 assert '999' not in out and '证据缺口' in out
 assert len(g.runs['run-1']['validated_claims'])==1

def test_mixed_foreign_evidence_remains_hard_rejection():
 g=setup()
 with pytest.raises(CoreDataError) as e:g.finalize('run-1',json.dumps({'claims':[good(g),{'text':'foreign','evidence':[{'id':'foreign','quote':'x'}]}]}))
 assert e.value.code=='rag_citation_invalid'

def test_document_claim_keeps_strict_invalid_quote_contract():
 g=setup()
 with pytest.raises(CoreDataError):g.document_claim('run-1','unverified',[{'id':'evidence-1','quote_id':'fake'}])

@pytest.mark.parametrize('fault',['generation','project_revoked'])
def test_boundary_failure_is_not_softened_by_mixed_gap(fault):
 g=setup()
 def denied(*args,**kwargs):raise CoreDataError('rag_generation_changed' if fault=='generation' else 'rag_evidence_invalid','Denied')
 if fault=='generation':g.service.assert_generation=denied
 else:g.service.validate_evidence=denied
 bad={'text':'false','evidence':[{'id':'evidence-1','quote_id':'fake'}]}
 with pytest.raises(CoreDataError) as e:g.finalize('run-1',json.dumps({'claims':[good(g),bad]}))
 assert e.value.code==('rag_generation_changed' if fault=='generation' else 'rag_evidence_invalid')


def test_no_evidence_schema_contains_only_gap_variant_not_empty_enums():
 g=setup();g.runs['run-1']['admitted']={};g.runs['run-1']['quote_options']={}
 schema=g.answer_schema('run-1')
 variants=schema['properties']['claims']['items']['anyOf']
 assert len(variants)==1 and 'evidence_gap' in variants[0]['required']
 assert '"enum": []' not in json.dumps(schema)


def test_malformed_gap_is_rejected():
 g=setup()
 with pytest.raises(CoreDataError):g.finalize('run-1',json.dumps({'claims':[{'text':'bad','evidence':[],'evidence_gap':{}}]}))
