from agent.document_review import review_request


def test_only_default_review_requests_use_evidence_fields():
    batch = {'freeform_review': True, 'fields': [], 'request': '审阅附件'}
    for layout in ('default', 'table', 'custom'):
        messages, _ = review_request({**batch, 'review_layout': layout}, {'evidence': []})
        assert ('**依据摘录**' in messages[0]['content']) == (layout == 'default')
