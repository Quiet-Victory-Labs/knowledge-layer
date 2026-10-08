import type { CurationRecord, KnowledgeSnapshot } from './platformApi';

const REVIEWABLE = new Set(['candidate', 'pending', 'pending_review', 'needs_review']);

export function reviewableProposals(snapshot: KnowledgeSnapshot | null | undefined): CurationRecord[] {
  return (snapshot?.proposals ?? []).filter((proposal) => REVIEWABLE.has(proposal.status ?? ''));
}

export function staleApprovedContractProposals(snapshot: KnowledgeSnapshot | null | undefined): CurationRecord[] {
  return (snapshot?.proposals ?? []).filter((proposal) =>
    proposal.proposal_type === 'contract_assertion' &&
    (proposal.status === 'stale_review' || (proposal.status === 'approved' && proposal.effective_on_current_snapshot === false)));
}
