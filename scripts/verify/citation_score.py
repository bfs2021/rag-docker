"""Citation scores on a real Weaviate: higher is better, by the collection's metric (#376).

Inside the disposable API container (`03_query.sh`):

    docker compose exec -T -e RAG_TEST_PREFIX="$PREFIX" api python - < scripts/verify/citation_score.py

Creates owned collections with each distance metric through `POST /collections`,
inserts known vectors, and asks `/query` in the `hnsw` and `flat` modes with
Ollama's reformulation, embedding and answer controlled. Weaviate's distances
are real. Then deletes one collection, recreates it under the same name with
another metric, and checks the next query converts by the new metric. Its
collections are deleted at the end, pass or fail.
"""
import math
import os
import sys
import tempfile
import uuid
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from config import settings
from main import app
from services import weaviate_client as wc

prefix = os.environ.get("RAG_TEST_PREFIX", "Vfy") + "Score" + uuid.uuid4().hex[:8]
QUERY = [1.0, 0.0, 0.0, 0.0]


def unit(cos):
    return [cos, math.sqrt(1 - cos * cos), 0.0, 0.0]


# metric -> (vectors, expected score of each against QUERY), best match first.
CASES = {
    "cosine": ([unit(c) for c in (0.95, 0.6, 0.1, -0.5)], [0.95, 0.6, 0.1, -0.5]),
    "dot": ([[a, 0.0, 0.0, 0.0] for a in (3.0, 1.5, -0.5, -2.0)], [3.0, 1.5, -0.5, -2.0]),
    "l2-squared": ([[1.0, t, 0.0, 0.0] for t in (0.0, 0.5, 1.0, math.sqrt(3))], [1.0, 0.8, 0.5, 0.25]),
}

client = TestClient(app)
created = []
failures = []


def create(name, metric, index_type):
    response = client.post("/collections", json={"name": name, "index_type": index_type,
                                                  "distance_metric": metric})
    assert response.status_code == 201, response.text
    if name not in created:
        created.append(name)
    coll = wc.get_client().collections.get(name)
    for i, vector in enumerate(CASES[metric][0]):
        coll.data.insert(properties={"content": f"Score chunk {i}", "source_file": "score.txt",
                                     "chunk_index": i}, vector=vector)


def check(name, metric):
    expected = CASES[metric][1]
    for mode in ("hnsw", "flat"):
        with patch("services.rag_pipeline.ollama.chat", new=AsyncMock(return_value="Controlled")), \
                patch("services.rag_pipeline.ollama.embed", new=AsyncMock(return_value=QUERY)):
            response = client.post("/query", json={"collection": name, "question": "score",
                                                   "retrieval_mode": mode, "top_k": 4,
                                                   "include_citations": True})
        label = f"{metric} collection, {mode} mode"
        if response.status_code != 200:
            failures.append(f"{label}: {response.status_code} {response.text}")
            continue
        got = [c["score"] for c in response.json()["citations"]]
        ok = (len(got) == len(expected)
              and all(isinstance(g, (int, float)) and abs(g - e) < 1e-3 for g, e in zip(got, expected))
              and got == sorted(got, reverse=True))
        print(("PASS " if ok else "FAIL ") + f"{label}: scores {got}, expected {expected}", flush=True)
        if not ok:
            failures.append(f"{label}: scores {got}, expected {expected}")


with tempfile.TemporaryDirectory(prefix="citation-score-") as directory, \
        patch.object(settings, "upload_dir", directory):
    try:
        for metric, index_type in (("cosine", "hnsw"), ("dot", "flat"), ("l2-squared", "flat")):
            name = prefix + metric.replace("-", "").capitalize()
            create(name, metric, index_type)
            check(name, metric)
        # Same name, another metric: the next query must use the new metric.
        name = prefix + "Cosine"
        response = client.delete("/collections/" + name + "?confirm=true")
        assert response.status_code == 200, response.text
        create(name, "l2-squared", "hnsw")
        print("recreated " + name + " with l2-squared", flush=True)
        check(name, "l2-squared")
    finally:
        for name in created:
            try:
                if wc._collection_exists_sync(name):
                    response = client.delete("/collections/" + name + "?confirm=true")
                    if response.status_code != 200:
                        failures.append(f"cleanup {name}: {response.status_code} {response.text}")
            except Exception as error:  # noqa: BLE001
                failures.append(f"cleanup {name}: {error!r}")
        wc.close_client()

if failures:
    print("FAIL " + "; ".join(failures), flush=True)
    sys.exit(1)
print("PASS citation scores convert by each collection's metric, best first", flush=True)
