"""Lexical fallback improves recall without dropping request constraints."""

from types import SimpleNamespace

import pytest

from knowledge_layer.access import Principal, principal_scope
from knowledge_layer.contracts import search_document_evidence
from knowledge_layer.store import KnowledgeStore


def _store(*passages):
    evidence = [{
        "id": f"passage-{index}", "evidence_type": "document_derived_term",
        "asset_id": "policy", "source_pointer": "policy.md", "summary": "Policy",
        "details": {"source_excerpt": text, "source_revision": "revision-1",
                    "source_locator": {"line_start": index + 1},
                    "extraction_complete": True, "retention_complete": True},
    } for index, text in enumerate(passages)]
    return SimpleNamespace(iter_current_evidence_sources=lambda _: iter([{
        "connection_id": "source", "connection_name": "Policies", "snapshot_id": "snapshot-1",
        "evidence": evidence,
    }]))


def test_partial_evidence_is_opt_in_labeled_and_preserves_citation():
    store = _store("Account identity engine.", "Google identity tokens authenticate requests.")
    request = {"tenant_id": "test", "query": "account identity interpretation"}
    assert search_document_evidence(store, **request)["matches"] == []
    result = search_document_evidence(store, **request, match_mode="ranked")
    assert result["total_matches"] == 1
    assert result["search_mode"] == "lexical_ranked_partial_terms"
    assert result["match_mode"] == "ranked"
    match = result["matches"][0]
    assert match["match_type"] == "partial"
    assert match["matched_terms"] == ["account", "identity"]
    assert match["unmatched_terms"] == ["interpretation"]
    assert match["excerpt"] == "Account identity engine."
    assert match["source_revision"] == "revision-1"
    assert match["source_locator"] == {"line_start": 1}
    assert match["evidence_ref"] == {
        "connection_id": "source", "snapshot_id": "snapshot-1", "evidence_id": "passage-0",
    }


@pytest.mark.parametrize("full_first", [False, True])
def test_any_full_match_discards_all_partial_fallback_candidates(full_first):
    passages = ["Account identity engine.", "Account identity interpretation policy."]
    if full_first:
        passages.reverse()
    result = search_document_evidence(_store(*passages), tenant_id="test",
                                     query="account identity interpretation", match_mode="ranked")
    assert result["total_matches"] == 1
    assert len(result["matches"]) == 1
    assert result["matches"][0]["excerpt"] == "Account identity interpretation policy."
    assert result["matches"][0]["match_type"] == "full"
    assert result["matches"][0]["unmatched_terms"] == []
    assert result["search_mode"] == "lexical_all_query_terms"


def test_two_term_identity_query_never_falls_back_to_a_generic_single_term():
    result = search_document_evidence(_store("Google identity tokens authenticate requests."),
                                     tenant_id="test", query="account identity", match_mode="ranked")
    assert result["matches"] == []
    assert result["total_matches"] == 0


def test_ranked_candidates_need_two_thirds_of_all_meaningful_terms():
    result = search_document_evidence(_store("Account identity policy."), tenant_id="test",
                                     query="account identity lifecycle verification", match_mode="ranked")
    assert result["matches"] == []


@pytest.mark.parametrize("constraint", [
    "not", "no", "never", "without", "except", "cannot", "can", "should", "must",
    "42", "7.5", "CAN", "SHOULD", "account_id", "accountID", "account-id", "kg/day",
    '"interpretation"', "'interpretation'", "`interpretation`", "“interpretation”", "‘interpretation’",
    '"the"', "don't", "can’t",
])
def test_partial_fallback_cannot_omit_protected_constraints(constraint):
    result = search_document_evidence(_store("Account identity policy."), tenant_id="test",
                                     query=f"account identity {constraint}", match_mode="ranked")
    assert result["matches"] == []


@pytest.mark.parametrize("query,passage", [
    ('"CAN" account identity interpretation', "CAN account identity policy."),
    ("SHOULD account identity interpretation", "SHOULD account identity policy."),
    ("account_id identity interpretation", "account_id identity policy."),
    ("account identity interpretation 42", "account identity 42 policy."),
    ("not account identity interpretation", "not account identity policy."),
])
def test_present_protected_terms_allow_an_explicit_partial_candidate(query, passage):
    result = search_document_evidence(_store(passage), tenant_id="test", query=query, match_mode="ranked")
    assert result["total_matches"] == 1
    assert result["matches"][0]["match_type"] == "partial"
    assert result["matches"][0]["unmatched_terms"] == ["interpretation"]


@pytest.mark.parametrize("query,passage", [
    ("billing policy 100 usd", "Billing policy is 100 eur."),
    ("billing policy usd", "Billing policy is eur."),
    ("cargo policy 100 kg", "Cargo policy is 100 lb."),
    ("cargo policy 100 widgets", "Cargo policy is 100 crates."),
])
def test_matching_amount_cannot_hide_a_missing_currency_or_unit(query, passage):
    result = search_document_evidence(_store(passage), tenant_id="test", query=query, match_mode="ranked")
    assert result["matches"] == []


def test_matching_amount_and_currency_allow_only_the_unprotected_fallback():
    result = search_document_evidence(_store("Billing policy is 100 usd."), tenant_id="test",
                                     query="billing policy 100 usd interpretation", match_mode="ranked")
    assert result["matches"][0]["match_type"] == "partial"
    assert result["matches"][0]["unmatched_terms"] == ["interpretation"]


@pytest.mark.parametrize("query", [
    "account identity don’t “interpretation”",
    "account identity don’t ‘interpretation’",
    "account identity ‘don't interpretation’",
    "account identity ‘don’t interpretation’",
])
def test_smart_quoted_constraints_survive_adjacent_or_enclosed_contractions(query):
    result = search_document_evidence(_store("Account identity don't policy."),
                                     tenant_id="test", query=query, match_mode="ranked")
    assert result["matches"] == []


@pytest.mark.parametrize("contraction", ["don't", "don’t"])
def test_smart_single_quotes_close_after_a_contraction_not_at_its_apostrophe(contraction):
    result = search_document_evidence(_store("Account identity don't policy."), tenant_id="test",
                                     query=f"‘account {contraction}’ identity interpretation", match_mode="ranked")
    assert result["total_matches"] == 1
    assert result["matches"][0]["match_type"] == "partial"
    assert result["matches"][0]["unmatched_terms"] == ["interpretation"]


def test_ranked_fallback_uses_granted_current_snapshots_only():
    store = KnowledgeStore(database_url="sqlite:///:memory:")
    try:
        granted = store.create_connection("test", kind="local_files", name="Public policy", config={})
        hidden = store.create_connection("test", kind="local_files", name="Private policy", config={})
        for source, passage in ((granted, "Account identity engine."),
                                (hidden, "Account identity interpretation.")):
            evidence = next(_store(passage).iter_current_evidence_sources("test"))["evidence"]
            store.save_snapshot("test", source["id"], {
                "status": "discovery_complete", "discovery_complete": True, "evidence": evidence,
            })
        store.grant_source_access("test", granted["id"], principal_type="user", principal_id="reader", role="read")
        store.access_enforced = True
        with principal_scope(Principal("reader", frozenset())):
            result = search_document_evidence(store, tenant_id="test",
                                             query="account identity interpretation", match_mode="ranked")
        assert result["total_matches"] == 1
        assert result["matches"][0]["connection_id"] == granted["id"]
        assert result["matches"][0]["match_type"] == "partial"
        assert result["document_evidence_scanned"] == 1
        store.access_enforced = False
        store.save_snapshot("test", granted["id"], {
            "status": "discovery_complete", "discovery_complete": True,
            "evidence": next(_store("Equipment maintenance.").iter_current_evidence_sources("test"))["evidence"],
        })
        store.access_enforced = True
        with principal_scope(Principal("reader", frozenset())):
            result = search_document_evidence(store, tenant_id="test",
                                             query="account identity interpretation", match_mode="ranked")
        assert result["matches"] == []
        assert result["total_matches"] == 0
    finally:
        store.close()


@pytest.mark.parametrize("query", ['"tell me about" account identity', "CAN account identity",
                                        "SHOULD account identity"])
def test_prefix_handling_does_not_strip_quoted_phrases_or_domain_modals(query):
    result = search_document_evidence(_store("Account identity policy."), tenant_id="test", query=query)
    assert result["matches"] == []


def test_ranked_matches_are_bounded_and_coverage_stays_honest():
    store = _store("Account identity policy.", "Account identity engine.", "Other account identity.")
    result = search_document_evidence(store, tenant_id="test", query="account identity interpretation",
                                     limit=2, match_mode="ranked")
    assert result["total_matches"] == 3
    assert len(result["matches"]) == 2
    assert result["document_evidence_scanned"] == 3
    assert result["unknown_text_coverage_evidence"] == result["incomplete_text_evidence"] == 0
    assert "missing match is not proof" in result["search_scope"]


@pytest.mark.parametrize("match_mode", ["semantic", "partial", None, []])
def test_invalid_match_mode_refuses_before_any_source_read(match_mode):
    def refuse_read(_tenant):
        raise AssertionError("invalid mode must not read source evidence")
    with pytest.raises(ValueError, match="match_mode"):
        search_document_evidence(SimpleNamespace(iter_current_evidence_sources=refuse_read),
                                 tenant_id="test", query="account identity", match_mode=match_mode)
