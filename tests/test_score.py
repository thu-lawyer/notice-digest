"""score / store / feedback 单元测试（unittest 风格，纯标准库）。"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from notice_digest import score as score_mod
from notice_digest.config import Config, load_config, simple_yaml_load
from notice_digest.feedback import make_token, verify_token
from notice_digest.store import Store, parse_published_at
from notice_digest.timeparse import parse_item_time

TZ = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 8, 9, 0, tzinfo=TZ)


def sample_item(**over):
    item = {
        "id": "weixinzs_467875816:12237642",
        "source_id": "weixinzs_467875816",
        "source_name": "清华大学法学院通知",
        "title": "关于举办人工智能与法治前沿讲座的通知",
        "published_at": "2026-10-08T07:30:00+08:00",
        "url": "https://mp.weixin.qq.com/s?__biz=abc",
        "category": "讲座活动",
        "intent_group": "activity",
        "english": {"title": "AI and Law Lecture", "source_name": "THU Law"},
    }
    item.update(over)
    return item


class TestConfig(unittest.TestCase):
    def test_defaults_when_profile_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = load_config(
                profile_path=Path(tmp) / "nope.yaml", env_path=Path(tmp) / "nope.env"
            )
        self.assertEqual(cfg.campus, "thu")
        self.assertEqual(cfg.send_at, "07:30")
        self.assertEqual(cfg.top_n, 20)
        self.assertIn("kw:讲座", cfg.weights_prior)
        self.assertTrue(cfg.sections)

    def test_profile_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp) / "profile.yaml"
            profile.write_text(
                "campus: ruc\n"
                "top_n: 30\n"
                "to_addr: someone@example.com\n"
                "smtp:\n"
                "  host: smtp.example.com\n"
                "  port: 587\n"
                "keywords_mute:\n"
                "  - 招聘\n"
                "  - 广告\n",
                encoding="utf-8",
            )
            cfg = load_config(profile_path=profile, env_path=Path(tmp) / "nope.env")
        self.assertEqual(cfg.campus, "ruc")
        self.assertEqual(cfg.top_n, 30)
        self.assertEqual(cfg.smtp_host, "smtp.example.com")
        self.assertEqual(cfg.smtp_port, 587)
        self.assertEqual(cfg.keywords_mute, ["招聘", "广告"])

    def test_env_file_and_getenv_priority(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text("ND_HMAC_SECRET=from-file\nND_SMTP_HOST=smtp.file\n", encoding="utf-8")
            cfg = load_config(profile_path=Path(tmp) / "nope.yaml", env_path=env)
            self.assertEqual(cfg.hmac_secret, "from-file")
            self.assertEqual(cfg.smtp_host, "smtp.file")

    def test_simple_yaml_subset(self):
        parsed = simple_yaml_load("a: 1\nb:\n  c: text\nd:\n  - x\n  - y\n")
        self.assertEqual(parsed["a"], 1)
        self.assertEqual(parsed["b"]["c"], "text")
        self.assertEqual(parsed["d"], ["x", "y"])

    def test_clamp_top_n(self):
        cfg = Config(top_n=0)
        self.assertEqual(cfg.clamp_top_n(), 1)
        self.assertEqual(cfg.clamp_top_n(999), 200)
        self.assertEqual(cfg.clamp_top_n(15), 15)


class TestStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "t.db")
        self.store.init_schema()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_published_at_shanghai(self):
        ts = parse_published_at("2026-10-08T07:30:00+08:00")
        naive = parse_published_at("2026-10-08 07:30:00")
        self.assertIsNotNone(ts)
        self.assertEqual(ts, naive)
        self.assertIsNone(parse_published_at(None))
        self.assertIsNone(parse_published_at("not a date"))

    def test_upsert_idempotent(self):
        first = self.store.upsert_items([sample_item(), sample_item(id="b")])
        self.assertEqual(first, (2, 0))
        second = self.store.upsert_items([sample_item(), sample_item(id="b")])
        self.assertEqual(second, (0, 2))
        self.assertEqual(len(self.store.known_ids()), 2)

    def test_get_item_roundtrip(self):
        self.store.upsert_items([sample_item()])
        item = self.store.get_item("weixinzs_467875816:12237642")
        self.assertIsNotNone(item)
        self.assertEqual(item["title"], sample_item()["title"])
        self.assertEqual(item["english"]["title"], "AI and Law Lecture")
        self.assertIsNone(self.store.get_item("missing"))

    def test_update_detail_and_pending(self):
        self.store.upsert_items(
            [
                sample_item(),
                sample_item(id="info-1", category="校园服务", intent_group="information"),
            ]
        )
        self.assertEqual(len(self.store.pending_enrich(10)), 1)

        self.store.update_detail("weixinzs_467875816:12237642", {"detail_status": "complete"})
        self.assertEqual(self.store.pending_enrich(10), [])

        item = self.store.get_item("weixinzs_467875816:12237642")
        self.assertEqual(item["detail_status"], "complete")
        self.assertEqual(item["detail"], {"detail_status": "complete"})

    def test_needs_enrich_rules(self):
        self.store.upsert_items(
            [
                sample_item(id="a", category="讲座活动", intent_group="information"),
                sample_item(id="b", category="校园服务", intent_group="activity"),
                sample_item(id="c", category="校园服务", intent_group="information"),
            ]
        )
        pending = {i["id"] for i in self.store.pending_enrich(10)}
        self.assertEqual(pending, {"a", "b"})

    def test_weights_roundtrip(self):
        self.assertEqual(self.store.get_weights(), {})
        self.store.put_weights({"cat:讲座活动": 1.5, "kw:AI": 0.75})
        w = self.store.get_weights()
        self.assertAlmostEqual(w["cat:讲座活动"], 1.5)
        self.store.put_weights({"kw:AI": -2.0})
        self.assertAlmostEqual(self.store.get_weights()["kw:AI"], -2.0)

    def test_feedback_idempotent(self):
        self.assertTrue(self.store.record_feedback("a", "up", 1.0, NOW.isoformat()))
        self.assertFalse(self.store.record_feedback("a", "up", 1.0, NOW.isoformat()))
        self.assertTrue(self.store.record_feedback("a", "click", 0.6, NOW.isoformat()))
        self.assertEqual(len(self.store.recent_feedback(10)), 2)

    def test_record_send(self):
        self.store.record_send("2026-10-08", "主题", 12)
        self.store.record_send("2026-10-08", "主题2", 13)
        last = self.store.last_send()
        self.assertEqual(last["n_items"], 13)

    def test_stats(self):
        self.store.upsert_items([sample_item()])
        stats = self.store.stats()
        self.assertEqual(stats["total_items"], 1)
        self.assertIn("讲座活动", stats["by_category"])


class TestScore(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()

    def test_features_onehot(self):
        feats = score_mod.features_of(sample_item(), self.cfg, NOW)
        self.assertEqual(feats.get("cat:讲座活动"), 1.0)
        self.assertEqual(feats.get("src:清华大学法学院通知"), 1.0)
        self.assertEqual(feats.get("kw:AI"), 1.0)  # 来自 english.title
        self.assertEqual(feats.get("kw:人工智能"), 1.0)
        self.assertEqual(feats.get("kw:法治"), 1.0)
        self.assertEqual(feats.get("kw:讲座"), 1.0)
        self.assertEqual(feats.get("kw:法学"), None)  # 未出现即为 0，不产生特征

    def test_mute_feature(self):
        feats = score_mod.features_of(sample_item(title="某公司招聘宣讲会"), self.cfg, NOW)
        self.assertEqual(feats.get("mute:招聘"), 1.0)

    def test_time_features(self):
        parsed = parse_item_time("标题", "今天 19:00", "", "", NOW)
        feats = score_mod.features_of(sample_item(), self.cfg, NOW, parsed)
        self.assertEqual(feats.get("time:today"), 1.0)

    def test_deadline_feature(self):
        parsed = parse_item_time("标题", "报名截止10月8日17时", "", "", NOW)
        feats = score_mod.features_of(sample_item(), self.cfg, NOW, parsed)
        self.assertEqual(feats.get("dl:today"), 1.0)

    def test_score_items_sorted_and_explainable(self):
        items = [
            sample_item(id="low", title="食堂开放时间调整", category="校园服务"),
            sample_item(id="high", title="法学院人工智能与法治讲座，报名截止10月8日"),
        ]
        scored = score_mod.score_items(items, {}, self.cfg, NOW)
        self.assertEqual([s.item_id for s in scored], ["high", "low"])
        self.assertGreater(scored[0].score, scored[1].score)
        for s in scored:
            for name, value in s.reasons:
                self.assertIsInstance(name, str)
                self.assertIsInstance(value, float)
            for line in s.reason_strings:
                self.assertIn(", ", line)

    def test_explain_text(self):
        scored = score_mod.score_items([sample_item()], {}, self.cfg, NOW)[0]
        text = score_mod.explain(scored)
        self.assertIn("总分", text)
        self.assertIn("cat:讲座活动", text)

    def test_learn_direction(self):
        base = {"cat:讲座活动": 1.0}
        up = score_mod.learn(base, {"cat:讲座活动": 1.0}, 1.0)
        down = score_mod.learn(base, {"cat:讲座活动": 1.0}, 0.0)
        self.assertGreater(up["cat:讲座活动"], base["cat:讲座活动"])
        self.assertLess(down["cat:讲座活动"], base["cat:讲座活动"])
        # 原 dict 不被原地修改
        self.assertEqual(base["cat:讲座活动"], 1.0)

    def test_learn_only_touches_hit_features(self):
        base = {"cat:讲座活动": 1.0, "kw:AI": 0.5}
        out = score_mod.learn(base, {"cat:讲座活动": 1.0}, 1.0)
        self.assertEqual(out["kw:AI"], 0.5)

    def test_learn_bounded(self):
        w = {"f": 0.0}
        for _ in range(2000):
            w = score_mod.learn(w, {"f": 1.0}, 1.0, lr=1.0)
        self.assertLessEqual(w["f"], score_mod.W_MAX)
        for _ in range(2000):
            w = score_mod.learn(w, {"f": 1.0}, 0.0, lr=1.0)
        self.assertGreaterEqual(w["f"], score_mod.W_MIN)

    def test_learn_empty_features(self):
        self.assertEqual(score_mod.learn({"a": 1.0}, {}, 1.0), {"a": 1.0})

    def test_decay(self):
        out = score_mod.decay({"a": 1.0, "b": -1.0}, 0.995)
        self.assertAlmostEqual(out["a"], 0.995)
        self.assertAlmostEqual(out["b"], -0.995)
        blended = score_mod.decay({"a": 1.0}, 0.5)
        self.assertAlmostEqual(blended["a"], 0.5)

    def test_sigmoid(self):
        self.assertAlmostEqual(score_mod.sigmoid(0.0), 0.5)
        self.assertGreater(score_mod.sigmoid(10), 0.99)
        self.assertLess(score_mod.sigmoid(-10), 0.01)

    def test_merged_weights_stored_overrides_prior(self):
        merged = score_mod.merged_weights(self.cfg, {"cat:讲座活动": -3.0})
        self.assertAlmostEqual(merged["cat:讲座活动"], -3.0)

    def test_merged_weights_clips_extremes(self):
        merged = score_mod.merged_weights(self.cfg, {"x": 99.0})
        self.assertEqual(merged["x"], score_mod.W_MAX)


class TestFeedbackTokens(unittest.TestCase):
    def setUp(self):
        self.cfg = Config(hmac_secret="unit-test-secret")

    def test_token_roundtrip(self):
        token = make_token("item:1", "up", self.cfg)
        self.assertTrue(verify_token(token, "item:1", "up", self.cfg))

    def test_token_rejects_wrong_kind_or_id(self):
        token = make_token("item:1", "up", self.cfg)
        self.assertFalse(verify_token(token, "item:1", "down", self.cfg))
        self.assertFalse(verify_token(token, "item:2", "up", self.cfg))
        self.assertFalse(verify_token("", "item:1", "up", self.cfg))
        self.assertFalse(verify_token("deadbeef", "item:1", "up", self.cfg))

    def test_token_rejects_other_secret(self):
        token = make_token("item:1", "up", self.cfg)
        other = Config(hmac_secret="different")
        self.assertFalse(verify_token(token, "item:1", "up", other))

    def test_missing_secret_raises(self):
        with self.assertRaises(ValueError):
            make_token("item:1", "up", Config(hmac_secret=""))
        self.assertFalse(verify_token("x", "item:1", "up", Config(hmac_secret="")))

    def test_exp_bound_token(self):
        token = make_token("item:1", "click", self.cfg, exp=1234567890)
        self.assertTrue(verify_token(token, "item:1", "click", self.cfg, exp=1234567890))
        self.assertFalse(verify_token(token, "item:1", "click", self.cfg, exp=9999999999))

    def test_no_secret_in_token(self):
        token = make_token("item:1", "up", self.cfg)
        self.assertNotIn("unit-test-secret", token)
        self.assertEqual(len(token), 32)


class TestFeedbackServiceWiring(unittest.TestCase):
    """校验服务绑定与鉴权逻辑（不真正监听端口）。"""

    def test_serve_rejects_non_loopback(self):
        from notice_digest import feedback as fb

        cfg = Config(hmac_secret="s3cret")
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "t.db")
            store.init_schema()
            with self.assertRaises(ValueError):
                fb.serve(cfg, store, host="0.0.0.0", port=18791)
            store.close()

    def test_serve_requires_secret(self):
        from notice_digest import feedback as fb

        cfg = Config(hmac_secret="")
        os.environ.pop("ND_HMAC_SECRET", None)
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "t.db")
            store.init_schema()
            with self.assertRaises(ValueError):
                fb.serve(cfg, store, port=18792)
            store.close()

    def test_record_and_learn(self):
        from notice_digest import feedback as fb

        cfg = Config(hmac_secret="s3cret")
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "t.db")
            store.init_schema()
            store.upsert_items([sample_item()])
            sid = sample_item()["id"]
            first = fb.record_and_learn(store, cfg, sid, "up", NOW)
            self.assertTrue(first["recorded"])
            self.assertTrue(first["learned"])
            again = fb.record_and_learn(store, cfg, sid, "up", NOW)
            self.assertFalse(again["recorded"])
            self.assertFalse(again["learned"])
            store.close()

    def test_daily_decay_once(self):
        from notice_digest import feedback as fb

        cfg = Config(hmac_secret="s3cret")
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "t.db")
            store.init_schema()
            store.put_weights({"cat:讲座活动": 1.0})
            self.assertTrue(fb.maybe_daily_decay(store, NOW, 0.5))
            self.assertAlmostEqual(store.get_weights()["cat:讲座活动"], 0.5)
            self.assertFalse(fb.maybe_daily_decay(store, NOW, 0.5))
            self.assertAlmostEqual(store.get_weights()["cat:讲座活动"], 0.5)
            store.close()


if __name__ == "__main__":
    unittest.main()
