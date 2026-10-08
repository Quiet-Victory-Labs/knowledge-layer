import assert from 'node:assert/strict';
import test from 'node:test';
import { reviewableProposals, staleApprovedContractProposals } from '../src/reviewStatus.ts';
import type { KnowledgeSnapshot } from '../src/platformApi.ts';

test('review queue contains only statuses accepted by the batch review API', () => {
  const snapshot: KnowledgeSnapshot = { proposals: [
    { id: 'candidate', status: 'candidate' },
    { id: 'pending', status: 'pending' },
    { id: 'pending-review', status: 'pending_review' },
    { id: 'needs-review', status: 'needs_review' },
    { id: 'stale', status: 'stale_review', proposal_type: 'contract_assertion' },
    { id: 'approved', status: 'approved' },
    { id: 'rejected', status: 'rejected' },
    { id: 'missing-status' },
    { id: 'wrong-case', status: 'PENDING' },
  ] };
  assert.deepEqual(reviewableProposals(snapshot).map((item) => item.id),
    ['candidate', 'pending', 'pending-review', 'needs-review']);
  assert.deepEqual(staleApprovedContractProposals(snapshot).map((item) => item.id), ['stale']);
});

test('outdated approved contract assertion is shown for retirement, not batch acceptance', () => {
  const snapshot: KnowledgeSnapshot = { proposals: [
    { id: 'outdated', status: 'approved', proposal_type: 'contract_assertion', effective_on_current_snapshot: false },
    { id: 'current', status: 'approved', proposal_type: 'contract_assertion', effective_on_current_snapshot: true },
  ] };
  assert.deepEqual(reviewableProposals(snapshot), []);
  assert.deepEqual(staleApprovedContractProposals(snapshot).map((item) => item.id), ['outdated']);
});
