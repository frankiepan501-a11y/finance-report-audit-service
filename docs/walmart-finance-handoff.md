# Walmart finance handoff — 2026-10-09

## 2026-10-10 final index correction

The first real Finance Assistant approval completed aggregation and archive, but the official index still linked the old sheet: `_company_mark_index_finalized` updated status/time only. Walmart finalization now publishes the run's approved `review_url` together with its final status. No other platform's link changes.

For an already archived Walmart run, internal `mode=repair_index` verifies a matching successful human approval in the Finance Assistant audit ledger and writes only the Walmart index link. It does not re-approve, regenerate, aggregate, send messages, or replace the original archive timestamp. It reads back the link and records the previous link plus approval record ID in a separate repair audit; repeated calls with the correct link do not write. The old sheet remains intact.

Verification: `test_walmart_final_index.py` first reproduced the missing URL, then covered approval/version gates, failed writes, Finance identity, and repeated calls. Deployment sentinel: `walmart_final_index_link_20261010=true`. September readback evidence and production correction receipt live under the parent project's `outputs/walmart-2026-09-p0-20261009/closeout/`.

## Identity correction confirmed by user

The previous Event App choice was an implementation error: the user had already assigned Finance work to 财务助手 (`cli_aa1d52694f61dbc1`). For Walmart final review, new cards, contact lookup, ledger operations, callbacks and result PATCH use that App. Old Event App credentials are used only to close its already-issued cards. Other channels remain an explicit migration backlog, not a claim of completed Finance-domain rollout.

Use `/finance-assistant/walmart/review` with the existing internal authorization. Default `mode=preflight` verifies the Finance App's run/gap/audit access, current department members, group and bound sheet without sending. `mode=send` sends the actual ready run to Finance group+members; a clearly marked smoke run goes only to Frankie. `status` reads Finance App messages. `smoke_replay` accepts only a registered isolated Frankie card and never a production run. The existing strictly authenticated `/finance-assistant/r5/callback` handles the new review actions and acknowledges before completing the business operation. A shared service lock serializes send/decision work. Before sends, read state/version again; never reset a human decision.

Delivery receipts now record `app_id`. All prior Event App cards are retired under their actual App, then new cards are issued under the Finance App with stable UUIDs. Old callback routes cannot act on new App cards. If Finance access/configuration fails, fail explicitly; never fall back to the old App for new sends.

The sections below retain the initial recipient/layout change; the Event App identity statement is superseded by this correction.

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
