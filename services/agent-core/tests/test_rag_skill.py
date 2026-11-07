from agent.skills import SkillRegistry


def test_skill_rag_requirement_is_explicit_and_survives_edit(tmp_path):
    root = tmp_path / "skill"
    root.mkdir()
    (root / "SKILL.md").write_text("---\nname: airworthiness\ndescription: 条款问答\nrag:\n  required: true\n---\n引用原文。", encoding="utf-8")
    registry = SkillRegistry(tmp_path / "data" / "skills.json")
    registry.add(str(root))
    assert registry.requires_rag("$airworthiness 查找条款")
    assert not registry.requires_rag("普通问题")
    item = registry.load("airworthiness")
    assert item["rag_required"]
    registry.save_skill("airworthiness", "条款问答", "继续引用原文。", expected_revision=item["revision"])
    assert registry.load("airworthiness")["rag_required"]
