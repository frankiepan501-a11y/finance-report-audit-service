import copy
import json
import unittest
from unittest.mock import patch, Mock
import app
import walmart_monthly as m


def run(state="AI生成中", **payload):
    return {"record_id":"r", "fields":{"run_id":"company-profit-2026-10-walmart", "期间":"2026-10",
        "报表状态":state, "报表链接":"https://u1wpma3xuhr.feishu.cn/sheets/TestDraft",
        "payload_json":json.dumps({"platform_id":"walmart", "report_period":"2026-10",
            "monthly_job":{"id":"job", "status":"generating", "started_ms":1}, **payload})}}


class MonthlyTests(unittest.TestCase):
    def setUp(self):
        check=patch.object(m,'check_sheet',return_value=([], 'checked-sheet'))
        check.start();self.addCleanup(check.stop)
    def test_approved_month_begin_does_not_write_or_generate(self):
        with patch.object(m,'find',return_value=run('已归档')), patch.object(m,'save') as save:
            self.assertFalse(m.begin(app,'FIN','2026-10')['generate'])
            save.assert_not_called()

    def test_ready_month_begin_does_not_regenerate(self):
        with patch.object(m,'find',return_value=run('待财务终审')), patch.object(m,'save') as save:
            self.assertFalse(m.begin(app,'FIN','2026-10')['generate'])
            save.assert_not_called()

    def test_concurrent_generation_is_skipped(self):
        row=run(monthly_job={'status':'generating','started_ms':100})
        with patch.object(m,'find',return_value=row),patch.object(app,'_now_ms',return_value=1000):
            self.assertFalse(m.begin(app,'FIN','2026-10')['generate'])

    def test_approved_register_does_not_change_review_hash(self):
        row=run('已归档',source_hash='approved')
        with patch.object(m,'patch_run') as save:
            self.assertEqual(m.register(app,'FIN',row,{'source_hash':'different'})['status'],'closed_preserved')
            save.assert_not_called()

    def test_old_job_cannot_replace_candidate(self):
        with self.assertRaises(app.HTTPException):
            m.register(app,'FIN',run(),{'job_id':'old'})

    def test_unreadable_report_cannot_pass_audit(self):
        row=run(monthly_validation={'version':m.VERSION,'errors':[]})
        gap={'fields':{'gap_id':'g'}}
        with patch.object(app,'_company_report_summary',return_value={'ok':False}), \
             patch.object(m,'issue') as issue,patch.object(m,'open_gaps',return_value=[gap]), \
             patch.object(m,'patch_run',return_value=row),patch.object(m,'send_gaps',return_value={'status':'p0_gap'}), \
             patch.object(app,'_company_send_walmart_finance') as send:
            self.assertEqual(m.finish(app,'FIN',row)['status'],'p0_gap')
            issue.assert_called_once();send.assert_not_called()

    def test_clean_month_automatically_sends_finance(self):
        row=run(monthly_validation={'version':m.VERSION,'errors':[]})
        with patch.object(app,'_company_report_summary',return_value={'ok':True}), \
             patch.object(app,'_company_apply_report_audit_gate',return_value=row), \
             patch.object(m,'find',return_value=None),patch.object(m,'open_gaps',return_value=[]), \
             patch.object(m,'patch_run',return_value=row),patch.object(app,'_company_finance_card',return_value={}), \
             patch.object(app,'_company_send_walmart_finance',return_value=[]) as send:
            self.assertEqual(m.finish(app,'FIN',row)['status'],'finance_ready')
            self.assertEqual(send.call_args.args[:2],('FIN','FIN'))
            self.assertEqual(send.call_args.kwargs['recipient_mode'],'finance_gray')

    def test_recovery_rechecks_queued_manual_correction(self):
        row=run('待AI重跑',monthly_job={'status':'queued'},monthly_validation={'version':m.VERSION})
        with patch.object(m,'records',return_value=[row]),patch.object(app,'_last_month',return_value='2026-10'), \
             patch.object(m,'finish',return_value={'status':'finance_ready'}) as finish:
            m.recover(app,'FIN');finish.assert_called_once()

    def test_recovery_preserves_archived_month(self):
        row=run('已归档')
        with patch.object(m,'records',return_value=[row]),patch.object(app,'_last_month',return_value='2026-10'), \
             patch.object(m,'finish') as finish,patch.object(m,'trigger') as trigger, \
             patch.object(app,'_company_aggregate_run') as aggregate:
            m.recover(app,'FIN');finish.assert_not_called();trigger.assert_not_called();aggregate.assert_not_called()

    def test_resume_rejects_unauthorized_finance_identity(self):
        row=run('P0待处理',walmart_gap_delivery={'targets':{'a':{'message_id':'m','gap_key':'k','active':True}}})
        body={'value':{'run_id':'company-profit-2026-10-walmart','gap_key':'k'},'message_id':'m','operator_open_id':'outsider'}
        with patch.object(app,'finance_assistant_tok',return_value='FIN'),patch.object(m,'find',return_value=row), \
             patch.object(app,'_contact_user',return_value={'union_id':'outside'}), \
             patch.object(app,'_company_walmart_finance_recipients',return_value=[{'type':'union_id','id':'finance'}]), \
             patch.object(m,'save') as save:
            self.assertTrue(m.resume(app,body)['ignored']);save.assert_not_called()

    def test_gap_card_form_has_one_submit_and_no_action_wrapper(self):
        card=m.gap_card(app,run(),[{'fields':{'缺口说明':'测试：缺当月汇率'}}],'k')
        form=next(e for e in card['elements'] if e['tag']=='form')
        self.assertEqual(len([e for e in form['elements'] if e.get('action_type')=='form_submit']),1)
        self.assertFalse(any(e['tag']=='action' for e in form['elements']))

    def test_read_error_is_not_empty_table(self):
        response=Mock();response.json.return_value={'code':99991672}
        with patch.object(app.requests,'request',return_value=response):
            with self.assertRaises(app.HTTPException):m.records(app,'FIN',app.COMPANY_RUN_TBL)

    def test_api_signature_is_not_source_identity(self):
        row=run('已归档')
        with patch.object(app,'_company_update_run') as write:
            result=app._company_audit_run_cycle('FIN',row)
            self.assertEqual(result['status'],'closed_preserved');write.assert_not_called()

    def test_draft_never_refreshes_from_official_index(self):
        row=run()
        with patch.object(app,'_company_report_link') as index:
            self.assertEqual(app._company_refresh_report_link('FIN',row),row);index.assert_not_called()

    def test_seed_write_interrupted_is_recovered(self):
        row=run('',monthly_job={})
        with patch.object(m,'trigger',return_value={'status':'generation_requested'}) as trigger:
            m.recover_one(app,'FIN',row);trigger.assert_called_once_with(app,'2026-10')

    def test_one_bad_month_cannot_starve_the_next(self):
        rows=[run('P0待处理'),run('AI初审中')]
        with patch.object(m,'records',return_value=rows),patch.object(app,'_last_month',return_value='2026-10'), \
             patch.object(m,'recover_one',side_effect=[RuntimeError('simulated'),{'status':'finance_ready'}]) as work:
            result=m.recover(app,'FIN')
            self.assertEqual(work.call_count,2)
            self.assertEqual(result['results'][-1]['status'],'finance_ready')

    def test_source_outage_is_retried_without_human_click(self):
        row=run('P0待处理',monthly_job={'status':'blocked','retry_generation':True})
        with patch.object(m,'trigger',return_value={'status':'generation_requested'}) as trigger:
            m.recover_one(app,'FIN',row);trigger.assert_called_once()

    def test_changed_sheet_rotates_review_key(self):
        row=run(monthly_validation={'version':m.VERSION,'errors':[]}, reviewed_sheet_hash='old', review_version='old')
        with patch.object(app,'_company_report_summary',return_value={'ok':True}), \
             patch.object(app,'_company_apply_report_audit_gate',return_value=row), \
             patch.object(m,'find',return_value=None),patch.object(m,'open_gaps',return_value=[]), \
             patch.object(m,'patch_run',return_value=row) as save,patch.object(app,'_company_finance_card',return_value={}), \
             patch.object(app,'_company_send_walmart_finance',return_value=[]):
            m.finish(app,'FIN',row)
            self.assertNotEqual(save.call_args.kwargs.get('review_version'),'old')

    def test_source_errors_cannot_be_bypassed_by_recheck(self):
        row=run('P0待处理', monthly_validation={'errors':['API unavailable']},
            walmart_gap_delivery={'targets':{'a':{'message_id':'m','gap_key':'k','active':True}}})
        body={'value':{'run_id':'company-profit-2026-10-walmart','gap_key':'k'},'message_id':'m','operator_open_id':'finance',
              'card_form_value':{'reason':'已修改','resolution':'recheck'}}
        with patch.object(app,'finance_assistant_tok',return_value='FIN'),patch.object(m,'find',return_value=row), \
             patch.object(app,'_contact_user',return_value={'union_id':'finance'}), \
             patch.object(app,'_company_walmart_finance_recipients',return_value=[{'type':'union_id','id':'finance'}]), \
             patch.object(m,'open_gaps',return_value=[]),patch.object(app,'_patch_message_card',return_value={'code':0}), \
             patch.object(m,'save') as save:
            self.assertFalse(m.resume(app,body)['ok']);save.assert_not_called()

    def test_financial_sheet_edit_after_approval_blocks_aggregation(self):
        row=run('财务通过',monthly_validation={'version':m.VERSION}, monthly_approval={'sheet_hash':'approved'})
        with patch.object(app,'_bt_find',return_value=row):
            result=app._company_aggregate_gate('FIN','2026-10','沃尔玛毛利报表')
            self.assertFalse(result['allow']);self.assertFalse(result['delete_stale'])

    def test_manual_rerun_also_preserves_approved_month(self):
        with patch.object(app,'_bt_find',return_value=run('已归档')),patch.object(app,'_company_update_run') as write:
            self.assertEqual(app._company_rerun_run('FIN','id')['status'],'closed_preserved')
            write.assert_not_called()

    def test_legacy_seed_preserves_existing_walmart(self):
        row=run('已归档')
        with patch.object(app,'_bt_find',return_value=row),patch.object(app,'_bt_write') as write:
            self.assertEqual(app._company_seed_run('FIN','2026-10','walmart'),row)
            write.assert_not_called()

    def test_missing_month_trigger_failure_does_not_starve_old_month(self):
        row=run('AI初审中')
        with patch.object(m,'records',return_value=[row]),patch.object(app,'_last_month',return_value='2026-09'), \
             patch.object(m,'trigger',side_effect=RuntimeError()),patch.object(m,'recover_one',return_value={'ok':True}) as recover:
            m.recover(app,'FIN');recover.assert_called_once()

    def test_missing_cost_gap_write_receipt_blocks_finance(self):
        row=run(monthly_validation={'version':m.VERSION,'errors':[]})
        with patch.object(app,'_company_report_summary',return_value={'ok':True,'summary':{'cm_n':1}}), \
             patch.object(app,'_company_apply_report_audit_gate',return_value=row), \
             patch.object(m,'find',return_value=None),patch.object(m,'open_gaps',return_value=[]), \
             patch.object(m,'patch_run',return_value=row) as save,patch.object(app,'_company_send_walmart_finance') as send:
            with self.assertRaises(app.HTTPException):m.finish(app,'FIN',row)
            send.assert_not_called()
            self.assertEqual(save.call_args.args[3]['报表状态'],'AI初审中')

    def test_p0_with_failed_gap_save_recovers_audit(self):
        row=run('P0待处理',monthly_job={},monthly_validation={'version':m.VERSION})
        with patch.object(m,'open_gaps',return_value=[]),patch.object(m,'finish',return_value={'ok':True}) as finish:
            m.recover_one(app,'FIN',row);finish.assert_called_once()

    def test_pending_supplement_is_resumed_before_other_actions(self):
        row=run('待AI重跑',monthly_resolution={'pending':True})
        with patch.object(m,'continue_resolution',return_value={'ok':True}) as continuation,patch.object(m,'trigger') as trigger:
            m.recover_one(app,'FIN',row);continuation.assert_called_once();trigger.assert_not_called()

    def test_supplement_save_failure_does_not_close_any_gap(self):
        row=run('P0待处理',walmart_gap_delivery={'targets':{'a':{'message_id':'m','gap_key':'k','active':True}}})
        body={'value':{'run_id':'company-profit-2026-10-walmart','gap_key':'k'},'message_id':'m','operator_open_id':'finance',
              'card_form_value':{'reason':'成本已补齐','resolution':'recheck'}}
        with patch.object(app,'finance_assistant_tok',return_value='FIN'),patch.object(m,'find',return_value=row), \
             patch.object(app,'_contact_user',return_value={'union_id':'finance'}), \
             patch.object(app,'_company_walmart_finance_recipients',return_value=[{'type':'union_id','id':'finance'}]), \
             patch.object(m,'open_gaps',return_value=[{'record_id':'gap','fields':{}}]), \
             patch.object(m,'patch_run',side_effect=RuntimeError()),patch.object(m,'save') as save:
            with self.assertRaises(RuntimeError):m.resume(app,body)
            save.assert_not_called()

    def test_registered_job_retries_interrupted_audit(self):
        row=run('AI初审中',monthly_job={'status':'registered','id':'job'})
        with patch.object(m,'finish',return_value={'ok':True}) as finish:
            m.register(app,'FIN',row,{'job_id':'job'});finish.assert_called_once()

    def test_partial_put_receipt_preserves_run_identity(self):
        row=run()
        with patch.object(m,'api',return_value={'record':{'record_id':'r','fields':{'报表状态':'待财务终审'}}}):
            result=m.patch_run(app,'FIN',row,{'报表状态':'待财务终审'},review_version='new')
            self.assertEqual(result['fields']['run_id'],row['fields']['run_id'])
            self.assertEqual(app._company_run_payload(result['fields'])['review_version'],'new')

    def test_recovery_does_not_dispatch_historical_legacy_runs(self):
        row=run('P0待处理',monthly_job={})
        with patch.object(m,'records',return_value=[row]),patch.object(app,'_last_month',return_value='2026-10'), \
             patch.object(m,'recover_one') as work:
            m.recover(app,'FIN');work.assert_not_called()
