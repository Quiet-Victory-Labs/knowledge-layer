"""Bounded hybrid retrieval over source-neutral knowledge metadata."""

from __future__ import annotations

import os
import threading
from bisect import insort
from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor
from functools import lru_cache
from itertools import chain
from typing import Any

from rapidfuzz import fuzz

from knowledge_layer.concept_index import records as concept_records
from knowledge_layer.connectors.common import normalize_name

MODEL_ID = "sentence-transformers/all-MiniLM-L6-v2"
MAX_SEMANTIC_CANDIDATES = 256
_EMBEDDING_LOCK = threading.RLock()
_BUILD_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="knowledge-embedding")
_VECTOR_FUTURES: OrderedDict[tuple[str, ...], Future[Any]] = OrderedDict()


def context_text_match(query: str, *, name: str, text: str) -> dict[str, Any] | None:
    """Reuse lexical ranking for context without asserting paraphrase equivalence.

    Label phrases resolve directly. Definition phrases, reordered tokens
    and simple plurals are suggestions only; every meaningful query token must
    occur in the metadata, including at least one in the label. No model, source
    values, domain synonyms or approved aliases are invented.
    """
    normalized = normalize_name(query)
    label = normalize_name(name)
    content = normalize_name(text)
    if not normalized:
        return None
    if normalized == label:
        return {"match_basis": "exact_name", "score": 1.0, "result_kind": "direct_match"}
    stop_words = {"a", "an", "the", "and", "or", "of", "for", "in", "to", "is", "are",
                  "what", "which", "how", "does", "show", "me"}
    if not set(normalized.split()) - stop_words:
        return None
    if f" {normalized} " in f" {label} ":
        return {"match_basis": "label_phrase", "score": 1.0, "result_kind": "direct_match"}
    if f" {normalized} " in f" {content} ":
        return {"match_basis": "text_phrase", "score": 1.0, "result_kind": "retrieval_candidate",
                "requires_confirmation": True}

    def tokens(value: str) -> set[str]:
        return {word[:-1] if len(word) > 4 and word.endswith("s")
                and not word.endswith(("ss", "us", "is")) else word
                for word in value.split() if word not in stop_words}

    words = tokens(normalized)
    if len(words) < 2 or not words.issubset(tokens(content)) or not words.intersection(tokens(label)):
        return None
    return {"match_basis": "token_overlap", "score": round(fuzz.token_set_ratio(normalized, content) / 100, 4),
            "result_kind": "retrieval_candidate", "matched_tokens": sorted(words),
            "requires_confirmation": True}


def term_context(store: Any, *, tenant_id: str, term: str, connection_id: str | None,
                 concepts: list[dict[str, Any]], include_inventory: bool = True,
                 connection: dict[str, Any] | None = None) -> dict[str, Any]:
    from knowledge_layer.inventory_citations import inventory_citation_operation

    with inventory_citation_operation():
        return _term_context(store, tenant_id=tenant_id, term=term, connection_id=connection_id,
                             concepts=concepts, include_inventory=include_inventory, connection=connection)


def _term_context(store: Any, *, tenant_id: str, term: str, connection_id: str | None,
                  concepts: list[dict[str, Any]], include_inventory: bool = True,
                  connection: dict[str, Any] | None = None) -> dict[str, Any]:
    """Resolve labels and suggest current, authorized metadata across record kinds."""
    from knowledge_layer.contracts import semantic_contract

    direct = []
    candidates = []
    metrics = []
    glossary = []
    upstream = []

    def add_candidate(kind: str, context: dict[str, Any], match: dict[str, Any]) -> None:
        candidates.append({"candidate_type": kind, **match, "context": context})

    for concept in concepts:
        if connection_id and concept["connection_id"] != connection_id:
            continue
        text = " ".join(str(concept.get(key) or "") for key in ("name", "semantic_type", "description"))
        if concept.get("definition_status") == "approved":
            text += " " + str(concept.get("definition") or "")
        match = context_text_match(term, name=str(concept.get("name") or ""), text=text)
        if not match:
            continue
        if match["result_kind"] == "direct_match":
            direct.append((match["match_basis"] != "exact_name", concept))
        else:
            add_candidate("concept", {"concept": concept}, match)

    sources = [connection or store.get_connection(tenant_id, connection_id)] if connection_id else (
        source for source in store.iter_connections(tenant_id)
        if any(isinstance(metric, dict) and metric.get("status") == "approved"
               and context_text_match(term, name=str(metric.get("name") or ""), text=" ".join(
                   str(metric.get(key) or "") for key in ("name", "description", "field")))
               for metric in (source.get("snapshot") or {}).get("metrics", []))
    )
    for source in sources:
        # An inventory-only source has no legacy dataset contract. It may still
        # have independently published local meanings, resolved below.
        if source is None or source.get("snapshot") is None:
            continue
        source_id = source["id"]
        snapshot = source["snapshot"]
        # No approved metric or assertion can contribute to these collections.
        # Avoid assembling unused datasets and repeatedly projecting the source.
        if not (any(isinstance(item, dict) and item.get("status") == "approved"
                    for item in snapshot.get("metrics", []))
                or any(isinstance(item, dict) and item.get("proposal_type") == "contract_assertion"
                       and item.get("status") == "approved" for item in snapshot.get("proposals", []))):
            continue
        contract = semantic_contract(store, tenant_id=tenant_id, connection_id=source_id)
        datasets = {item["asset_id"]: item for item in contract["datasets"]}
        for metric in contract["metrics"]:
            asset = datasets.get(metric.get("source_asset_id"))
            if not asset or metric.get("field") not in {field.get("name") for field in asset["fields"]}:
                continue
            text = " ".join(str(item or "") for item in (
                metric.get("name"), metric.get("description"), metric.get("field")))
            match = context_text_match(term, name=str(metric.get("name") or ""), text=text)
            if not match:
                continue
            context = {**metric, "connection_id": source_id, "source_kind": contract["source_kind"],
                       "snapshot_id": contract["snapshot_id"], "snapshot_version": contract["snapshot_version"],
                       "source_pointer": asset["source_pointer"], "asset_name": asset["name"],
                       "contract_ref": {"connection_id": source_id, "asset_id": asset["asset_id"]}}
            if match["result_kind"] == "direct_match":
                metrics.append({**context, **match})
            else:
                add_candidate("metric", context, match)
        # Supporting glossary stays source-scoped and passes the same current
        # evidence, permission and approval checks as the source contract.
        if connection_id:
            for collection, kind, target in (
                ("cited_glossary", "cited_glossary", glossary),
                ("cited_upstream_definitions", "cited_upstream_definition", upstream),
            ):
                for item in contract[collection]:
                    match = context_text_match(term, name=str(item.get("term") or ""),
                                               text=f"{item.get('term') or ''} {item.get('definition') or ''}")
                    if match:
                        if match["result_kind"] == "direct_match":
                            target.append(item)
                        else:
                            add_candidate(kind, item, match)

    # Multiple field assertions may cite one glossary term. Keep one portable
    # originating assertion per source asset so repeats do not consume the
    # compact suggestion budget; the full contract retains every citation.
    unique_candidates = {}
    for item in candidates:
        detail = item["context"]
        assertion = detail.get("source_assertion") or {}
        concept = detail.get("concept") or {}
        key = (item["candidate_type"], detail.get("connection_id") or concept.get("connection_id"),
               detail.get("id") or detail.get("concept_id") or concept.get("id"), assertion.get("asset_id"))
        unique_candidates.setdefault(key, item)
    candidates = list(unique_candidates.values())
    def candidate_priority(item: dict[str, Any]) -> tuple[Any, ...]:
        detail = item["context"]
        concept = detail.get("concept") or {}
        reviewed = item["candidate_type"] in {"metric", "cited_glossary", "cited_upstream_definition"} or concept.get("definition_status") == "approved"
        return (not reviewed, -item["score"], {"metric": 0, "cited_glossary": 1, "cited_upstream_definition": 1, "concept": 2}[item["candidate_type"]],
                str(detail.get("id") or detail.get("concept_id") or concept.get("id") or ""))

    candidates.sort(key=candidate_priority)
    metrics.sort(key=lambda item: (item["match_basis"] != "exact_name", str(item.get("id") or "")))
    direct.sort(key=lambda item: (item[0], str(item[1].get("id") or "")))
    selected_candidates = candidates[:10]
    # Rank metadata before expensive graph expansion. Broad terms may match
    # thousands of concepts, but only the bounded selected records are read.
    selected_concepts = [concept for _, concept in direct[:10]]
    context_connections = {}
    if selected_concepts or any(item["candidate_type"] == "concept" for item in selected_candidates):
        expanding = selected_concepts + [item["context"]["concept"] for item in selected_candidates
                                        if item["candidate_type"] == "concept"]
        cross_scope_concepts = (
            store._context_cross_source_concepts(tenant_id, expanding)
            if connection_id and hasattr(store, "_context_cross_source_concepts") else
            store.get_concepts(tenant_id) if connection_id else concepts
        )
        ids = list(dict.fromkeys(item["connection_id"] for item in expanding))
        if connection is not None and connection_id in ids:
            context_connections[connection_id] = connection
        context_connections.update(_context_connections(
            store, tenant_id, [source_id for source_id in ids if source_id not in context_connections]))

        def expand(concept: dict[str, Any]) -> dict[str, Any]:
            source = context_connections.get(concept["connection_id"])
            if source is None or _current_concept(source, concept["id"]) != concept:
                from knowledge_layer.store import KnowledgeConflict

                raise KnowledgeConflict("Source context changed during assembly; reload current meaning")
            return store._context_for_concept(tenant_id, concept, cross_scope_concepts, source)

        selected_concepts = [expand(concept) for concept in selected_concepts]
        for item in selected_candidates:
            if item["candidate_type"] == "concept":
                item["context"] = expand(item["context"]["concept"])
    result = {"tenant_id": tenant_id, "term": term, **({"connection_id": connection_id} if connection_id else {}),
            "matches": selected_concepts, "total_matches": len(direct), "more_available": len(direct) > 10,
            "metric_matches": metrics[:10], "metric_total_matches": len(metrics), "metric_more_available": len(metrics) > 10,
            "cited_glossary": glossary[:10], "cited_glossary_total_matches": len(glossary),
            "cited_glossary_more_available": len(glossary) > 10,
            "cited_upstream_definitions": upstream[:10], "cited_upstream_definitions_total_matches": len(upstream),
            "cited_upstream_definitions_more_available": len(upstream) > 10,
            "retrieval_candidates": selected_candidates, "retrieval_candidate_total_matches": len(candidates),
            "retrieval_candidate_more_available": len(candidates) > 10,
            "retrieval_policy": "Text and token suggestions are relevance matches, not approved aliases or equivalence. Descriptions may exclude the requested meaning. Confirm scope and read the current asset contract before querying."}
    if include_inventory and hasattr(store, "inventory_concept_proposals"):
        from knowledge_layer.inventory_concept_search import (
            InventoryConceptSearch,
            _check_response,
            resolve_inventory_term,
        )

        inventory = resolve_inventory_term(store, tenant_id, term, connection_id, recheck=False)
        # Preserve conflicts as distinct source-scoped meanings, never merge by label.
        result["matches"] = (inventory["matches"] + result["matches"])[:10]
        result["total_matches"] += inventory["total_matches"]
        result["more_available"] = result["total_matches"] > 10
        result["retrieval_candidates"] = (
            inventory["retrieval_candidates"] + result["retrieval_candidates"])[:10]
        result["retrieval_candidate_total_matches"] += inventory["retrieval_candidate_total_matches"]
        result["retrieval_candidate_more_available"] = result["retrieval_candidate_total_matches"] > 10
        if inventory["inventory_present"]:
            for key in ("inventory_candidate_limit_reached", "inventory_candidate_digest", "inventory_candidates_checked",
                        "inventory_search_complete", "inventory_present", "counts_basis"):
                result[key] = inventory[key]
        packets = [item for item in result["matches"] if item.get("basis") == "inventory"]
        packets += [item["context"] for item in result["retrieval_candidates"]
                    if item["candidate_type"] == "inventory_concept"]
        if inventory["inventory_present"]:
            _check_response(result)
        InventoryConceptSearch(store).recheck(tenant_id, packets)
    _recheck_context_sources(store, tenant_id, context_connections, result)
    return result


def _context_connections(store: Any, tenant_id: str, ids: list[str]) -> dict[str, dict[str, Any]]:
    """Read a bounded request-local batch; never reuse state between requests."""
    if hasattr(store, "_context_connections"):
        return store._context_connections(tenant_id, ids)
    return {source_id: source for source_id in ids
            if (source := store.get_connection(tenant_id, source_id)) is not None}


def _current_concept(source: dict[str, Any], concept_id: str) -> dict[str, Any] | None:
    for item in (source.get("snapshot") or {}).get("concepts", []):
        if isinstance(item, dict) and str(item.get("id") or item.get("concept_id") or "") == concept_id:
            return {**item, "id": concept_id, "connection_id": source["id"], "connection_name": source["name"]}
    return None


def _recheck_context_sources(store: Any, tenant_id: str, observed: dict[str, dict[str, Any]],
                             result: dict[str, Any]) -> None:
    """Refuse stale context and recheck every returned cross-source suggestion."""
    from knowledge_layer.store import KnowledgeConflict

    candidates: dict[str, list[tuple[dict[str, Any], Any]]] = {}
    packets = result["matches"] + [item["context"] for item in result["retrieval_candidates"]
                                    if item["candidate_type"] == "concept"]
    for packet in packets:
        for candidate in packet.get("cross_source_candidates", []):
            candidates.setdefault(candidate["connection_id"], []).append(
                (candidate, packet["concept"].get("semantic_type")))
    ids = list(dict.fromkeys([*observed, *candidates]))
    for offset in range(0, len(ids), 20):
        current = _context_connections(store, tenant_id, ids[offset:offset + 20])
        for source_id in ids[offset:offset + 20]:
            source = current.get(source_id)
            previous = observed.get(source_id)
            if source is None or (previous is not None and any(
                source.get(key) != previous.get(key) for key in (
                    "name", "kind", "status", "config", "last_error", "last_refreshed_at",
                    "last_refresh_attempt_at", "last_complete_refresh_at", "snapshot"))):
                raise KnowledgeConflict("Source context changed during assembly; reload current meaning")
            for candidate, semantic_type in candidates.get(source_id, []):
                concept = _current_concept(source, candidate["concept_id"])
                if (concept is None or concept.get("name") != candidate.get("name")
                        or concept.get("connection_name") != candidate.get("connection_name")
                        or concept.get("semantic_type") != semantic_type):
                    raise KnowledgeConflict("Source context changed during assembly; reload current meaning")


@lru_cache(maxsize=1)
def _model():
    from fastembed import TextEmbedding

    return TextEmbedding(
        model_name=MODEL_ID,
        cache_dir=os.getenv("KNOWLEDGE_EMBEDDING_CACHE", ".local/fastembed"),
    )


def _vectors(texts: tuple[str, ...]):
    # Cold model loading runs once in the background. Search immediately falls
    # back to lexical ranking while a new source corpus is indexed.
    with _EMBEDDING_LOCK:
        future = _VECTOR_FUTURES.get(texts)
        if future is None:
            # A bounded dictionary alone does not bound the executor queue:
            # evicted Futures still retain arguments and scheduled work. Keep
            # every unfinished build in a slot; only completed entries can be
            # evicted. Saturated searches retain immediate lexical fallback.
            if len(_VECTOR_FUTURES) >= 8:
                completed = next((key for key, value in _VECTOR_FUTURES.items() if value.done()), None)
                if completed is None:
                    return None
                del _VECTOR_FUTURES[completed]
            future = _BUILD_POOL.submit(_vectors_cached, texts)
            _VECTOR_FUTURES[texts] = future
        _VECTOR_FUTURES.move_to_end(texts)
    return future.result() if future.done() else None


@lru_cache(maxsize=8)
def _vectors_cached(texts: tuple[str, ...]):
    import numpy as np

    vectors = np.asarray(list(_model().embed(list(texts))), dtype=np.float32)
    lengths = np.linalg.norm(vectors, axis=1, keepdims=True)
    return vectors / np.maximum(lengths, 1e-12)


def _semantic_scores(texts: tuple[str, ...], query: str) -> list[float] | None:
    try:
        import numpy as np

        vectors = _vectors(texts)
        if vectors is None:
            return None
        query_vector = np.asarray(next(iter(_model().query_embed(query))), dtype=np.float32)
        query_vector /= max(float(np.linalg.norm(query_vector)), 1e-12)
        return [float(value) for value in vectors @ query_vector]
    except (ImportError, OSError, RuntimeError, ValueError):
        # Metadata search still works if the optional local embedding model is absent.
        return None


def _connection_records(connection: dict[str, Any]):
    snapshot = connection.get("snapshot") or {}
    for record in concept_records(snapshot):
        yield {**record, "connection_id": connection["id"],
               "connection_name": connection["name"], "snapshot_version": snapshot.get("version")}


def _records(store: Any, tenant_id: str):
    """Compatibility path for lightweight injected stores used by local tests."""
    for connection in store.iter_connections(tenant_id):
        yield from _connection_records(connection)


def search_knowledge(store: Any, *, tenant_id: str, query: str, limit: int = 5) -> dict[str, Any]:
    from knowledge_layer.inventory_citations import inventory_citation_operation

    with inventory_citation_operation():
        return _search_knowledge(store, tenant_id=tenant_id, query=query, limit=limit)


def _search_knowledge(store: Any, *, tenant_id: str, query: str, limit: int) -> dict[str, Any]:
    """Rank metadata only; a result is never an approved semantic assertion."""
    query = query.strip()
    if not 1 <= len(query) <= 200:
        raise ValueError("query must be between 1 and 200 characters")
    limit = max(1, min(int(limit), 10))
    selected: list[tuple[tuple[float, str, int], float, dict[str, Any]]] = []
    scanned = 0
    indexed = hasattr(store, "search_concept_candidates")
    inventory_search = None
    inventory_metadata = {"items": [], "limit_reached": False}
    if hasattr(store, "inventory_concept_proposals"):
        from knowledge_layer.inventory_concept_search import InventoryConceptSearch

        inventory_search = InventoryConceptSearch(store)
        inventory_metadata = inventory_search.candidates(tenant_id, query)
    legacy_budget = MAX_SEMANTIC_CANDIDATES - len(inventory_metadata["items"])
    if indexed:
        candidates = store.search_concept_candidates(tenant_id, query, limit=legacy_budget) if legacy_budget else []
        index_limit_reached = len(candidates) >= legacy_budget if legacy_budget else True
        candidates = candidates[:legacy_budget]
        records = (
            {"concept_id": row["concept_id"], "name": row["name"],
             "definition": row["definition"], "semantic_type": row["semantic_type"],
             "trust_state": row["trust_state"], "connection_id": row["connection_id"],
             "connection_name": row["connection_name"],
             "snapshot_version": row["snapshot_version"], "evidence_ids": row["evidence_ids"],
             "evidence": row["evidence"], "text": row["search_text"]}
            for row in candidates
        )
    else:
        index_limit_reached = False
        records = _records(store, tenant_id)
    records = chain(records, (
        {**row, "concept_id": row["id"], "basis": "inventory",
         "text": f"{row['name']} {row['definition']}"}
        for row in inventory_metadata["items"]))
    index_limit_reached = index_limit_reached or inventory_metadata["limit_reached"]
    for record in records:
        scanned += 1
        lexical = fuzz.token_set_ratio(query, record["text"]) / 100
        if query.casefold() == record["name"].casefold():
            lexical = 1.0
        insort(selected, ((-lexical, record["concept_id"], scanned), lexical, record))
        if len(selected) > MAX_SEMANTIC_CANDIDATES:
            selected.pop()
    if not selected:
        return {"query": query, "search_mode": "lexical_fallback", "results": [],
                "indexed_concepts": 0, "scanned_concepts": 0,
                "semantic_candidates": 0, "semantic_candidate_limit_reached": index_limit_reached,
                "index_limit_reached": index_limit_reached,
                "inventory_candidate_limit_reached": inventory_metadata["limit_reached"],
                "inventory_candidates_checked": len(inventory_metadata["items"]),
                "inventory_search_complete": not inventory_metadata["limit_reached"],
                "counts_basis": "current_authorized_bounded_metadata_candidates" if inventory_search else "legacy_metadata"}

    texts = tuple(record["text"] for _, _, record in selected)
    semantic = _semantic_scores(texts, query)
    ranked = []
    for index, (_, lexical, record) in enumerate(selected):
        semantic_score = max(0.0, semantic[index]) if semantic is not None else 0.0
        score = 0.55 * semantic_score + 0.45 * lexical if semantic is not None else lexical
        ranked.append((score, lexical, semantic_score, record))
    ranked.sort(key=lambda item: (-item[0], item[3]["concept_id"]))
    results = []
    projected: dict[str, dict[str, dict[str, Any]]] = {}
    if indexed:
        source_ids = list(dict.fromkeys(record["connection_id"] for _, _, _, record in ranked[:limit]
                                       if record.get("basis") != "inventory"))
        current_sources = _context_connections(store, tenant_id, source_ids)
        projected = {source_id: {item["concept_id"]: item for item in _connection_records(source)}
                     for source_id, source in current_sources.items()}
    inventory_packets = []
    for score, lexical, semantic_score, record in ranked[:limit]:
        if record.get("basis") == "inventory":
            packet = inventory_search.resolve(tenant_id, record)
            inventory_packets.append(packet)
            result = {"concept_id": packet["id"], "name": packet["concept"]["name"],
                "definition": packet["concept"]["definition"], "semantic_type": packet["concept"]["semantic_type"],
                "trust_state": packet["concept"]["trust_state"], "connection_id": record["connection_id"],
                "connection_name": record["connection_name"], "basis": "inventory", "context": packet,
                **{key: packet[key] for key in ("proposal_digest", "evidence_ref", "entity_digest",
                    "evidence_digest", "publication_state", "publication", "native_meaning_certified", "locally_executable")}}
        elif indexed:
            source_id = record["connection_id"]
            current = projected.get(source_id, {}).get(record["concept_id"])
            # The grant or source version may change after candidate lookup.
            # Never fall back to the cached index row when the final read fails.
            if current is None or current["snapshot_version"] != record["snapshot_version"]:
                continue
            result = {key: value for key, value in current.items() if key != "text"}
        else:
            result = {key: value for key, value in record.items() if key != "text"}
        result.update(
            {
                "score": round(score, 4),
                "lexical_similarity": round(lexical, 4),
                "semantic_similarity": round(semantic_score, 4) if semantic is not None else None,
                "result_kind": "retrieval_candidate",
            }
        )
        results.append(result)
    result = {
        "query": query,
        "search_mode": (
            "indexed_lexical_semantic_rerank" if indexed and semantic is not None
            else "indexed_lexical" if indexed
            else "lexical_prefilter_semantic_rerank" if semantic is not None and scanned > len(selected)
            else "hybrid_semantic_lexical" if semantic is not None else "lexical_fallback"
        ),
        "model": MODEL_ID if semantic is not None else None,
        "indexed_concepts": scanned,
        "scanned_concepts": scanned,
        "semantic_candidates": len(selected),
        "semantic_candidate_limit_reached": index_limit_reached if indexed else scanned > len(selected),
        "index_limit_reached": index_limit_reached,
        "inventory_candidate_limit_reached": inventory_metadata["limit_reached"],
        "inventory_candidates_checked": len(inventory_metadata["items"]),
        "inventory_search_complete": not inventory_metadata["limit_reached"],
        "counts_basis": "current_authorized_bounded_metadata_candidates" if inventory_search else "legacy_metadata",
        "results": results,
    }
    if inventory_search:
        from knowledge_layer.inventory_concept_search import _check_response

        _check_response(result)
        inventory_search.recheck(tenant_id, inventory_packets)
    return result
