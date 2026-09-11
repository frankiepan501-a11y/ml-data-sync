import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi import FastAPI, HTTPException, Header
from fastapi.testclient import TestClient
from app.final_review_api import router


class ReviewAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_patch = patch("app.db.DB_PATH", str(Path(self.temp.name) / "db.sqlite"))
        self.db_patch.start()
        def auth(authorization: str = Header(default="")):
            if authorization != "Bearer offline-only":
                raise HTTPException(401)
        app = FastAPI()
        app.include_router(router(auth))
        self.client = TestClient(app)
        self.headers = {"Authorization": "Bearer offline-only"}

    def tearDown(self):
        self.client.close()
        self.db_patch.stop()
        self.temp.cleanup()

    def post(self, path, body):
        return self.client.post("/report/ml-final/test/" + path, json=body, headers=self.headers)

    def test_auth_required_and_no_production_staging_endpoint(self):
        self.assertEqual(self.client.get("/report/ml-final/test/status").status_code, 401)
        self.assertEqual(self.client.post("/report/ml-final/production/prepare", headers=self.headers).status_code, 404)

    def test_full_test_transport_is_restart_safe(self):
        sample = self.post("prepare", {"actor": "ou_test"}).json()
        repeat = self.post("prepare", {"actor": "ou_test"}).json()
        self.assertTrue(repeat["delivery_uncertain"])
        self.assertIsNone(repeat["nonce"])
        body = {"revision": sample["revision"], "nonce": sample["nonce"], "message_id": "om_test"}
        self.assertEqual(self.post("register", body).status_code, 200)
        result = self.post("decide", {**body, "actor": "ou_test"}).json()
        self.assertEqual(result["state"], "finance_pending")
        self.assertFalse(result["published"])
        self.assertTrue(self.post("decide", {**body, "actor": "ou_test"}).json()["duplicate"])
        again = self.post("prepare", {"actor": "ou_test"}).json()
        self.assertEqual(again["message_id"], "om_test")
        self.assertIsNone(again["nonce"])
        pending = self.client.get("/report/ml-final/test/feedback", headers=self.headers).json()
        self.assertEqual(len(pending), 1)
        self.assertEqual(self.post("feedback/om_test/ack", {}).status_code, 200)
        self.assertEqual(self.client.get("/report/ml-final/test/feedback", headers=self.headers).json(), [])
