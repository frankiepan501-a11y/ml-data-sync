import copy
import os
import unittest

os.environ.setdefault("N8N_BASE_URL", "https://n8n.invalid/api/v1")
os.environ.setdefault("N8N_API_KEY", "test-key")
os.environ.setdefault("ML_SYNC_SERVICE_AUTH_TOKEN", "test-service-token")

from ops import repair_ml_monthly_workflows as repair
from ops import update_ml_close_workflows as legacy_update


def _node(name, type_, parameters):
    return {"id": name.lower().replace(" ", "-"), "name": name, "type": type_, "parameters": parameters, "position": [0, 0], "typeVersion": 1}


class MonthlyWorkflowRepairTests(unittest.TestCase):
    def test_monthly_workflow_uses_complete_month_and_fails_closed(self):
        workflow = {
            "id": repair.MONTHLY_WORKFLOW_ID,
            "name": "ML monthly aggregate · 每月 1 号 02:00 BJ 跑上月 5 sellers",
            "active": True,
            "settings": {"executionOrder": "v1"},
            "nodes": [
                _node("Monthly 02:00 BJ", "n8n-nodes-base.scheduleTrigger", {"rule": {"interval": [{"field": "cronExpression", "expression": "0 2 1 * *"}]}}),
                _node("Prepare sellers + month", "n8n-nodes-base.code", {"jsCode": "old prepare"}),
                _node("Backfill 本土店 (own-token)", "n8n-nodes-base.code", {"jsCode": "const tok = 'Bearer secret-value';\nold backfill"}),
                _node("POST /report/sync-feishu-monthly", "n8n-nodes-base.httpRequest", {"method": "POST", "url": "old", "options": {"timeout": 150000, "response": {"response": {"neverError": True}}}}),
                _node("POST /report/sync-meitong-cost", "n8n-nodes-base.httpRequest", {"method": "POST", "url": "cost", "options": {"timeout": 180000, "response": {"response": {"neverError": True}}}}),
                _node("Untouched", "n8n-nodes-base.noOp", {"value": 1}),
            ],
            "connections": {"A": {"main": [[{"node": "B", "type": "main", "index": 0}]]}},
        }
        untouched_before = copy.deepcopy(workflow["nodes"][-1])
        connections_before = copy.deepcopy(workflow["connections"])

        patched = repair.patch_monthly_workflow(workflow)
        nodes = {node["name"]: node for node in patched["nodes"]}

        self.assertEqual(nodes["Monthly 02:00 BJ"]["parameters"]["rule"]["interval"][0]["expression"], "0 2 2 * *")
        backfill_code = nodes["Backfill 本土店 (own-token)"]["parameters"]["jsCode"]
        self.assertIn("month=${month}", backfill_code)
        self.assertIn("refresh_after=${refreshAfter}", backfill_code)
        self.assertIn("Date.now() / 1000) - 600", backfill_code)
        self.assertIn("new_fetches === 0", backfill_code)
        self.assertIn("capped === false", backfill_code)
        self.assertIn("response.cached_month_unique === response.platform_total", backfill_code)
        self.assertIn("response.window_orders === response.platform_total", backfill_code)
        self.assertIn("return sellers.map(seller_id", backfill_code)
        self.assertIn("seller_id,", backfill_code)
        self.assertIn("const sellers = [2378517428, 3383185411]", backfill_code)
        prepare_code = nodes["Prepare sellers + month"]["parameters"]["jsCode"]
        self.assertIn("const sellers = [2378517428, 3383185411]", prepare_code)
        self.assertNotIn("1502236229", prepare_code)
        self.assertNotIn("1407362838", prepare_code)
        self.assertNotIn("recent_n=60", backfill_code)
        self.assertNotIn("catch (e) {}", backfill_code)
        self.assertIn("secret-value", backfill_code)
        sync = nodes["POST /report/sync-feishu-monthly"]["parameters"]
        self.assertIn("commit=true", sync["url"])
        self.assertNotIn("neverError", str(sync["options"]))
        self.assertNotIn("neverError", str(nodes["POST /report/sync-meitong-cost"]["parameters"]["options"]))
        self.assertEqual(patched["connections"], connections_before)
        self.assertEqual(nodes["Untouched"], untouched_before)

    def test_cbt_workflow_validates_explicit_month_without_auto_commit(self):
        schedule = _node("Schedule Trigger", "n8n-nodes-base.scheduleTrigger", {"rule": {"interval": [{"field": "cronExpression", "expression": "0 0 7-11 * *"}]}})
        ingest = _node("CBT ingest", "n8n-nodes-base.httpRequest", {"method": "POST", "url": "https://ml-sync.zeabur.app/report/cbt-ingest?commit=true", "options": {"timeout": 300000}})
        next_card = _node("Build next card request", "n8n-nodes-base.code", {"jsCode": "unchanged"})
        send = _node("Send next card", "n8n-nodes-base.httpRequest", {"method": "POST", "url": "={{$json.url}}"})
        workflow = {
            "id": repair.CBT_WORKFLOW_ID,
            "name": "美客多CBT官方导出解析(7-11号)",
            "active": True,
            "settings": {},
            "nodes": [schedule, ingest, next_card, send],
            "connections": {
                "Schedule Trigger": {"main": [[{"node": "CBT ingest", "type": "main", "index": 0}]]},
                "CBT ingest": {"main": [[{"node": "Build next card request", "type": "main", "index": 0}]]},
                "Build next card request": {"main": [[{"node": "Send next card", "type": "main", "index": 0}]]},
            },
        }

        patched = repair.patch_cbt_workflow(workflow)
        nodes = {node["name"]: node for node in patched["nodes"]}

        self.assertIn("Build target month", nodes)
        self.assertEqual(nodes["CBT ingest"]["parameters"]["url"], "=https://ml-sync.zeabur.app/report/cbt-ingest?month={{$json.month}}&commit=false")
        self.assertEqual(patched["connections"]["Schedule Trigger"]["main"][0][0]["node"], "Build target month")
        self.assertEqual(patched["connections"]["Build target month"]["main"][0][0]["node"], "CBT ingest")
        self.assertNotIn("Build next card request", nodes)
        self.assertNotIn("Send next card", nodes)
        self.assertNotIn("CBT ingest", patched["connections"])

    def test_safe_hash_ignores_server_owned_metadata(self):
        workflow = {
            "id": repair.MONTHLY_WORKFLOW_ID,
            "name": "name",
            "active": True,
            "versionId": "before",
            "updatedAt": "2026-09-07T00:00:00Z",
            "settings": {},
            "nodes": [],
            "connections": {},
        }
        changed = copy.deepcopy(workflow)
        changed["versionId"] = "after"
        changed["updatedAt"] = "2026-09-07T00:01:00Z"
        changed["serverOnly"] = {"value": 1}

        self.assertEqual(repair._safe_hash(workflow), repair._safe_hash(changed))

    def test_legacy_updater_fails_closed_when_reactivation_fails(self):
        workflow = {
            "id": "wf-1",
            "name": "safe",
            "active": True,
            "versionId": "v1",
            "settings": {},
            "nodes": [],
            "connections": {},
        }
        with unittest.mock.patch.object(
            legacy_update,
            "req",
            side_effect=[workflow, {**workflow, "active": False, "versionId": "v2"}, RuntimeError("activate failed")],
        ):
            with self.assertRaisesRegex(RuntimeError, "activate failed"):
                legacy_update.put_wf(workflow, [], {})


if __name__ == "__main__":
    unittest.main()
