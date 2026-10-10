import copy
import json
import unittest
from unittest.mock import Mock, patch
from fastapi.testclient import TestClient

import app


class WalmartFinalIndexTests(unittest.TestCase):
    def setUp(self):
        blocker = patch('requests.sessions.Session.request', side_effect=AssertionError('Unexpected HTTP'))
        blocker.start()
        self.addCleanup(blocker.stop)
        self.url = 'https://u1wpma3xuhr.feishu.cn/sheets/approved-v3'
        self.fields = {'run_id': 'company-profit-2026-09-walmart', '期间': '2026-09',
                       '平台': '沃尔玛', '报表状态': '已归档', '总表状态': '已灌总表', 'P0数量': '0',
                       '报表链接': self.url, 'payload_json': json.dumps({
                           'platform_id': 'walmart', 'meta': {}, 'review_url': self.url, 'review_version': 'V3'})}
        self.index = {'record_id': 'index-row', 'fields': {'沃尔玛毛利报表': {'link': 'old-report'},
                      '亚马逊毛利报表': {'link': 'amazon-report'}}}

    def test_archive_publishes_approved_walmart_url_with_final_status(self):
        with patch.object(app, '_idx_find_record', return_value=self.index), \
             patch.object(app, '_company_ensure_index_finalize_fields', return_value={'ok': True}), \
             patch.object(app.requests, 'put', return_value=Mock(json=lambda: {'code': 0})) as put:
            result = app._company_mark_index_finalized('FIN', self.fields, 1791596613384)
        self.assertTrue(result['ok'])
        changed = put.call_args.kwargs['json']['fields']
        self.assertEqual(changed['沃尔玛毛利报表']['link'], self.url)
        self.assertNotIn('亚马逊毛利报表', changed)

    def approval(self):
        return {'record_id': 'audit-human', 'fields': {
            'run_id': self.fields['run_id'], 'action': 'company_profit_finance_approve',
            'actor_open_id': 'human-finance', 'result': 'ok', 'source_message_id': 'review-card',
            'payload_json': json.dumps({'value': {'delivery_identity': 'finance_assistant',
                'walmart_review_key': app._company_walmart_review_key(self.fields)}})}}

    def test_repair_is_link_only_and_repeat_does_not_write(self):
        rows = [self.approval()]
        updated = copy.deepcopy(self.index)
        updated['fields']['沃尔玛毛利报表'] = {'link': self.url}
        with patch.object(app, '_bitable_all', return_value=rows), \
             patch.object(app, '_idx_find_record', side_effect=[self.index, updated, updated]), \
             patch.object(app, '_company_write_system_audit') as audit, \
             patch.object(app.requests, 'put', return_value=Mock(json=lambda: {'code': 0})) as put:
            result = app._finance_walmart_repair_index('FIN', self.fields)
            repeat = app._finance_walmart_repair_index('FIN', self.fields)
        self.assertTrue(result['ok'])
        self.assertTrue(repeat['unchanged'])
        put.assert_called_once()
        self.assertEqual(set(put.call_args.kwargs['json']['fields']), {'沃尔玛毛利报表'})
        self.assertEqual(put.call_args.kwargs['headers']['Authorization'], 'Bearer FIN')
        audit.assert_called_once()

    def test_repair_rejects_unapproved_changed_or_unrecorded_review(self):
        for case in ('pending', 'changed', 'missing'):
            fields = copy.deepcopy(self.fields)
            rows = [self.approval()]
            if case == 'pending': fields['报表状态'] = '待财务终审'
            if case == 'changed': fields['报表链接'] = self.url + '-changed'
            if case == 'missing': rows = []
            with self.subTest(case=case), patch.object(app, '_bitable_all', return_value=rows), \
                 self.assertRaises(app.HTTPException):
                app._finance_walmart_repair_index('FIN', fields)

    def test_repair_does_not_report_success_on_failed_write(self):
        with patch.object(app, '_bitable_all', return_value=[self.approval()]), \
             patch.object(app, '_idx_find_record', return_value=self.index), \
             patch.object(app.requests, 'put', return_value=Mock(json=lambda: {'code': 99991672})), \
             self.assertRaises(app.HTTPException):
            app._finance_walmart_repair_index('FIN', self.fields)

    def test_scoped_endpoint_uses_finance_identity_and_current_record(self):
        run = {'record_id': 'run-row', 'fields': self.fields}
        with patch.object(app, '_require_internal_auth'), \
             patch.object(app, 'FINANCE_ASSISTANT_VERIFICATION_TOKEN', 'configured'), \
             patch.object(app, 'FINANCE_ASSISTANT_ENCRYPT_KEY', ''), \
             patch.object(app, 'finance_assistant_tok', return_value='FIN'), \
             patch.object(app, '_finance_walmart_get', return_value={}), \
             patch.object(app, '_bt_find', return_value=run), \
             patch.object(app, '_finance_walmart_record', return_value=run) as reread, \
             patch.object(app, '_finance_walmart_repair_index', return_value={'ok': True}) as repair:
            response = TestClient(app.app).post('/finance-assistant/walmart/review',
                json={'mode': 'repair_index', 'run_id': self.fields['run_id']})
        self.assertEqual(response.status_code, 200)
        reread.assert_called_once_with('FIN', 'run-row')
        repair.assert_called_once_with('FIN', self.fields)


if __name__ == '__main__':
    unittest.main()
