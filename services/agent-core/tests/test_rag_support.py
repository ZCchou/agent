from agent.rag.support import assess_support


def test_structural_integrity_is_flagged_when_purpose_quote_only_covers_performance():
    result = assess_support('验证设备的性能、耐久性与结构完整性。', ['本试验验证性能标准要求，包括耐久性要求。'])
    assert result['status'] == 'needs_review'
    assert result['uncovered_concepts'] == ['结构完整性']


def test_verbatim_purpose_has_extract_support_and_paraphrases_stay_reviewable():
    assert assess_support('验证设备性能。', ['8.1 验证设备性能。'])['status'] == 'exact_excerpt'
    assert assess_support('核查安装条件下是否正常工作。', ['本试验验证设备的性能要求。'])['status'] == 'needs_review'
