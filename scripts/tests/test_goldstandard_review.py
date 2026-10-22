"""Gold-standard review errors: unknown sessions, unknown pairs, empty edits (#381).

Runs with the API's dependencies, inside the verify project's API container:

    docker compose exec -T -e RAG_TEST_API_DIR=/app api python - < scripts/tests/test_goldstandard_review.py

Each test uses its own temporary UPLOAD_DIR, a synthetic session and in-process
HTTP without startup sweeps. Nothing reaches Weaviate or Ollama: the model call
behind regenerate fails the test if it is reached.
"""
import copy
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.environ.get("RAG_TEST_API_DIR") or str(Path(__file__).resolve().parents[2] / "api"))

from fastapi.testclient import TestClient

from config import settings
from main import app
from services import goldstandard as gs

SID = "gs_381abcde"
MISSING_SID = "gs_00000000"


def session():
    pair = {"pair_id": "p0", "question": "Inert question", "answer": "Inert answer",
            "contexts": ["Inert context"], "ground_truth": "Inert truth",
            "source_file": "inert.txt", "chunk_index": 0, "status": "pending"}
    return {"session_id": SID, "collection": "Owned", "status": "completed",
            "pairs_total": 1, "pairs_completed": 1, "pairs": [pair]}


async def no_model_call(chunk):
    raise AssertionError("regenerate reached the model")


class ReviewErrorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        upload = patch.object(settings, "upload_dir", self.temp.name)
        upload.start()
        self.addCleanup(upload.stop)
        original = gs._sessions
        gs._sessions = {}
        self.addCleanup(setattr, gs, "_sessions", original)
        model = patch.object(gs, "_generate_pair", no_model_call)
        model.start()
        self.addCleanup(model.stop)
        gs.store_session(session())
        self.sessions = Path(self.temp.name) / "goldstandard_sessions"
        self.client = TestClient(app)        # no startup sweeps

    def state(self):
        """The session store on disk and in the cache."""
        files = {p.name: p.read_bytes() for p in sorted(self.sessions.iterdir()) if p.is_file()}
        return files, copy.deepcopy(gs._sessions)

    def patch_pair(self, sid, pid, body):
        return self.client.patch(f"/goldstandard/session/{sid}/pair/{pid}", json=body)

    def regenerate(self, sid, pid):
        return self.client.post("/goldstandard/regenerate", json={"session_id": sid, "pair_id": pid})

    def assert_error(self, response, status, code, *names):
        self.assertEqual(response.status_code, status, response.text)
        error = response.json()["error"]
        self.assertEqual(error["code"], code)
        for name in names:
            self.assertIn(name, error["message"])

    def test_patch_unknown_session_is_session_not_found(self):
        before = self.state()
        response = self.patch_pair(MISSING_SID, "p_1", {"status": "approved"})
        self.assert_error(response, 404, "SESSION_NOT_FOUND", MISSING_SID)
        self.assertEqual(response.json()["error"]["message"], f"Session '{MISSING_SID}' not found.")
        self.assertEqual(self.state(), before)

    def test_patch_unknown_pair_is_pair_not_found(self):
        before = self.state()
        response = self.patch_pair(SID, "p_1", {"status": "approved"})
        self.assert_error(response, 404, "PAIR_NOT_FOUND", "p_1", SID)
        self.assertEqual(self.state(), before)

    def test_regenerate_unknown_session_is_session_not_found(self):
        before = self.state()
        response = self.regenerate(MISSING_SID, "p_1")
        self.assert_error(response, 404, "SESSION_NOT_FOUND", MISSING_SID)
        self.assertEqual(response.json()["error"]["message"], f"Session '{MISSING_SID}' not found.")
        self.assertEqual(self.state(), before)

    def test_regenerate_unknown_pair_is_pair_not_found(self):
        before = self.state()
        response = self.regenerate(SID, "p_1")
        self.assert_error(response, 404, "PAIR_NOT_FOUND", "p_1", SID)
        self.assertEqual(self.state(), before)

    def test_patch_refuses_empty_and_whitespace_content(self):
        before = self.state()
        for field in ("question", "answer", "ground_truth"):
            for value in ("", "   ", "\n\t"):
                with self.subTest(field=field, value=value):
                    response = self.patch_pair(SID, "p0", {"status": "edited", field: value})
                    self.assert_error(response, 422, "INVALID_PARAMETER")
                    self.assertIn(field, response.text)
                    self.assertEqual(self.state(), before)

    def test_empty_content_is_refused_before_the_session_lookup(self):
        response = self.patch_pair(MISSING_SID, "p_1", {"status": "edited", "answer": ""})
        self.assert_error(response, 422, "INVALID_PARAMETER")

    def test_valid_edits_are_still_accepted(self):
        response = self.patch_pair(SID, "p0", {"status": "edited", "question": "  Spaced question  "})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["question"], "  Spaced question  ")
        self.assertEqual(gs.get_session(SID)["pairs"][0]["question"], "  Spaced question  ")
        response = self.patch_pair(SID, "p0", {"status": "approved"})
        self.assertEqual(response.status_code, 200, response.text)
        response = self.patch_pair(SID, "p0", {"status": "rejected", "answer": None})
        self.assertEqual(response.status_code, 200, response.text)
        pair = gs.get_session(SID)["pairs"][0]
        self.assertEqual((pair["status"], pair["answer"]), ("rejected", "Inert answer"))


if __name__ == "__main__":
    unittest.main(argv=["test_goldstandard_review"], verbosity=2)
