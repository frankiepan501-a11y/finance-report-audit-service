# Walmart finance handoff — 2026-10-09

## Scope and contract

User requested Walmart final-review cards to the finance group `oc_6b2da626d80eb6284bbe9dcf895030b9` plus current Finance department employees. Do not add Frankie separately, hardcode employee names, migrate Apps, or change other platforms' routes. The existing Event App sends, receives callbacks, and updates cards. Financial approval remains a human decision.

Reuse the approved Amazon review layout: platform/shop/month/status; financial summary; AI findings; review focus; report link; horizontal approve/return buttons. Walmart labels distinguish inbound freight from fulfillment charges. Report version and same-review notice are displayed.

## Implementation

- Production Walmart finance sends resolve Finance department members and their union IDs at send time. Missing identities stop dispatch. Frankie-only test mode and other platform routing remain unchanged.
- The run payload `walmart_finance_delivery` retains each destination/message ID and review key. Existing cards are updated; departed recipients' cards are closed. A stable send UUID guards ambiguous send retries. Existing `原卡message_ids` are retained.
- Callback checks current recipient card, report version, review state and current Finance membership. After a decision, current group and employee cards show the same result. No user is allowed to approve from a retired card.
- Scope is the existing single-worker service (`Procfile`). This change does not introduce a distributed approval coordinator.

## Verification and rollout

Run `python -m unittest discover` with the project's dependencies. Tests prohibit unmocked HTTP and cover department membership, routing isolation, replay/update behavior, old-card retirement, receipt failures, stale/unauthorized decisions, result synchronization and card structure.

Deploy from the recorded Git revision. Health sentinel: `walmart_finance_group_department_20261009=true`. For the September V3 handoff, seed the existing three delivery receipts from verified messages before reconciling: reuse both Finance employee cards, close the extra Frankie card, create only the group card. Verify actual messages and run state after sending. Never repeat a send after an ambiguous response without examining stored receipts.

Rollback code to the prior commit if needed; retain the delivery ledger and original financial evidence. Do not restore active buttons on retired cards or revert a real Finance decision. No report amounts or official index entries are changed by routing migration.
