"""金标集回归：标注须始终通过校验，计分逻辑自评 100%。改结构化文本或枚举后必须仍然通过。"""

import sys
from dataclasses import fields
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import gold_eval  # noqa: E402

from app.models import Plan  # noqa: E402
from conftest import SNAPSHOT_HINT, snapshots_available  # noqa: E402


def test_gold_annotations_validate():
    if not snapshots_available():
        pytest.skip(SNAPSHOT_HINT)
    assert gold_eval.validate_all() == 0


def test_scoring_self_check():
    assert gold_eval.self_check() == 0


def test_plan_model_covers_gold_plan_keys():
    assert gold_eval.PLAN_KEYS <= {f.name for f in fields(Plan)}


def test_rules_meet_classification_target_on_dev_set():
    """回归门槛只用调试集；留出集只在 M1.15 运行（金标集规范第 2.4 节）。"""
    samples = gold_eval.load_gold("dev")
    preds = {s["id"]: gold_eval.predict(s) for s in samples}
    assert gold_eval.score(samples, preds)["classification"].value >= gold_eval.TARGETS["classification"]


def test_rules_meet_m1_targets_on_dev_set():
    """M1 验收指标（规则模式，D21）在调试集上作为回归门槛。"""
    samples = gold_eval.load_gold("dev")
    preds = {s["id"]: gold_eval.predict(s) for s in samples}
    overall = gold_eval.score(samples, preds)
    gated = gold_eval.score(samples, preds, plan_formats=gold_eval.RULE_MODE_FORMATS)
    assert overall["vendor"].value >= 0.90
    assert overall["coupon_precision"].value >= 0.95
    assert gated["price_period"].value >= 0.90
    assert gated["ram_disk"].value >= 0.85


def test_segmentation_covers_dev_set_plans():
    """分段召回门槛（调试集）：价格覆盖与配置覆盖 ≥ 95%，不产生多余块，形态判定与金标一致。"""
    samples = gold_eval.load_gold("dev")
    total = gold_eval.segment_coverage(samples)[0]["合计"]
    assert total["price"] / total["plans"] >= 0.95
    assert total["config"] / total["plans"] >= 0.95
    assert total["extra"] == 0
    for s in samples:
        if s["labels"]["record_kind"] == "offer":
            assert gold_eval.predict(s)["plan_format"] == s["labels"]["plan_format"], s["id"]


def test_cli_modes_run(capsys):
    """命令行各模式能跑通（--diff 等只打印的分支也要覆盖）。"""
    assert gold_eval.main(["--run", "--diff"]) in (0, 1)  # 规则层尚未抽套餐，未达标时返回 1
    assert gold_eval.main(["--segments", "--diff"]) == 0
    out = capsys.readouterr().out
    assert "不一致样本" in out and "合计" in out



def test_cli_llm_mode_with_fake_client(capsys, monkeypatch):
    """--run --mode llm 的接线：LLM 输出经校验合并后计分，编造的码被拒并列出；未配置时返回 2。"""
    import app.config
    import app.extract.llm as llm

    class FakeClient:
        def __init__(self, settings):
            pass

        def complete(self, messages):
            return llm.LlmResponse({"coupons": [{"code": "FAKE2026XYZ"}], "summary_zh": "测试摘要"}, {"total_tokens": 7})

        def close(self):
            pass

    real_load = app.config.load_settings
    env = {"LLM_BASE_URL": "https://llm.example.com/v1", "LLM_API_KEY": "k", "LLM_MODEL": "m"}
    monkeypatch.setattr(llm, "LlmClient", FakeClient)
    monkeypatch.setattr(app.config, "load_settings", lambda: real_load(env, env_file=None))
    assert gold_eval.main(["--run", "--mode", "llm", "--diff"]) in (0, 1)
    out = capsys.readouterr().out
    assert "LLM 模式" in out and "[拒绝]" in out and "FAKE2026XYZ" in out and "[摘要]" in out
    assert "token：total_tokens" in out

    monkeypatch.setattr(app.config, "load_settings", lambda: real_load({}, env_file=None))
    assert gold_eval.main(["--run", "--mode", "llm"]) == 2
