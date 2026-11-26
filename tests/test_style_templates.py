"""C2 剧本风格模板库：加载回退、建议镜头数、prompt 注入（纯 CPU）。"""
from __future__ import annotations

from api import style_templates as st
from api.router import build_script_prompts


def test_file_templates_have_required_fields():
    tpls = st.load_templates()
    assert len(tpls) >= 6
    ids = [t["id"] for t in tpls]
    assert len(ids) == len(set(ids)), "风格 id 不能重复"
    for t in tpls:
        assert t["name"], t
        assert t["prompt_hint"], t
        assert t["shot_seconds"] > 0, t


def test_missing_file_falls_back(monkeypatch, tmp_path):
    monkeypatch.setattr(st, "_FILE", tmp_path / "nope.json")
    assert st.load_templates() == st._builtin()


def test_broken_file_falls_back(monkeypatch, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(st, "_FILE", bad)
    assert st.load_templates() == st._builtin()


def test_unknown_template_returns_default():
    assert st.get_template("no-such")["id"] == st.DEFAULT_STYLE_ID
    assert st.get_template(None)["id"] == st.DEFAULT_STYLE_ID
    assert st.get_template("")["id"] == st.DEFAULT_STYLE_ID
    assert st.get_template("cyberpunk")["id"] == "cyberpunk"


def test_suggest_shot_count_table():
    assert st.suggest_shot_count(15, 6) == 3
    assert st.suggest_shot_count(30, 6) == 5
    assert st.suggest_shot_count(60, 6) == 10
    assert st.suggest_shot_count(90, 6) == 15


def test_suggest_shot_count_invalid_keeps_none():
    assert st.suggest_shot_count(0, 6) is None
    assert st.suggest_shot_count(-5, 6) is None
    assert st.suggest_shot_count("abc", 6) is None
    assert st.suggest_shot_count(30, 0) is None
    assert st.suggest_shot_count(None, None) is None


def test_style_prompt_injected_into_system():
    tpl = st.get_template("cyberpunk")
    system, user = build_script_prompts("雨夜决定", 5, "都市青年", tpl)
    assert tpl["prompt_hint"][:20] in system
    assert tpl["name"] in user
    assert tpl["description"] in user


def test_unknown_style_still_builds_prompt():
    tpl = st.get_template("nope")
    system, user = build_script_prompts("主题", 4, "", tpl)
    assert tpl["prompt_hint"][:20] in system
    assert "主题" in user
