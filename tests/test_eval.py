import json

import pytest

from core.schemas import TopicSpec
from eval.run_eval import (
    SYNTHETIC_BANNER, TOPICS_FILE, TopicResult, fake_llm, main, run_eval, summarize, to_markdown,
)
from tests.fakes import FfmpegFakeRunner


def test_topics_file_is_the_fixed_25():
    data = json.loads(TOPICS_FILE.read_text())
    topics = data["topics"]
    assert len(topics) == 25
    assert len({t["id"] for t in topics}) == 25
    for t in topics:
        TopicSpec.model_validate({k: t[k] for k in ("title", "objectives", "prerequisites")})
        assert t["course"] in data["course_titles"]


def test_refuses_to_run_without_a_key(env, monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("LLM_MODEL", "some-model")
    assert main(["--out", str(tmp_path / "res")]) == 2
    assert "Refusing to run" in capsys.readouterr().err
    assert not (tmp_path / "res").exists()


def test_refuses_without_model(env, monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-not-real")
    monkeypatch.delenv("LLM_MODEL", raising=False)
    assert main(["--out", str(tmp_path / "res")]) == 2


def test_summarize_math():
    rs = [
        TopicResult("a", "A", True, True, 1, 0, [], None, True, 60, 60.2, 10, 30, 1000, 500, 2),
        TopicResult("b", "B", True, False, 3, 2, ["hallucinated_api", "latex"], None, True, 60, 60, 20, 50, 3000, 900, 4),
        TopicResult("c", "C", False, False, 4, 3, ["latex"] * 4, "latex", None, 60, None, 40, 90, 5000, 2000, 5),
        TopicResult("d", "D", True, True, 1, 0, [], None, False, 60, 62, 10, 40, 1000, 400, 2),
    ]
    s = summarize(rs, {"k": 3})
    assert s["first_try_success_pct"] == 50.0
    assert s["success_after_repairs_pct"] == 75.0
    assert s["avg_attempts"] == 2.25
    assert s["avg_wall_seconds_per_video"] == 40.0
    assert s["error_classes_all_failed_attempts"] == {"latex": 5, "hallucinated_api": 1}
    assert s["error_classes_first_attempt"] == {"hallucinated_api": 1, "latex": 1}
    assert s["failed_topics"] == {"c": "latex"}
    assert s["duration_within_tolerance_pct_of_successes"] == pytest.approx(66.7)


@pytest.mark.ffmpeg
def test_harness_metrics_with_fake_llm(env, tmp_path):
    from pipeline.media import probe_duration
    topics = json.loads(TOPICS_FILE.read_text())["topics"][:4]
    s = run_eval(topics, {}, llm=fake_llm(), runner=FfmpegFakeRunner(), settings=env, k=2,
                 out_root=tmp_path, synthetic=True, probe=probe_duration)
    assert s["first_try_success_pct"] == 75.0  # fake LLM breaks every third codegen on purpose
    assert s["success_after_repairs_pct"] == 100.0
    assert s["avg_attempts"] == 1.25
    assert s["error_classes_first_attempt"] == {"hallucinated_api": 1}
    out = tmp_path / next(p.name for p in tmp_path.iterdir())
    assert out.name.startswith("SYNTHETIC-")
    md = (out / "SYNTHETIC-summary.md").read_text()
    assert SYNTHETIC_BANNER in md and "| First-try render success | 75.0% |" in md
    assert (out / "SYNTHETIC-results.csv").read_text().startswith(f"# {SYNTHETIC_BANNER}")


def test_markdown_has_no_banner_for_real_runs():
    s = summarize([TopicResult("a", "A", True, True, 1, 0)], {"timestamp": "t", "model": "m", "k": 3,
                                                              "quality": "l", "backend": "docker",
                                                              "with_narration": False, "machine": "x",
                                                              "synthetic": False})
    assert "SYNTHETIC" not in to_markdown(s)
