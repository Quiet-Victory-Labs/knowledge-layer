"""Local-only comparison runtime: immutable copied knowledge, read-only SPARQL.

Run export against the demo browser API, then serve. This never contacts Example.
Native applications use a retained copy; it is not a production auth boundary.
"""

from __future__ import annotations

import argparse
import gc
import json
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from urllib.parse import parse_qs

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from rdflib import BNode, Dataset, Literal, Namespace, URIRef
from rdflib.namespace import DCTERMS, RDF, RDFS, SKOS
from rdflib.plugins.sparql import prepareQuery
from starlette.concurrency import run_in_threadpool

from knowledge_layer.workspace_graph import _Graph

KL = Namespace("urn:kl:property:")
NODE = Namespace("urn:kl:node:")
TYPE = Namespace("urn:kl:type:")
WORKSPACE = URIRef("urn:kl:graph:knowledge")
VOCAB = URIRef("urn:kl:graph:vocabulary")
SCHEME = URIRef("urn:kl:scheme:example")


def graph_json(triples, ox=None) -> bytes:
    """AWS requests Neptune's JSON triple binding shape for graph queries."""

    def binding(term):
        if isinstance(term, URIRef) or (ox and isinstance(term, ox.NamedNode)):
            return {"type": "uri", "value": str(term) if isinstance(term, URIRef) else term.value}
        if isinstance(term, BNode) or (ox and isinstance(term, ox.BlankNode)):
            return {"type": "bnode", "value": str(term) if isinstance(term, BNode) else term.value}
        value = {"type": "literal", "value": str(term) if isinstance(term, Literal) else term.value}
        if term.language:
            value["xml:lang"] = term.language
        elif term.datatype:
            value["datatype"] = (
                str(term.datatype) if isinstance(term, Literal) else term.datatype.value
            )
        else:
            value["datatype"] = "http://www.w3.org/2001/XMLSchema#string"
        return value

    return json.dumps(
        {
            "head": {"vars": ["subject", "predicate", "object"]},
            "results": {
                "bindings": [
                    dict(zip(("subject", "predicate", "object"), map(binding, triple), strict=True))
                    for triple in triples
                ]
            },
        }
    ).encode()


def write_aws_styles(directory: Path, classes) -> None:
    """Native Graph Explorer styling import; does not change the RDF copy."""
    body = {
        "meta": {
            "kind": "styling-export",
            "version": 1,
            "timestamp": datetime.now(UTC).isoformat(),
            "source": "Graph Explorer",
            "sourceVersion": "3.2.2",
        },
        "data": {
            "vertices": {
                str(kind): {
                    "displayNameAttribute": str(RDFS.label),
                    "longDisplayNameAttribute": str(KL.description),
                }
                for kind in classes
            },
            "edges": {},
        },
    }
    (directory / "aws-styles.json").write_text(json.dumps(body, indent=2))


def export_copy(api: str, destination: Path) -> dict:
    """Use the authorized API's retained snapshots, without any source refresh."""
    sources = []
    cursor = None
    with httpx.Client(base_url=api, timeout=60) as client:
        while True:
            page = client.get(
                "/api/knowledge/connections",
                params={"limit": 100, **({"cursor": cursor} if cursor else {})},
            )
            page.raise_for_status()
            body = page.json()
            for item in body["connections"]:
                detail = client.get(f"/api/knowledge/connections/{item['id']}")
                detail.raise_for_status()
                value = detail.json()
                sources.append({**value["connection"], "snapshot": value.get("snapshot")})
            cursor = body.get("next_cursor")
            if not cursor:
                break
    builder = _Graph("demo", sources)
    builder.build()
    nodes = list(builder.nodes.values())
    edges = list(builder.edges.values())
    dataset = Dataset(default_union=True)
    graph, vocab = dataset.graph(WORKSPACE), dataset.graph(VOCAB)
    for prefix, namespace in [
        ("kl", KL),
        ("node", NODE),
        ("type", TYPE),
        ("skos", SKOS),
        ("dct", DCTERMS),
    ]:
        dataset.bind(prefix, namespace)
    by_id = {n["id"]: n for n in nodes}
    for node in nodes:
        subject = NODE[node["id"]]
        graph.add((subject, RDF.type, TYPE[node["kind"]]))
        graph.add((subject, RDFS.label, Literal(node["label"], lang="en")))
        for key in [
            "status",
            "trust_state",
            "connection_id",
            "snapshot_id",
            "record_id",
            "source_name",
            "description",
            "definition_status",
            "definition_trust_state",
            "native_type",
            "asset_name",
        ]:
            if node.get(key) is not None:
                graph.add((subject, KL[key], Literal(str(node[key]))))
        for eid in node.get("evidence_ids", []):
            graph.add((subject, KL["evidence"], NODE[eid]))
        if node["kind"] in {"concept", "metric", "ontology"}:
            vocab.add((subject, RDF.type, SKOS.Concept))
            vocab.add((subject, SKOS.prefLabel, Literal(node["label"], lang="en")))
            vocab.add((subject, SKOS.inScheme, SCHEME))
            vocab.add((subject, SKOS.topConceptOf, SCHEME))
            vocab.add((SCHEME, SKOS.hasTopConcept, subject))
            vocab.add(
                (
                    subject,
                    SKOS.note,
                    Literal(
                        f"Source: {node.get('source_name', 'unknown')}. Record state: {node['status']}. Definition state: {node.get('definition_status', 'unreviewed')}. Snapshot: {node.get('snapshot_id', 'unknown')}",
                        lang="en",
                    ),
                )
            )
            if node.get("description"):
                predicate = (
                    SKOS.definition if node.get("definition_status") == "approved" else SKOS.note
                )
                prefix = "" if predicate == SKOS.definition else "[Definition requires review] "
                vocab.add((subject, predicate, Literal(prefix + node["description"], lang="en")))
            if node.get("connection_id"):
                collection = URIRef(f"urn:kl:collection:{node['connection_id']}")
                vocab.add((collection, RDF.type, SKOS.Collection))
                vocab.add(
                    (
                        collection,
                        SKOS.prefLabel,
                        Literal(node.get("source_name", node["connection_id"]), lang="en"),
                    )
                )
                vocab.add((collection, SKOS.member, subject))
    for edge in edges:
        subject, obj = NODE[edge["subject_id"]], NODE[edge["object_id"]]
        # Proposed links are explicitly named pending; this is a browsing projection,
        # not an assertion that proposed semantic equivalence is established.
        prefix = (
            "pending_"
            if edge["status"]
            in {"pending", "pending_review", "candidate", "proposed", "unreviewed"}
            else ""
        )
        predicate = KL[prefix + edge["predicate"]]
        graph.add((subject, predicate, obj))
        claim = URIRef(f"urn:kl:relationship:{edge['id']}")
        graph.add((claim, RDF.type, TYPE["relationship"]))
        graph.add((claim, RDF.subject, subject))
        graph.add((claim, RDF.predicate, predicate))
        graph.add((claim, RDF.object, obj))
        graph.add(
            (claim, RDFS.label, Literal(f"{edge['predicate']} [{edge['status']}]", lang="en"))
        )
        graph.add((claim, KL["status"], Literal(edge["status"])))
        graph.add((claim, KL["trust_state"], Literal(edge["trust_state"])))
        for eid in edge.get("evidence_ids", []):
            graph.add((claim, KL["evidence"], NODE[eid]))
        if (
            edge["predicate"] in {"broader", "narrower", "related"}
            and edge["status"] == "approved"
            and by_id.get(edge["subject_id"], {}).get("kind") in {"concept", "metric", "ontology"}
            and by_id.get(edge["object_id"], {}).get("kind") in {"concept", "metric", "ontology"}
        ):
            vocab.add((subject, SKOS[edge["predicate"]], obj))
    vocab.add((SCHEME, RDF.type, SKOS.ConceptScheme))
    vocab.add((SCHEME, DCTERMS.title, Literal("Example knowledge vocabulary", lang="en")))
    vocab.add((SCHEME, RDFS.label, Literal("Example knowledge vocabulary", lang="en")))
    vocab.add(
        (
            SCHEME,
            DCTERMS.description,
            Literal(
                "Source-scoped vocabulary from retained Knowledge Layer snapshots. Pending definitions remain review notes. Source collections are navigation groups; no hierarchy has been invented.",
                lang="en",
            ),
        )
    )
    info = {
        "nodes": len(nodes),
        "edges": len(edges),
        "terms": len(set(vocab.subjects(RDF.type, SKOS.Concept))),
        "sources": len(sources),
        "triples": len(dataset),
        "exported_at": datetime.now(UTC).isoformat(),
        "budget_reached": builder.budget_reached,
    }
    destination.mkdir(parents=True, exist_ok=True)
    dataset.serialize(destination / "knowledge.trig", format="trig")
    combined = dataset.graph(URIRef("urn:kl:combined"))
    for s, p, o, _ in list(dataset.quads()):
        combined.add((s, p, o))
    combined.serialize(destination / "knowledge.ttl", format="turtle")
    (destination / "info.json").write_text(json.dumps(info, indent=2))
    write_aws_styles(destination, set(dataset.objects(None, RDF.type)))
    return info


def create_runtime(directory: Path, aws_dist: Path | None = None) -> FastAPI:
    # Oxigraph keeps native-app schema scans fast on the complete copied dataset.
    # RDFLib remains the fallback for installations without the graph-lab extra.
    try:
        import pyoxigraph as ox
    except ImportError:
        ox = None
    if ox:
        store = ox.Store()
        store.load(path=str(directory / "knowledge.trig"), format=ox.RdfFormat.TRIG)
        local_graphs = {URIRef(graph.value) for graph in store.named_graphs()}
    else:
        dataset = Dataset(default_union=True)
        dataset.parse(directory / "knowledge.trig", format="trig")
        local_graphs = {graph.identifier for graph in dataset.graphs()}
    copy_info = json.loads((directory / "info.json").read_text())
    classes = (
        [
            quad.object.value
            for quad in store.quads_for_pattern(None, ox.NamedNode(str(RDF.type)), None, None)
        ]
        if ox
        else list(dataset.objects(None, RDF.type))
    )
    write_aws_styles(directory, sorted(set(classes), key=str))
    # Local Skosmos generic dialect uses named vocabulary graph; AWS uses union.
    app = FastAPI(title="Knowledge UI comparison copy")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:4173", "http://127.0.0.1:4183"],
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )
    query_lock = Lock()

    @app.get("/lab/info")
    def info():
        return JSONResponse({**copy_info, "query_engine": "Oxigraph" if ox else "RDFLib"})

    @app.get("/lab/knowledge.ttl")
    def turtle():
        return FileResponse(
            directory / "knowledge.ttl",
            media_type="text/turtle",
            filename="knowledge-comparison.ttl",
        )

    @app.get("/lab/aws-styles.json")
    def aws_styles():
        return FileResponse(
            directory / "aws-styles.json",
            media_type="application/json",
            filename="knowledge-aws-styles.json",
        )

    @app.get("/defaultConnection")
    @app.get("/aws/defaultConnection")
    def default_connection():
        return {
            "GRAPH_EXP_CONNECTION_URL": "http://127.0.0.1:4182",
            "GRAPH_EXP_PUBLIC_OR_PROXY_ENDPOINT": "http://127.0.0.1:4182",
            "GRAPH_EXP_USING_PROXY_SERVER": False,
            "GRAPH_EXP_GRAPH_TYPE": "sparql",
            "GRAPH_EXP_IAM": False,
            "GRAPH_EXP_NODE_EXPANSION_LIMIT": 100,
            "GRAPH_EXP_FETCH_REQUEST_TIMEOUT": 30000,
        }

    @app.post("/logger")
    @app.post("/aws/logger")
    def logger():
        return {"ok": True}

    @app.get("/rdf/statistics/summary")
    def summary():
        # Neptune summary is optional. Generic SPARQL schema discovery follows.
        return {
            "payload": {"graphSummary": None},
            "comparison": "Generic SPARQL: discover schema with queries",
        }

    @app.api_route("/sparql", methods=["GET", "POST"])
    async def sparql(request: Request):
        query = request.query_params.get("query", "")
        if request.method == "POST":
            body = await request.body()
            if len(body) > 100_000:
                raise HTTPException(413, "Query is too large")
            content_type = request.headers.get("content-type", "")
            query = (
                body.decode()
                if "application/sparql-query" in content_type
                else parse_qs(body.decode()).get("query", [""])[0]
            )
        if not query or len(query) > 80_000:
            raise HTTPException(400, "A bounded SPARQL query is required")
        return await run_in_threadpool(evaluate_query, query, request.headers.get("accept", ""))

    def evaluate_query(query: str, accept: str):
        with query_lock:
            return evaluate_locked(query, accept)

    def evaluate_locked(query: str, accept: str):
        try:
            prepared = prepareQuery(query)

            def external_clause(value):
                if getattr(value, "name", None) == "ServiceGraphPattern":
                    return True
                if isinstance(value, dict):
                    return any(external_clause(v) for v in value.values())
                if isinstance(value, (list, tuple)):
                    return any(external_clause(v) for v in value)
                return False

            clauses = prepared.algebra.get("datasetClause") or []
            remote_dataset = any(
                value not in local_graphs
                for clause in clauses
                for key, value in clause.items()
                if key in {"default", "named"}
            )
            if remote_dataset or external_clause(prepared.algebra):
                raise ValueError(
                    "Remote dataset and SERVICE clauses are unavailable on the local copy"
                )
            if prepared.algebra.name not in {
                "SelectQuery",
                "AskQuery",
                "ConstructQuery",
                "DescribeQuery",
            }:
                raise ValueError("Only read queries are supported")
            if ox:
                result = store.query(query, use_default_graph_as_union=True)
                try:
                    if isinstance(result, (ox.QuerySolutions, ox.QueryBoolean)):
                        data = result.serialize(format=ox.QueryResultsFormat.JSON)
                        mime = "application/sparql-results+json"
                    elif "sparql-results+json" in accept:
                        data = graph_json(result, ox)
                        mime = "application/sparql-results+json"
                    else:
                        fmt, mime = (
                            (ox.RdfFormat.RDF_XML, "application/rdf+xml")
                            if "rdf+xml" in accept
                            else (ox.RdfFormat.TURTLE, "text/turtle")
                        )
                        data = result.serialize(format=fmt)
                finally:
                    # Native result iterators must be finalized on their owner thread.
                    del result
                    gc.collect()
                return Response(content=data, media_type=mime)
            result = dataset.query(prepared)
            if result.type in {"SELECT", "ASK"}:
                data = result.serialize(format="json")
                return Response(content=data, media_type="application/sparql-results+json")
            if "sparql-results+json" in accept:
                return Response(
                    content=graph_json(result.graph), media_type="application/sparql-results+json"
                )
            fmt, mime = (
                ("xml", "application/rdf+xml") if "rdf+xml" in accept else ("turtle", "text/turtle")
            )
            return Response(content=result.graph.serialize(format=fmt), media_type=mime)
        except Exception as exc:
            raise HTTPException(
                400, f"Comparison query failed: {type(exc).__name__}: {str(exc)[:240]}"
            ) from exc

    if aws_dist and aws_dist.is_dir():
        app.mount("/aws/explorer", StaticFiles(directory=aws_dist, html=True), name="aws-explorer")
        app.mount("/", StaticFiles(directory=aws_dist, html=True), name="aws-static")
    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["export", "serve"])
    parser.add_argument("--directory", type=Path, default=Path(".local/graph-lab/data"))
    parser.add_argument("--api", default="http://127.0.0.1:4173")
    parser.add_argument("--aws-dist", type=Path)
    parser.add_argument("--port", type=int, default=4182)
    args = parser.parse_args()
    if args.action == "export":
        print(json.dumps(export_copy(args.api, args.directory)))
    else:
        import uvicorn

        uvicorn.run(
            create_runtime(args.directory, args.aws_dist),
            host="127.0.0.1",
            port=args.port,
            access_log=False,
            limit_concurrency=32,
        )
