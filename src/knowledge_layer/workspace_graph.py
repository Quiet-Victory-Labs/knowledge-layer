"""One permission-scoped knowledge graph over current connected sources.

Source records remain distinct. Shared term clusters are navigation/reconciliation
nodes, never automatic identity, an approved join, or a new business definition.
"""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Any

from knowledge_layer.connectors.common import normalize_name, stable_id
from knowledge_layer.store import (
    KnowledgeAccessDenied,
    KnowledgeNotFound,
    KnowledgeValidationError,
    validate_tenant_id,
)

MAX_RECORDS = 20000
MAX_LINKS = 60000
GENERIC_TERMS = frozenset({
    "id", "name", "description", "status", "context", "decision", "consequences",
    "created at", "updated at", "source", "agent", "serve", "control",
})


def workspace_graph(
    store: Any, *, tenant_id: str, connection_id: str | None = None,
    q: str | None = None, focus_id: str | None = None, depth: int = 2,
    limit: int = 180, source_limit: int = 40, cursor: str | None = None,
) -> dict[str, Any]:
    """Project a bounded current workspace; do not read or mutate upstream sources."""
    tenant = validate_tenant_id(tenant_id)
    if not 10 <= limit <= 400 or not 1 <= source_limit <= 100 or not 0 <= depth <= 3:
        raise KnowledgeValidationError("Graph limit must be 10-400, sources 1-100, depth 0-3")
    if q is not None and (not isinstance(q, str) or len(q) > 200):
        raise KnowledgeValidationError("Graph search must be at most 200 characters")
    if focus_id is not None and (not isinstance(focus_id, str) or not 1 <= len(focus_id) <= 160):
        raise KnowledgeValidationError("Graph focus must be a bounded node ID")
    if connection_id and cursor:
        raise KnowledgeValidationError("Source filters cannot use a workspace page cursor")
    if connection_id:
        source = store.get_connection(tenant, connection_id)
        if source is None:
            raise KnowledgeNotFound("Source was not found")
        sources, next_cursor, total = [source], None, 1
    else:
        page = store.page_connections(tenant, limit=source_limit, cursor=cursor)
        sources, next_cursor, total = page["connections"], page["next_cursor"], page["total"]

    # A source filter retains its explicitly cited context in other authorized
    # sources. Never include a denied dependency or an unbounded estate scan.
    included = {source["id"]: source for source in sources}
    if connection_id:
        for source in list(sources):
            for proposal in (source.get("snapshot") or {}).get("proposals", []):
                for ref in proposal.get("evidence_refs") or []:
                    cid = ref.get("connection_id")
                    if not cid or cid in included or len(included) >= source_limit:
                        continue
                    try:
                        dependency = store.get_connection(tenant, cid)
                    except (KnowledgeNotFound, KnowledgeAccessDenied):
                        continue
                    if dependency:
                        included[cid] = dependency
    sources = list(included.values())
    builder = _Graph(tenant, sources)
    builder.build()
    nodes, edges, view_truncated = builder.select(q=q, focus_id=focus_id, depth=depth,
                                                limit=limit, allow_missing_focus=bool(cursor))
    return {
        "view": "workspace", "focus_id": focus_id, "query": q or "",
        "connection_id": connection_id, "version": None,
        "connections": [{
            "id": source["id"], "name": source["name"], "kind": source["kind"],
            "snapshot_id": (source.get("snapshot") or {}).get("id"),
            "snapshot_version": (source.get("snapshot") or {}).get("version"),
            "agent_ready": (source.get("snapshot") or {}).get("agent_ready", False),
            "source_status": source.get("status"),
        } for source in sources],
        "total_sources": total, "loaded_sources": len(sources), "next_cursor": next_cursor,
        "nodes": nodes, "edges": edges,
        "truncated": bool(next_cursor or builder.budget_reached or view_truncated),
        "coverage": {
            "source_scope": "filtered_source_and_authorized_citations" if connection_id else "authorized_source_page",
            "source_page_complete": next_cursor is None,
            "records_considered": len(builder.nodes), "links_considered": len(builder.edges),
            "visible_nodes": len(nodes), "visible_edges": len(edges),
            "record_budget_reached": builder.budget_reached,
            "shared_terms": sum(node["kind"] == "shared_term" for node in builder.nodes.values()),
            "cross_source_links": sum(edge.get("cross_source", False) for edge in builder.edges.values()),
            "matching_definitions_are_equivalent": False,
            "query_execution": False,
        },
        "trust_policy": "Shared labels are reconciliation candidates. Scoped definitions, explicit source relationships, citations and review states remain distinct. No query is executed.",
    }


class _Graph:
    def __init__(self, tenant: str, sources: list[dict[str, Any]]):
        self.tenant, self.sources = tenant, sources
        self.nodes: dict[str, dict[str, Any]] = {}
        self.edges: dict[str, dict[str, Any]] = {}
        self.lookup: dict[tuple[str, str, str], str] = {}
        self.term_members: dict[str, list[str]] = defaultdict(list)
        self.budget_reached = False

    def node(self, cid: str, kind: str, record_id: str, label: str, **values: Any) -> str:
        key = (cid, kind, str(record_id))
        nid = stable_id("ws", self.tenant, *key)
        if nid not in self.nodes and len(self.nodes) >= MAX_RECORDS:
            self.budget_reached = True
            return nid
        self.lookup[key] = nid
        self.nodes[nid] = {
            "id": nid, "kind": kind, "record_id": str(record_id), "label": str(label)[:240],
            "connection_id": cid or None, "description": None, "status": "observed",
            "trust_state": "observed_metadata", "evidence_ids": [], **values,
        }
        if kind in {"concept", "metric", "field", "ontology"}:
            term = normalize_name(str(label))
            if term and nid not in self.term_members[term]:
                self.term_members[term].append(nid)
        return nid

    def edge(self, left: str | None, predicate: str, right: str | None, *,
             status: str = "observed", trust_state: str = "observed_metadata",
             evidence_ids: list[str] | None = None, **extra: Any) -> None:
        if left not in self.nodes or right not in self.nodes or left == right:
            return
        # Reviewed and pending claims with identical endpoints stay separate.
        # Structural/provenance links without a claim remain deduplicated.
        eid = stable_id("wse", left, predicate, right, extra.get("assertion_id"))
        if eid not in self.edges and len(self.edges) >= MAX_LINKS:
            self.budget_reached = True
            return
        self.edges[eid] = {
            "id": eid, "subject_id": left, "object_id": right, "predicate": predicate[:100],
            "status": status, "trust_state": trust_state, "evidence_ids": evidence_ids or [],
            "cross_source": bool(self.nodes[left].get("connection_id") != self.nodes[right].get("connection_id")),
            **extra,
        }

    def local(self, cid: str, record_id: Any, kind: str | None = None) -> str | None:
        if kind:
            return self.lookup.get((cid, kind, str(record_id)))
        return next((self.lookup.get((cid, k, str(record_id))) for k in
                     ("concept", "metric", "asset", "document", "ontology", "field", "evidence", "assertion")
                     if (cid, k, str(record_id)) in self.lookup), None)

    def build(self) -> None:
        for source in self.sources:
            cid, snapshot = source["id"], source.get("snapshot") or {}
            sid = snapshot.get("id")
            sn = self.node(cid, "source", cid, source["name"], snapshot_id=sid,
                           source_name=source["name"], description="Connected source with its current retained knowledge snapshot.",
                           status=source.get("status") or "not_synced", agent_ready=snapshot.get("agent_ready", False))
            common = {"snapshot_id": sid, "source_name": source["name"]}
            for asset in snapshot.get("assets", []):
                kind = "document" if asset.get("asset_type") == "document" else "ontology" if asset.get("asset_type") == "semantic_definition" else "asset"
                upstream = asset.get("upstream_definition") or {}
                aid = self.node(cid, kind, asset["id"], asset.get("name") or asset["id"],
                                **common, description=asset.get("description") or upstream.get("definition"),
                                source_pointer=asset.get("source_pointer"), asset_type=asset.get("asset_type"),
                                upstream_definition=upstream or None)
                self.edge(sn, "contains", aid)
                for field in asset.get("schema") or []:
                    name = field.get("name")
                    if not name:
                        continue
                    fid = self.node(cid, "field", f"{asset['id']}:{name}", name, **common,
                                    description=field.get("description"), asset_id=asset["id"], asset_name=asset.get("name"),
                                    source_pointer=asset.get("source_pointer"), native_type=field.get("native_type"),
                                    data_type=field.get("data_type"), nullable=field.get("nullable"),
                                    status=field.get("description_status") or "observed",
                                    trust_state="owner_reviewed" if field.get("description_status") == "approved" else "observed_metadata")
                    self.edge(aid, "has_field", fid)
            for evidence in snapshot.get("evidence", []):
                en = self.node(cid, "evidence", evidence["id"], evidence.get("evidence_type") or "Evidence", **common,
                               description=str(evidence.get("summary") or "")[:1200], source_pointer=evidence.get("source_pointer"),
                               evidence_type=evidence.get("evidence_type"), trust_state=evidence.get("trust_state") or "observed_metadata")
                self.edge(en, "observed_in", self.local(cid, evidence.get("asset_id")))
            for collection, kind in (("concepts", "concept"), ("metrics", "metric")):
                for record in snapshot.get(collection, []):
                    upstream = record.get("upstream_definition") or {}
                    handles = [self.local(cid, eid, "evidence") for eid in record.get("evidence_ids") or []]
                    handles = [eid for eid in handles if eid]
                    rn = self.node(cid, kind, record["id"], record.get("name") or record["id"], **common,
                                   description=record.get("definition") or record.get("description") or upstream.get("definition"),
                                   status=record.get("status") or "candidate", trust_state=record.get("trust_state") or "inferred",
                                   definition_status=record.get("definition_status"),
                                   definition_assertion_id=stable_id("ws", self.tenant, cid, "assertion", record["definition_proposal_id"])
                                   if record.get("definition_proposal_id") else None,
                                   definition_trust_state="owner_reviewed" if record.get("definition_status") == "approved" else None,
                                   semantic_type=record.get("semantic_type"), evidence_ids=handles,
                                   upstream_definition=upstream or None, unit=record.get("unit") or upstream.get("unit"))
                    provenance = list(record.get("provenance") or [])
                    provenance += [record.get("source_binding") or {}, {"asset_id": record.get("source_asset_id") or record.get("asset_id"), "field": record.get("field")}]
                    for binding in provenance:
                        an = self.local(cid, binding.get("asset_id"))
                        if an:
                            self.edge(rn, "derived_from", an, status=record.get("status") or "candidate", trust_state=record.get("trust_state") or "inferred")
                            if binding.get("field"):
                                self.edge(rn, "describes_field", self.local(cid, f"{binding['asset_id']}:{binding['field']}", "field"),
                                          status=record.get("status") or "candidate", trust_state=record.get("trust_state") or "inferred")
                    for handle in handles:
                        self.edge(rn, "supported_by", handle, status=record.get("status") or "candidate")
                    if not provenance or not any(self.local(cid, binding.get("asset_id")) for binding in provenance):
                        self.edge(sn, "contains", rn)
        for source in self.sources:
            self._relationships(source)
        self._terms()

    def _relationships(self, source: dict[str, Any]) -> None:
        cid, snapshot = source["id"], source.get("snapshot") or {}
        sources = {row["id"]: row for row in self.sources}
        for relationship in snapshot.get("relationships", []):
            other = relationship.get("to_connection_id") or cid
            subject = self.local(cid, relationship.get("subject_id"))
            if relationship.get("source_field") and relationship.get("source_asset_id"):
                subject = self.local(cid, f"{relationship['source_asset_id']}:{relationship['source_field']}", "field") or subject
            handles = [en for eid in relationship.get("evidence_ids") or []
                       if (en := self.local(cid, eid, "evidence"))]
            for ref in relationship.get("evidence_refs") or []:
                dependency = sources.get(ref.get("connection_id"))
                if (dependency or {}).get("status") == "error" or ((dependency or {}).get("snapshot") or {}).get("id") != ref.get("snapshot_id"):
                    continue
                if en := self.local(ref.get("connection_id"), ref.get("evidence_id"), "evidence"):
                    handles.append(en)
            self.edge(subject, relationship.get("predicate") or "related_to",
                      self.local(other, relationship.get("object_id")), status=relationship.get("status") or "candidate",
                      trust_state=relationship.get("trust_state") or "inferred",
                      evidence_ids=list(dict.fromkeys(handles)),
                      assertion_id=stable_id("ws", self.tenant, cid, "assertion", relationship["decision_proposal_id"])
                      if relationship.get("decision_proposal_id") else None)
        for proposal in snapshot.get("proposals", []):
            if proposal.get("status") not in {"pending_review", "approved"}:
                continue
            if proposal.get("source_snapshot_id") and proposal["source_snapshot_id"] != snapshot.get("id"):
                continue
            if proposal.get("status") == "approved" and proposal.get("effective_on_current_snapshot") is False:
                continue
            payload = proposal.get("payload") or {}
            text = proposal.get("proposed_definition") or payload.get("description") or payload.get("name")
            target = self.local(cid, proposal.get("target_concept_id") or proposal.get("asset_id") or proposal.get("subject_id"))
            field_name = payload.get("field") or payload.get("measure_field")
            if field_name:
                target = self.local(cid, f"{proposal.get('asset_id')}:{field_name}", "field") or target
            if not target:
                continue
            kind = proposal.get("assertion_kind") or proposal.get("proposal_type") or "definition"
            pn = self.node(cid, "assertion", proposal["id"], payload.get("name") or kind.replace("_", " "),
                           snapshot_id=snapshot.get("id"), source_name=source["name"],
                           description=str(text or proposal.get("rationale") or "")[:4000],
                           status=proposal["status"], trust_state="owner_reviewed" if proposal["status"] == "approved" else "candidate",
                           assertion_kind=kind, rationale=proposal.get("rationale"),
                           evidence_ids=[en for eid in proposal.get("evidence_ids") or [] if (en := self.local(cid, eid, "evidence"))])
            self.edge(pn, "defines" if text else "proposes_relationship", target, status=proposal["status"],
                      trust_state="owner_reviewed" if proposal["status"] == "approved" else "candidate")
            if pn not in self.nodes:
                continue
            binding_handles = list(self.nodes[pn]["evidence_ids"])
            for en in self.nodes[pn]["evidence_ids"]:
                self.edge(pn, "supported_by", en, status=proposal["status"])
            for ref in proposal.get("evidence_refs") or []:
                dependency = sources.get(ref.get("connection_id"))
                ds = (dependency or {}).get("snapshot") or {}
                if ds.get("id") != ref.get("snapshot_id") or (dependency or {}).get("status") == "error":
                    continue
                en = self.local(ref.get("connection_id"), ref.get("evidence_id"), "evidence")
                self.edge(pn, "supported_by", en, status=proposal["status"], evidence_ids=[en] if en else [])
                if en:
                    binding_handles.append(en)
                # The explicit current citation is a proven evidence connection,
                # not an equivalence between the structured field and doc term.
                for concept in ds.get("concepts", []):
                    if ref.get("evidence_id") in (concept.get("evidence_ids") or []):
                        self.edge(pn, "cites_term", self.local(dependency["id"], concept["id"], "concept"), status=proposal["status"])
            if kind == "term_binding":
                dependency = sources.get(payload.get("target_connection_id"))
                ds = (dependency or {}).get("snapshot") or {}
                if ds.get("id") == payload.get("target_snapshot_id") and (dependency or {}).get("status") != "error":
                    term = self.local(payload.get("target_connection_id"), payload.get("target_concept_id"), "concept")
                    self.edge(target, payload.get("relationship") or "related_to", term,
                              status=proposal["status"], trust_state="owner_reviewed" if proposal["status"] == "approved" else "candidate",
                              evidence_ids=list(dict.fromkeys(binding_handles)), assertion_id=pn)

    def _terms(self) -> None:
        for label, member_ids in list(self.term_members.items()):
            members = [self.nodes[mid] for mid in member_ids]
            source_ids = {member.get("connection_id") for member in members}
            if len(source_ids) < 2:
                continue
            definitions = sorted({normalize_name(str(member["description"])) for member in members if member.get("description")})
            units = sorted({str(member["unit"]) for member in members if member.get("unit")})
            tn = self.node("", "shared_term", label, label.title(), status="candidate", trust_state="possible_shared_term",
                           description="Matching source labels link these scoped records for reconciliation. This cluster does not assert that their definitions, identities or formulas are equivalent.",
                           source_count=len(source_ids), member_count=len(members),
                           definition_variant_count=len(definitions), unit_variants=units,
                           conflict=len(definitions) > 1 or len(units) > 1,
                           members=[{"id": member["id"], "connection_id": member["connection_id"], "record_id": member["record_id"],
                                     "kind": member["kind"], "snapshot_id": member.get("snapshot_id")} for member in members][:100])
            for mid in member_ids:
                self.edge(tn, "possible_meaning_in", mid, status="candidate", trust_state="label_match_requires_review")

    def select(self, *, q: str | None, focus_id: str | None, depth: int, limit: int,
               allow_missing_focus: bool = False):
        adjacent: dict[str, list[str]] = defaultdict(list)
        for edge in self.edges.values():
            adjacent[edge["subject_id"]].append(edge["object_id"])
            adjacent[edge["object_id"]].append(edge["subject_id"])
        query = normalize_name(q or "")
        if focus_id:
            if focus_id not in self.nodes:
                if allow_missing_focus:
                    return [], [], bool(self.nodes)
                raise KnowledgeNotFound("Node was not found in this authorized source page")
            seeds = [focus_id]
        elif query:
            seeds = sorted((nid for nid, node in self.nodes.items()
                            if query in normalize_name(str(node["label"]))),
                           key=lambda nid: (self.nodes[nid]["kind"] != "shared_term", self.nodes[nid]["label"], nid))
            if not seeds:
                seeds = [nid for nid, node in self.nodes.items() if query in normalize_name(str(node.get("description") or ""))]
        else:
            seeds = [nid for nid, node in self.nodes.items() if node["kind"] == "source"]
            terms = [nid for nid, node in self.nodes.items() if node["kind"] == "shared_term"]
            terms.sort(key=lambda nid: (
                normalize_name(self.nodes[nid]["label"]) in GENERIC_TERMS,
                -len({self.nodes[mid]["kind"] for mid in adjacent[nid]}),
                -self.nodes[nid]["source_count"], self.nodes[nid]["label"], nid,
            ))
            # Round robin keeps overview connections distributed across shared
            # terms and sources rather than exhausting the view on one hub.
            for tn in terms[:12]:
                seeds.append(tn)
                by_source: dict[str, list[str]] = defaultdict(list)
                for mid in adjacent[tn]:
                    by_source[self.nodes[mid].get("connection_id") or ""].append(mid)
                for ids in by_source.values():
                    ids.sort(key=lambda mid: (self.nodes[mid]["kind"] != "concept", mid))
                seeds.extend(ids[0] for ids in list(by_source.values())[:6])
            # Sources without a shared label still appear with their assets.
            for source_id in seeds[:len(self.sources)]:
                seeds.extend(adjacent[source_id][:2])
        selected: dict[str, None] = dict.fromkeys(seeds[:limit])
        queue = deque((nid, 0) for nid in selected)
        while queue and len(selected) < limit:
            nid, distance = queue.popleft()
            if distance >= depth:
                continue
            neighbors = sorted(adjacent[nid], key=lambda other: (
                self.nodes[other]["kind"] == "evidence", self.nodes[other]["kind"] != "shared_term", other,
            ))
            for other in neighbors:
                if other not in selected:
                    selected[other] = None
                    queue.append((other, distance + 1))
                    if len(selected) == limit:
                        break
        # Every presented link has both endpoints; evidence IDs remain exact
        # handles whose neighborhoods can be requested on the same source page.
        edges = [edge for edge in self.edges.values() if edge["subject_id"] in selected and edge["object_id"] in selected]
        links_truncated = len(edges) > limit * 5
        if len(edges) > limit * 5:
            edges = edges[:limit * 5]
        return [self.nodes[nid] for nid in selected], edges, len(selected) < len(self.nodes) or links_truncated


def workspace_ontology(graph: dict[str, Any]) -> dict[str, Any]:
    """Export the same bounded unified view with explicit candidate link states."""
    from rdflib import RDF, RDFS, Graph, Literal, Namespace
    from rdflib.namespace import SKOS

    from knowledge_layer.ontology import KL

    rdf = Graph()
    rdf.bind("kl", KL)
    rdf.bind("skos", SKOS)
    ns = Namespace("https://knowledge-layer.example/workspace/")
    root = ns[stable_id("graph", [node["id"] for node in graph["nodes"]], graph.get("query"), graph.get("focus_id"))]
    rdf.add((root, RDF.type, KL.WorkspaceGraph))
    rdf.add((root, KL.truncated, Literal(graph["truncated"])))
    rdf.add((root, KL.trustPolicy, Literal(graph["trust_policy"])))
    classes = {"source": KL.Connection, "asset": KL.Asset, "field": KL.Field,
               "document": KL.Asset, "ontology": KL.Concept, "concept": SKOS.Concept,
               "shared_term": KL.TermCluster, "metric": KL.Metric, "evidence": KL.Evidence,
               "assertion": KL.Proposal}
    for node in graph["nodes"]:
        uri = ns[node["id"]]
        rdf.add((root, KL.visibleNode, uri))
        rdf.add((uri, RDF.type, classes[node["kind"]]))
        rdf.add((uri, RDFS.label, Literal(node["label"])))
        rdf.add((uri, KL.nodeKind, Literal(node["kind"])))
        properties = {"description": RDFS.comment, "status": KL.status,
                      "trust_state": KL.trustState, "snapshot_id": KL.snapshotId,
                      "connection_id": KL.connectionId, "record_id": KL.recordId,
                      "definition_status": KL.definitionStatus, "definition_trust_state": KL.definitionTrustState,
                      "native_type": KL.nativeType, "nullable": KL.nullable, "unit": KL.unit}
        for key, predicate in properties.items():
            if node.get(key) is not None:
                rdf.add((uri, predicate, Literal(node[key])))
        if node.get("definition_assertion_id"):
            rdf.add((uri, KL.definitionProposal, ns[node["definition_assertion_id"]]))
        for evidence_id in node.get("evidence_ids") or []:
            rdf.add((uri, KL.evidence, ns[evidence_id]))
    for edge in graph["edges"]:
        uri = ns[edge["id"]]
        rdf.add((root, KL.visibleRelationship, uri))
        rdf.add((uri, RDF.type, KL.Relationship))
        rdf.add((uri, KL.subject, ns[edge["subject_id"]]))
        rdf.add((uri, KL.object, ns[edge["object_id"]]))
        rdf.add((uri, KL.predicateName, Literal(edge["predicate"])))
        rdf.add((uri, KL.status, Literal(edge["status"])))
        rdf.add((uri, KL.trustState, Literal(edge["trust_state"])))
        if edge.get("assertion_id"):
            rdf.add((uri, KL.decisionProposal, ns[edge["assertion_id"]]))
        for evidence_id in edge.get("evidence_ids") or []:
            rdf.add((uri, KL.evidence, ns[evidence_id]))
        # Reified candidate links deliberately do not emit owl:sameAs,
        # skos:exactMatch or another equivalence assertion.
    return {"format": "turtle", "media_type": "text/turtle", "triple_count": len(rdf),
            "schema": "workspace", "schema_version": "knowledge-workspace-rdf-shacl-v1",
            "content": rdf.serialize(format="turtle"), "coverage": graph["coverage"],
            "next_cursor": graph["next_cursor"], "truncated": graph["truncated"],
            "trust_policy": graph["trust_policy"]}


def register_workspace_tools(server: Any, store: Any, *, tenant_id: str = "demo") -> None:
    from knowledge_layer.mcp import READ_ONLY_ANNOTATIONS, _contract_tool_response

    @server.tool(annotations=READ_ONLY_ANNOTATIONS, description="Explore one collective current knowledge graph across authorized sources: tables, fields, document terms, ontology imports, metrics, definitions and evidence links. Shared label clusters are candidates, not equivalence. Search or focus a returned node; pass next_cursor for another source page. Limits: nodes 10-400, sources 1-100, depth 0-3. Reads retained metadata only.")
    def get_workspace_graph(
        connection_id: str | None = None, q: str | None = None,
        focus_id: str | None = None, depth: int = 2, limit: int = 180,
        source_limit: int = 40, cursor: str | None = None,
    ) -> dict[str, Any]:
        return _contract_tool_response(workspace_graph(
            store, tenant_id=tenant_id, connection_id=connection_id, q=q, focus_id=focus_id,
            depth=depth, limit=limit, source_limit=source_limit, cursor=cursor,
        ))

    @server.tool(annotations=READ_ONLY_ANNOTATIONS, description="Export the same bounded collective workspace graph as Turtle RDF, preserving source/snapshot bindings and separate candidate/reviewed link states. Matching labels never emit equivalence. Uses the same search, focus and page limits as get_workspace_graph.")
    def get_workspace_ontology(
        connection_id: str | None = None, q: str | None = None,
        focus_id: str | None = None, depth: int = 2, limit: int = 180,
        source_limit: int = 40, cursor: str | None = None,
    ) -> dict[str, Any]:
        return _contract_tool_response(workspace_ontology(workspace_graph(
            store, tenant_id=tenant_id, connection_id=connection_id, q=q, focus_id=focus_id,
            depth=depth, limit=limit, source_limit=source_limit, cursor=cursor,
        )))
