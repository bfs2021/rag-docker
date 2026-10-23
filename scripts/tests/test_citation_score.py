"""A citation's `score` is higher-is-better in every retrieval mode (issue #376).

Runs with the API's dependencies, in the built API image with no network
(`03_query.sh`):

    docker run --rm --network none -v "$PWD:/repo:ro" -w /repo -e RAG_TEST_API_DIR=/repo/api \\
      "$(docker compose images -q api)" python scripts/tests/test_citation_score.py

The Weaviate client and Ollama are mocked; nothing reaches a backend.
`scripts/verify/citation_score.py` checks the same conversions on a real Weaviate.
"""
import logging
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.environ.get("RAG_TEST_API_DIR") or str(Path(__file__).resolve().parents[2] / "api"))

from fastapi.testclient import TestClient
from weaviate.classes.config import VectorDistances

from main import app
from models.schemas import Citation
from services import metrics, rag_pipeline
from services import weaviate_client as wc

ROOT = Path(__file__).resolve().parents[2]
DISTANCE_QUERIES = (
    ("near_vector", lambda: wc._near_vector_query_sync("Coll", [0.1, 0.2], 5)),
    ("near_text", lambda: wc._near_text_query_sync("Coll", "overtime", 5)),
)


def obj(index, distance=None, score=None):
    return SimpleNamespace(
        properties={"content": f"chunk {index}", "source_file": "a.txt", "chunk_index": index},
        metadata=SimpleNamespace(distance=distance, score=score))


def client_for(metric, objects, hybrid=None):
    """A mocked client whose collection has `metric` (a VectorDistances, or a
    vector_index_config object when not a VectorDistances) and returns `objects`."""
    coll = MagicMock()
    config = (SimpleNamespace(distance_metric=metric) if isinstance(metric, VectorDistances)
              else metric)
    coll.config.get.return_value = SimpleNamespace(vector_index_config=config)
    coll.query.near_vector.return_value = SimpleNamespace(objects=objects)
    coll.query.near_text.return_value = SimpleNamespace(objects=objects)
    coll.query.hybrid.return_value = SimpleNamespace(objects=hybrid or [])
    client = MagicMock()
    client.collections.get.return_value = coll
    return client


def scores(client, query):
    with patch.object(wc, "get_client", return_value=client):
        return [row["score"] for row in query()]


class DistanceModeTests(unittest.TestCase):
    """`hnsw`, `flat` (near_vector) and `semantic` (near_text)."""

    def check(self, metric, distances, expected):
        for name, query in DISTANCE_QUERIES:
            with self.subTest(query=name, metric=metric):
                got = scores(client_for(metric, [obj(i, d) for i, d in enumerate(distances)]), query)
                self.assertEqual(len(got), len(expected))
                for g, e in zip(got, expected):
                    self.assertAlmostEqual(g, e, places=9)
                # Weaviate returns the best match first; its score is now the highest.
                self.assertEqual(got, sorted(got, reverse=True))

    def test_cosine_is_one_minus_distance(self):
        self.check(VectorDistances.COSINE, [0.0, 0.09, 0.5, 1.2, 2.0], [1.0, 0.91, 0.5, -0.2, -1.0])

    def test_dot_is_the_dot_product(self):
        self.check(VectorDistances.DOT, [-12.5, -3.0, 0.0, 4.0], [12.5, 3.0, 0.0, -4.0])

    def test_l2_squared_is_one_over_one_plus_distance(self):
        self.check(VectorDistances.L2_SQUARED, [0.0, 0.25, 1.0, 3.0], [1.0, 0.8, 0.5, 0.25])

    def test_missing_distance_is_null_not_a_perfect_match(self):
        for metric in (VectorDistances.COSINE, VectorDistances.DOT, VectorDistances.L2_SQUARED):
            for name, query in DISTANCE_QUERIES:
                with self.subTest(query=name, metric=metric):
                    got = scores(client_for(metric, [obj(0, 0.2), obj(1, None)]), query)
                    self.assertIsNotNone(got[0])
                    self.assertIsNone(got[1])

    def test_distance_zero_is_a_real_value(self):
        got = scores(client_for(VectorDistances.COSINE, [obj(0, 0.0)]), DISTANCE_QUERIES[0][1])
        self.assertEqual(got, [1.0])

    def test_unknown_or_unreadable_metric_gives_null(self):
        for config in (None, SimpleNamespace(), SimpleNamespace(distance_metric="hamming")):
            for name, query in DISTANCE_QUERIES:
                with self.subTest(query=name, config=config):
                    self.assertEqual(scores(client_for(config, [obj(0, 0.3)]), query), [None])

    def test_metric_is_read_on_every_query(self):
        # A collection deleted and recreated with another metric is converted by its new one.
        for name, query in DISTANCE_QUERIES:
            with self.subTest(query=name):
                client = client_for(VectorDistances.COSINE, [obj(0, 0.25)])
                coll = client.collections.get.return_value
                self.assertEqual(scores(client, query), [0.75])
                coll.config.get.return_value = SimpleNamespace(
                    vector_index_config=SimpleNamespace(distance_metric=VectorDistances.L2_SQUARED))
                self.assertEqual(scores(client, query), [0.8])
                self.assertEqual(coll.config.get.call_count, 2)


class HybridTests(unittest.TestCase):
    def test_fusion_score_passes_through_and_missing_is_null(self):
        client = client_for(VectorDistances.COSINE, [], hybrid=[obj(0, score=0.83), obj(1, score=0.0),
                                                                obj(2, score=None)])
        got = scores(client, lambda: wc._hybrid_query_sync("Coll", "overtime", 0.5, 5))
        self.assertEqual(got, [0.83, 0.0, None])


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_citations_round_numbers_and_keep_null(self):
        chunks = [{"content": "x" * 300, "source_file": "a.txt", "chunk_index": 0, "score": 0.912345},
                  {"content": "y", "source_file": "b.txt", "chunk_index": 1, "score": None}]
        with patch.object(rag_pipeline.ollama, "chat", AsyncMock(side_effect=["Reformulated", "Answer"])), \
                patch.object(rag_pipeline.ollama, "embed", AsyncMock(return_value=[0.1])), \
                patch.object(wc, "near_vector_query", AsyncMock(return_value=chunks)):
            result = await rag_pipeline.run_query("q", "Coll", "hnsw", 5, 0.5, True, "end_user")
        self.assertEqual([c["score"] for c in result["citations"]], [0.9123, None])

    def test_api_returns_null_score(self):
        self.assertIsNone(Citation(source_file="a", chunk_index=0, score=None, excerpt="").score)
        answer = {"answer": "A", "retrieval_latency_ms": 1, "llm_latency_ms": 1, "chunks_retrieved": 2,
                  "citations": [{"source_file": "a.txt", "chunk_index": 0, "score": 0.91, "excerpt": "x"},
                                {"source_file": "b.txt", "chunk_index": 1, "score": None, "excerpt": "y"}]}
        client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(client.close)
        with patch.object(wc, "collection_exists", AsyncMock(return_value=True)), \
                patch.object(rag_pipeline, "run_query", AsyncMock(return_value=answer)), \
                patch.object(metrics, "record", AsyncMock()):
            response = client.post("/query", json={"question": "q", "collection": "Coll",
                                                   "include_citations": True})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual([c["score"] for c in response.json()["citations"]], [0.91, None])

    def test_unreadable_metric_config_is_a_503(self):
        # The metric read happens inside the Weaviate query, so its failure takes the #373 path.
        for mode in ("hnsw", "flat", "semantic"):
            with self.subTest(mode=mode):
                weaviate = client_for(VectorDistances.COSINE, [obj(0, 0.1)])
                coll = weaviate.collections.get.return_value
                coll.config.get.side_effect = RuntimeError("config read failed")
                client = TestClient(app, raise_server_exceptions=False)
                self.addCleanup(client.close)
                with patch.object(wc, "get_client", return_value=weaviate), \
                        patch.object(wc, "collection_exists", AsyncMock(return_value=True)), \
                        patch.object(rag_pipeline.ollama, "chat", AsyncMock(return_value="Controlled")), \
                        patch.object(rag_pipeline.ollama, "embed", AsyncMock(return_value=[0.1, 0.2])), \
                        patch.object(metrics, "record", AsyncMock()):
                    response = client.post("/query", json={"question": "q", "collection": "Coll",
                                                           "retrieval_mode": mode, "include_citations": True})
                self.assertEqual(response.status_code, 503, response.text)
                self.assertEqual(response.json()["error"]["code"], "SERVICE_UNAVAILABLE")
                self.assertNotIn("config read failed", response.text)
                coll.query.near_vector.assert_not_called()
                coll.query.near_text.assert_not_called()


class ImplementationCopyTests(unittest.TestCase):
    def test_embedded_copies_match(self):
        if not (ROOT / "IMPLEMENTATION.md").is_file():
            self.skipTest("needs the whole repository")
        document = (ROOT / "IMPLEMENTATION.md").read_text()
        for name in ("api/services/weaviate_client.py", "api/services/rag_pipeline.py",
                     "api/models/schemas.py", "ui/src/api/client.ts",
                     "ui/src/components/CitationsPanel.tsx", "scripts/verify/03_query.sh",
                     "scripts/verify/citation_score.py", "scripts/verify/browser/query_config.js",
                     "scripts/verify/README.md"):
            with self.subTest(name=name):
                fence = "````" if name.endswith(".md") else "```"
                header = "### " + name + "\n\n" + fence
                start = document.index("\n", document.index(header) + len(header)) + 1
                end = document.index("\n" + fence + "\n", start)
                self.assertEqual(document[start:end], (ROOT / name).read_text().rstrip("\n"))


if __name__ == "__main__":
    logging.basicConfig(level=logging.CRITICAL)
    unittest.main(verbosity=2)
