import unittest
import copy
import json
from unittest.mock import patch, Mock
from fastapi.testclient import TestClient
from test_walmart_finance_handoff import run, CARD, TARGETS
import app


class WalmartFinanceIdentityTests(unittest.TestCase):
    def setUp(self):
        p=patch('requests.sessions.Session.request',side_effect=AssertionError('Unexpected external HTTP'))
        p.start();self.addCleanup(p.stop)

    def test_walmart_dispatch_uses_finance_assistant_without_old_sender(self):
        with patch.object(app, 'finance_assistant_tok', return_value='FIN') as finance, \
             patch.object(app, 'event_tok', side_effect=AssertionError('Legacy sender selected')), \
             patch.object(app, '_company_send_walmart_finance', return_value=[]) as send:
            app._company_send_workflow_card('DATA', run(), 'finance', CARD, 'finance_gray')
            finance.assert_called_once()
            self.assertEqual(send.call_args.args[:2], ('FIN', 'FIN'))

    def test_migration_retires_all_old_app_cards_using_their_own_app(self):
        rr=run();p=app._company_run_payload(rr['fields'])
        p['walmart_finance_delivery']={'targets':{
            r['type']+':'+r['id']:{**r,'message_id':'old-'+r['id'],'active':True} for r in TARGETS}}
        rr['fields']['payload_json']=json.dumps(p)
        saves=[]
        def save(t,r,d):saves.append(copy.deepcopy(d))
        seq=iter(['new-group','new-a','new-b'])
        with patch.object(app,'_finance_walmart_get',return_value={'record':rr}), patch.object(app,'_company_walmart_finance_recipients',return_value=TARGETS), \
             patch.object(app,'event_tok',return_value='OLD'), \
             patch.object(app,'_patch_message_card',return_value={'code':0}) as patches, \
             patch.object(app,'_company_save_walmart_delivery',side_effect=save), \
             patch.object(app.requests,'post',side_effect=lambda *a,**k:Mock(json=lambda:{'code':0,'data':{'message_id':next(seq)}})) as sends:
            app._company_send_walmart_finance('FIN','FIN',rr,CARD)
        self.assertEqual(len(patches.call_args_list),3)
        self.assertTrue(all(c.args[0]=='OLD' for c in patches.call_args_list))
        self.assertTrue(all(c.kwargs['headers']['Authorization']=='Bearer FIN' for c in sends.call_args_list))
        active=[x for x in saves[-1]['targets'].values() if x['active']]
        self.assertEqual(len(active),3)
        self.assertTrue(all(x['app_id']==app.FINANCE_ASSISTANT_APP_ID for x in active))
        self.assertEqual(len([x for x in saves[-1]['targets'].values() if not x['active']]),3)

    def test_legacy_callback_cannot_approve_new_finance_card(self):
        f=run()['fields'];p=app._company_run_payload(f)
        p['walmart_finance_delivery']={'targets':{'group':{'message_id':'new','active':True,'app_id':app.FINANCE_ASSISTANT_APP_ID}}}
        f['payload_json']=json.dumps(p)
        ctx={'message_id':'new','value':{'walmart_review_key':app._company_walmart_review_key(f)},'operator_open_id':'fin-person'}
        self.assertIn('机器人归属',app._company_walmart_callback_guard('OLD',f,ctx))
        with patch.object(app,'_contact_user',return_value={'union_id':'u-a'}) as contact, \
             patch.object(app,'_company_walmart_finance_recipients',return_value=TARGETS):
            self.assertEqual(app._company_walmart_callback_guard('FIN',f,ctx,'finance_assistant'),'')
            self.assertEqual(contact.call_args.args,('FIN','fin-person'))

    def test_finance_webhook_checks_token_and_dispatches_only_finance_action(self):
        client=TestClient(app.app)
        body={'header':{'token':'correct'},'event':{'action':{'value':{
            'action':'company_profit_finance_approve','delivery_identity':'finance_assistant','run_id':'test'}}}}
        with patch.object(app,'FINANCE_ASSISTANT_VERIFICATION_TOKEN','correct'), \
             patch.object(app,'_finance_walmart_callback_safe',return_value={'ok':True}) as callback:
            wrong=copy.deepcopy(body);wrong['header']['token']='wrong'
            self.assertEqual(client.post('/finance-assistant/r5/callback',json=wrong).status_code,403)
            callback.assert_not_called()
            response=client.post('/finance-assistant/r5/callback',json=body)
            self.assertEqual(response.status_code,200)
            self.assertIn('正在核验',response.json()['toast']['content'])
            callback.assert_called_once_with(body)

    def test_finance_result_patch_never_uses_old_app(self):
        rr=run();p=app._company_run_payload(rr['fields'])
        p['walmart_finance_delivery']={'app_id':app.FINANCE_ASSISTANT_APP_ID,'targets':{
            'g':{'active':True,'message_id':'new','app_id':app.FINANCE_ASSISTANT_APP_ID}}}
        rr['fields']['payload_json']=json.dumps(p)
        with patch.object(app,'finance_assistant_tok',return_value='FIN'), \
             patch.object(app,'event_tok',side_effect=AssertionError('Legacy token used')), \
             patch.object(app,'_company_save_walmart_delivery'), \
             patch.object(app,'_patch_message_card',return_value={'code':0}) as patches:
            app._company_walmart_sync_result('FIN',rr,{'elements':[]})
        self.assertEqual(patches.call_args.args[:2],('FIN','new'))


if __name__ == '__main__':
    unittest.main()
