"""Gold-standard API paths with no other test (#398).

The empty-collection session outcome, the model reply parser, regenerate's
409 GENERATION_IN_PROGRESS while a generation runs, and regenerate's 502
PAIR_GENERATION_FAILED when the model fails every attempt.

Runs with the API's dependencies, inside the verify project's API container:

    docker compose exec -T -e RAG_TEST_API_DIR=/app api python - < scripts/tests/test_goldstandard_api_paths.py

Each test uses its own temporary UPLOAD_DIR and an empty session cache, and
sends its requests in-process to the real app, on the test's own event loop,
without startup sweeps. Nothing reaches Weaviate or Ollama: the backend client
and the model call are test doubles.
"""
import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

sys.path.insert(0, os.environ.get("RAG_TEST_API_DIR") or str(Path(__file__).resolve().parents[2] / "api"))

import httpx

from config import settings
from main import app
from services import goldstandard as gs
from services import weaviate_client as wc

COLLECTION = "OwnedGoldPaths"
VALID_REPLY = '{"question": "Inert question?", "answer": "Inert answer.", "ground_truth": "Inert answer."}'


def chunk_objects(count):
    return [SimpleNamespace(uuid=UUID(int=i), properties={
        "content": f"Synthetic chunk {i}", "source_file": "inert.txt", "chunk_index": i,
    }) for i in range(1, count + 1)]


def backend(objects):
    """A Weaviate client whose collection holds exactly these objects."""
    collection = MagicMock()
    collection.iterator.side_effect = lambda **kwargs: iter(objects)
    collection.query.fetch_objects.return_value = SimpleNamespace(objects=objects)
    client = MagicMock()
    client.collections.exists.return_value = True
    client.collections.get.return_value = collection
    return client


async def no_model_call(system, user):
    raise AssertionError("the model was called")


class ApiCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        for target, name, value in ((settings, "upload_dir", self.temp.name),
                                    (gs, "_sessions", {}), (gs, "_tasks", set())):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                        base_url="http://test")

    async def asyncTearDown(self):
        for task in list(gs._tasks):
            task.cancel()
        await asyncio.gather(*list(gs._tasks), return_exceptions=True)
        await self.client.aclose()

    def use_backend(self, objects):
        for target, name, value in ((wc, "collection_exists", AsyncMock(return_value=True)),
                                    (wc, "get_client", MagicMock(return_value=backend(objects)))):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def use_model(self, chat):
        patcher = patch.object(gs.ollama, "chat", chat)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def settle(self):
        while gs._tasks:
            await asyncio.wait_for(asyncio.gather(*list(gs._tasks)), 10)

    async def session(self, session_id):
        response = await self.client.get(f"/goldstandard/session/{session_id}")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def saved(self, session_id):
        return Path(self.temp.name) / "goldstandard_sessions" / f"{session_id}.json"

    def assert_error(self, response, status, code):
        self.assertEqual(response.status_code, status, response.text)
        self.assertEqual(response.json()["error"]["code"], code, response.text)


class EmptyCollectionTests(ApiCase):
    async def test_empty_collection_settles_completed_with_no_pairs_and_no_errors(self):
        self.use_backend([])
        self.use_model(no_model_call)
        response = await self.client.post("/goldstandard/generate",
                                          json={"collection": COLLECTION, "sample_size": 5})
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["pairs_total"], 0)
        session_id = response.json()["session_id"]
        await self.settle()

        session = await self.session(session_id)
        self.assertEqual(session["status"], "completed")
        for field in ("pairs_total", "pairs_attempted", "pairs_completed", "pairs_failed"):
            self.assertEqual(session[field], 0, field)
        self.assertEqual(session["pairs"], [])
        self.assertEqual(session["errors"], [])

        stored = json.loads(self.saved(session_id).read_text())
        self.assertEqual(stored["status"], "completed")
        self.assertEqual(stored["pairs"], [])
        self.assertEqual(stored.get("errors", []), [])


class ParseReplyTests(unittest.TestCase):
    OBJECT = {"question": "Q?", "answer": "A.", "ground_truth": "A."}

    def test_code_fences_are_removed(self):
        body = json.dumps(self.OBJECT)
        for reply in (f"```json\n{body}\n```", f"```\n{body}\n```", f"```JSON{body}```"):
            with self.subTest(reply=reply):
                self.assertEqual(gs._parse_gs_json(reply), self.OBJECT)

    def test_a_brace_in_leading_prose_does_not_hide_the_object(self):
        body = json.dumps(self.OBJECT)
        for reply in (f"Return an object like {{ this }}. Here it is: {body}",
                      f"Here you go:\n```json\n{body}\n```"):
            with self.subTest(reply=reply):
                self.assertEqual(gs._parse_gs_json(reply), self.OBJECT)

    def test_trailing_text_after_the_object_is_ignored(self):
        body = json.dumps(self.OBJECT)
        for reply in (f"{body}\nI hope this helps!", f"{body}\n{{\"note\": \"second object\"}}",
                      f"```json\n{body}\n```\nLet me know if you need more."):
            with self.subTest(reply=reply):
                self.assertEqual(gs._parse_gs_json(reply), self.OBJECT)

    def test_a_reply_without_an_object_is_refused(self):
        for reply in ("", "I cannot answer that.", "[1, 2]", '"text"', "42", "null",
                      "```json\n[\"a\", \"b\"]\n```", "{not json} {also not json}", '{"unterminated": '):
            with self.subTest(reply=reply), self.assertRaises(ValueError):
                gs._parse_gs_json(reply)


class GenerationInProgressTests(ApiCase):
    async def test_regenerate_while_generating_returns_409_generation_in_progress(self):
        self.use_backend(chunk_objects(2))
        held, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def chat(system, user):
            calls.append(user)
            if len(calls) > 1:
                held.set()
                await release.wait()
            return VALID_REPLY

        self.use_model(chat)
        response = await self.client.post("/goldstandard/generate",
                                          json={"collection": COLLECTION, "sample_size": 2, "seed": 7})
        self.assertEqual(response.status_code, 202, response.text)
        session_id = response.json()["session_id"]
        try:
            # The first pair is published before the model is asked for the second.
            await asyncio.wait_for(held.wait(), 10)
            before = await self.session(session_id)
            self.assertEqual(before["status"], "generating")
            self.assertEqual(len(before["pairs"]), 1)
            pair = before["pairs"][0]

            response = await self.client.post("/goldstandard/regenerate",
                                              json={"session_id": session_id, "pair_id": pair["pair_id"]})
            self.assert_error(response, 409, "GENERATION_IN_PROGRESS")
            self.assertEqual(len(calls), 2, "regenerate called the model")
            self.assertEqual((await self.session(session_id))["pairs"], [pair])
        finally:
            release.set()
        await self.settle()

        after = await self.session(session_id)
        self.assertEqual(after["status"], "completed")
        self.assertEqual(len(after["pairs"]), 2)
        self.assertEqual(after["pairs"][0], pair)


class RegenerateFailureTests(ApiCase):
    SESSION_ID = "gs_398abcde"

    async def asyncSetUp(self):
        await super().asyncSetUp()
        pair = {"pair_id": "p_398a", "question": "Inert question?", "answer": "Inert answer.",
                "contexts": ["Synthetic chunk"], "ground_truth": "Inert answer.",
                "source_file": "inert.txt", "chunk_index": 0, "status": "approved"}
        gs.store_session({"session_id": self.SESSION_ID, "collection": COLLECTION,
                          "status": "completed", "pairs_total": 1, "pairs_attempted": 1,
                          "pairs_completed": 1, "pairs_failed": 0, "pairs": [pair]})

    async def test_model_failing_every_attempt_returns_502_and_leaves_the_pair(self):
        unusable = ["I cannot produce that.", "[1, 2]", '{"question": "Q?", "answer": ']

        async def unusable_replies(system, user):
            unusable.append(unusable.pop(0))
            return unusable[-1]

        async def transport_error(system, user):
            raise httpx.ConnectError("synthetic connection failure")

        before = await self.session(self.SESSION_ID)
        stored = self.saved(self.SESSION_ID).read_bytes()
        for name, chat in (("unusable replies", unusable_replies), ("transport error", transport_error)):
            with self.subTest(failure=name), patch.object(gs.ollama, "chat", chat), \
                 self.assertLogs(gs.log, "WARNING"):
                response = await self.client.post("/goldstandard/regenerate",
                                                  json={"session_id": self.SESSION_ID, "pair_id": "p_398a"})
                self.assert_error(response, 502, "PAIR_GENERATION_FAILED")
                self.assertIn("unchanged", response.json()["error"]["message"])
                self.assertEqual(await self.session(self.SESSION_ID), before)
                self.assertEqual(self.saved(self.SESSION_ID).read_bytes(), stored)


if __name__ == "__main__":
    unittest.main()
