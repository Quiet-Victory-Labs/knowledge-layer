from pathlib import Path

from fastapi.testclient import TestClient
from rdflib import Dataset, Literal, URIRef

from examples.graph_lab_runtime import create_runtime


def runtime(tmp_path: Path):
    dataset = Dataset(default_union=True)
    graph = dataset.graph(URIRef("urn:test:graph"))
    graph.add((URIRef("urn:test:item"), URIRef("urn:test:label"), Literal("copy from source")))
    dataset.serialize(tmp_path / "knowledge.trig", format="trig")
    (tmp_path / "info.json").write_text('{"nodes": 1}')
    return TestClient(create_runtime(tmp_path))


def test_comparison_accepts_reads_without_rejecting_query_words_in_literals(tmp_path):
    client = runtime(tmp_path)
    response = client.post(
        "/sparql", data={"query": 'SELECT ?s WHERE { ?s ?p "copy from source" }'}
    )
    assert response.status_code == 200, response.text
    assert response.json()["results"]["bindings"][0]["s"]["value"] == "urn:test:item"


def test_comparison_rejects_writes_and_external_data_access(tmp_path):
    client = runtime(tmp_path)
    for query in [
        "INSERT DATA { <urn:a> <urn:b> <urn:c> }",
        "SELECT * WHERE { SERVICE <http://example.invalid/sparql> { ?s ?p ?o } }",
        "SELECT * FROM <http://example.invalid/data.ttl> WHERE { ?s ?p ?o }",
    ]:
        assert client.post("/sparql", data={"query": query}).status_code == 400
    result = client.post(
        "/sparql", data={"query": "SELECT (COUNT(*) AS ?count) WHERE { ?s ?p ?o }"}
    ).json()
    assert result["results"]["bindings"][0]["count"]["value"] == "1"


def test_comparison_named_graph_and_construct_work(tmp_path):
    client = runtime(tmp_path)
    response = client.get(
        "/sparql", params={"query": "ASK { GRAPH <urn:test:graph> { ?s ?p ?o } }"}
    )
    assert response.json()["boolean"] is True
    response = client.post(
        "/sparql",
        data={"query": "CONSTRUCT { ?s ?p ?o } WHERE { ?s ?p ?o }"},
        headers={"accept": "application/rdf+xml"},
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/rdf+xml")


def test_construct_supports_native_aws_json_binding_contract(tmp_path):
    client = runtime(tmp_path)
    response = client.post(
        "/sparql",
        data={"query": "CONSTRUCT { ?s ?p ?o } WHERE { ?s ?p ?o }"},
        headers={"accept": "application/sparql-results+json"},
    )
    assert response.status_code == 200
    assert response.json()["results"]["bindings"] == [
        {
            "subject": {"type": "uri", "value": "urn:test:item"},
            "predicate": {"type": "uri", "value": "urn:test:label"},
            "object": {
                "type": "literal",
                "value": "copy from source",
                "datatype": "http://www.w3.org/2001/XMLSchema#string",
            },
        }
    ]
