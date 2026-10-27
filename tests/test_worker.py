import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from strata_board_harness.board import TaskLease
from strata_board_harness.coordination import Journal
from strata_board_harness.coordinator import Coordinator
from strata_board_harness.worker import ExecutionWorker
from strata_board_harness.supervisor import Store
from test_coordinator import FakeBoard, IDENTITY, message


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Store(Path(self.tmp.name));self.addCleanup(self.store.close)
        self.journal=Journal(self.store,IDENTITY);self.now=1000
        self.store.connection.execute('CREATE TABLE engine_messages (body TEXT NOT NULL)')
        self.client=FakeBoard(lambda:self.now);self.posts=[]
        self.reports=[]
        self.client.post_selected_message=lambda *args:(self.reports if args[2].startswith('report-') else self.posts).append(args)
        owner=self
        class Engine:
            def __init__(self):
                self.state={'phase':'ready','session_id':'ses_fixture','steps':0,'progress_at':owner.now}
                self.session=SimpleNamespace(abort=lambda intention:None);self.posts=0;self.stops=[];self.recent=[]
                self.guard=lambda:True;self.authorize=lambda env,*args:env.get('author',{}).get('agent_id')=='manager'
            def prepare(self):return True
            def process_active(self):return self.state['phase']!='stopped'
            def start_prompt(self,text):
                if not owner.client.ticket_value['claimant']:raise AssertionError('Prompt before claim')
                self.posts+=1;self.state['phase']='running'
            def observe_history(self):return self.recent
            def stop(self,reason):
                self.stops.append(reason);self.state.update(phase='stopped',cleanup_verified=True);return True
        class Validation:
            def __init__(self):self.value={};self.cancels=0
            def status(self):return self.value
            def cancel(self):self.cancels+=1
            def checkpoint(self):return {'head':'a'*40,'clean':True,'source_sha256':'source-fixture','owned_effects':'none'}
        self.engine,self.validation=Engine(),Validation()
        self.selection={'ticket_id':'selected-task','thread_id':'selected-thread','authorization_id':'human-selected',
            'manager_agent_id':'manager','prompt':'selected brief','step_budget':40,'max_active_seconds':100}
        self.worker=ExecutionWorker(self.store,self.journal,self.client,self.engine,self.validation,self.selection,lambda:self.now)
        self.lease=TaskLease(self.journal,self.client,'selected-task',lambda:self.worker.observe(self.now).active,lambda:self.now)
        self.worker.attach_lease(self.lease)
        self.coordinator=Coordinator(self.store,self.journal,self.client,lambda:self.worker.observe(self.now),self.worker.quiesce,self.lease)

    def tick(self,now=None,backend='model_available'):
        if now is not None:self.now=now
        self.worker.tick(self.now,backend);return self.coordinator.tick(self.now,backend)

    def resume_with_manager_guidance(self, identifier):
        text='Continue the same selected task after exact manager review.'
        entry=message(identifier,text);entry['author']={'agent_id':'manager'}
        self.journal.persist_page(self.journal.cursor(),{'session':{'id':IDENTITY.session_id,
            'owner_id':IDENTITY.owner_id,'agent_id':IDENTITY.agent_id,'device_id':IDENTITY.device_id},
            'messages':[entry],'cursor':identifier})
        self.journal.save_body(identifier,text,entry['text_fields']['body']['sha256'])
        def deliver(message_id):
            self.journal.prepare_delivery(message_id,'ses_fixture','request-'+message_id)
            self.journal.record_receipt(message_id,'submitted',{'observation':'fake_engine_accepted'})
        self.engine.session.deliver=deliver
        self.engine.session.acknowledge=lambda *args:None
        self.worker._guidance(self.now,True)
        self.assertEqual(self.worker.state['phase'],'developing')

    def test_claim_precedes_prompt_and_time_cap_is_terminal_without_automatic_retry(self):
        self.tick();self.assertEqual(self.engine.posts,1)
        self.tick(1100)
        self.assertEqual(self.worker.state['stop_reason'],'active_time_cap')
        self.assertEqual(self.engine.posts,1);self.assertIsNone(self.client.ticket_value['claimant'])
        self.tick(1101);self.assertEqual(self.engine.posts,1)

    def test_transient_unreachable_guard_waits_and_only_prolonged_outage_stops(self):
        self.selection['max_active_seconds']=5000
        self.tick();self.assertEqual(self.engine.posts,1)
        def unreachable():raise RuntimeError('Bridge unavailable or response invalid')
        self.engine.guard=unreachable
        self.tick(1010);self.tick(1060)
        self.assertIsNone(self.worker.state['stop_reason']);self.assertEqual(self.engine.stops,[])
        self.engine.guard=lambda:True
        self.tick(1070)
        self.assertNotIn('guard_unavailable_since',self.worker.state)
        self.engine.guard=unreachable
        self.tick(1080);self.tick(1199)
        self.assertIsNone(self.worker.state['stop_reason'])
        self.tick(1201)
        self.assertEqual(self.worker.state['stop_reason'],'cross_host_guard_unavailable')

    def test_previous_live_claim_waits_without_prompt_then_claims_once_free(self):
        self.selection['max_active_seconds']=5000
        self.client.ticket_value.update(claimant={'kind':'agent','agent_id':'previous-attempt'},status='in_progress')
        self.tick(1000);self.tick(1500)
        self.assertEqual(self.engine.posts,0);self.assertIsNone(self.worker.state['stop_reason'])
        self.assertEqual(self.client.mutations,[])
        self.client.ticket_value.update(claimant=None,status='todo')
        self.tick(1600)
        self.assertEqual(self.engine.posts,1);self.assertEqual(self.worker.state['phase'],'developing')
        self.assertNotIn('claim_wait_since',self.worker.state)

    def test_own_expired_claim_is_recovered_only_with_manager_evidence(self):
        from dataclasses import asdict
        from test_coordinator import IDENTITY
        own={'kind':'agent',**asdict(IDENTITY)}
        self.client.ticket_value.update(claimant=own,claim_expired=True,status='todo')
        self.tick(1000)
        self.assertEqual(self.engine.posts,0);self.assertEqual(self.client.mutations,[])
        self.selection['recovery_evidence']='Manager reconciliation: previous execution archived, no engine, locks absent.'
        self.tick(1010)
        self.assertEqual(self.engine.posts,1)
        operation,_,arguments=self.client.mutations[0]
        self.assertEqual(operation,'claim_ticket');self.assertIn('Manager reconciliation',arguments['recovery_evidence'])

    def test_foreign_or_live_claim_is_never_recovered_even_with_evidence(self):
        self.selection['recovery_evidence']='Manager reconciliation'
        self.client.ticket_value.update(claimant={'kind':'agent','agent_id':'someone-else','owner_id':'x'},
                                        claim_expired=True,status='in_progress')
        self.tick(1000);self.assertEqual(self.engine.posts,0)
        from dataclasses import asdict
        from test_coordinator import IDENTITY
        self.client.ticket_value.update(claimant={'kind':'agent',**asdict(IDENTITY)},claim_expired=False)
        self.tick(1010);self.assertEqual(self.engine.posts,0);self.assertEqual(self.client.mutations,[])

    def test_claim_that_never_frees_stops_after_bounded_wait(self):
        self.client.ticket_value.update(claimant={'kind':'agent','agent_id':'someone-else'},status='in_progress')
        self.tick(1000);self.tick(3399);self.assertIsNone(self.worker.state['stop_reason'])
        self.tick(3401)
        self.assertEqual(self.worker.state['stop_reason'],'selected_task_not_claimable');self.assertEqual(self.engine.posts,0)

    def test_definite_reservation_mismatch_still_stops_immediately(self):
        self.selection['max_active_seconds']=5000
        self.tick();self.engine.guard=lambda:False;self.tick(1010)
        self.assertEqual(self.worker.state['stop_reason'],'cross_host_guard_lost')

    def test_same_failing_set_twice_sends_one_evidence_notice(self):
        self.selection['max_active_seconds']=5000
        notices=[];self.engine.session.notice=lambda key,text:notices.append((key,text)) or {'state':'accepted'}
        self.tick()
        def finish(identifier,at,failed):
            self.validation.value={'last_feedback':{'id':identifier,'finished_at':at,'exit_code':1,'operation':'test',
                'summary':{'counts':{'passed':3,'failed':len(failed)},'failed':failed}}}
        finish('job-1',1001,['AlarmE2eTest.createAlarm']);self.tick(1003)
        self.assertEqual(notices,[])
        finish('job-2',1004,['AlarmE2eTest.createAlarm']);self.tick(1006)
        self.assertEqual(len(notices),1);self.assertIn('EVIDENCE:',notices[0][1]);self.assertIn('job-2',notices[0][1])
        finish('job-3',1007,['AlarmE2eTest.createAlarm']);self.tick(1009)
        self.assertEqual(len(notices),1)
        finish('job-4',1010,['TimerE2eTest.zero']);self.tick(1012)
        finish('job-5',1013,['TimerE2eTest.zero']);self.tick(1015)
        self.assertEqual(len(notices),2)
        self.assertTrue(any('evidence required' in report[1] for report in self.reports))

    def test_context_window_error_in_engine_log_stops_at_once(self):
        self.selection['max_active_seconds']=5000
        self.engine.root=Path(self.tmp.name)/'engine';self.engine.root.mkdir()
        log=self.engine.root/'server.log';log.write_text('opencode server listening on http://127.0.0.1:4096\n')
        self.tick();self.assertIsNone(self.worker.state['stop_reason'])
        with log.open('a') as stream:
            stream.write('timestamp=x level=ERROR message="stream error" error.error="AI_APICallError: request exceeds '
                         'the context window: prompt (265543 tokens) leaves no room"\n')
        self.tick(1003)
        self.assertEqual(self.worker.state['stop_reason'],'engine_context_exhausted')
        self.assertTrue(any('context window' in report[1] for report in self.reports))

    def test_early_build_escalation_rearms_with_grace_after_guidance(self):
        self.selection['max_active_seconds']=5000
        self.tick();self.engine.state['steps']=12;self.tick(1002)
        self.assertEqual(len(self.posts),1)
        self.resume_with_manager_guidance('resume-build')
        self.engine.state['steps']=14;self.tick(1004)
        self.assertEqual(len(self.posts),1,'acknowledging guidance must not re-trigger the early-build blocker')
        self.engine.state['steps']=20;self.tick(1006)
        self.assertEqual(len(self.posts),2)

    def test_idle_offline_does_not_prepare_claim_or_invoke_model(self):
        self.tick(1000,'waiting_for_model');self.tick(3000,'waiting_for_model')
        self.assertEqual(self.engine.posts,0);self.assertEqual(self.client.mutations,[])

    def test_early_build_missing_escalates_once_with_evidence_and_stops(self):
        self.tick();self.engine.state['steps']=12;self.tick(1002)
        self.assertEqual(len(self.posts),1);self.assertIn('mandatory_early_build_missing',self.posts[0][1])
        self.assertEqual(self.posts[0][-1],'manager');self.tick(1004)
        self.assertEqual(len(self.posts),1);self.assertEqual(self.engine.posts,1)
        self.assertEqual(self.store.get('worker_blocker')['state'],'posted')
        self.assertEqual(self.worker.state['phase'],'waiting_for_manager')
        self.tick(1663)
        self.assertEqual(self.worker.state['stop_reason'],'manager_wait_elapsed')
        self.assertIsNone(self.client.ticket_value['claimant'])

    def test_authenticated_urgent_stop_is_processed_even_when_model_offline(self):
        self.tick()
        entry=message('stop','HARNESS_CONTROL STOP');entry['author']={'agent_id':'manager'}
        self.journal.persist_page(self.journal.cursor(),{'session':{'id':IDENTITY.session_id,'owner_id':IDENTITY.owner_id,
            'agent_id':IDENTITY.agent_id,'device_id':IDENTITY.device_id},'messages':[entry],'cursor':'control'})
        self.journal.save_body('stop','HARNESS_CONTROL STOP',entry['text_fields']['body']['sha256'])
        self.tick(1001,'waiting_for_model')
        self.assertEqual(self.worker.state['stop_reason'],'manager_control')
        self.assertEqual(self.engine.stops,['manager_control'])
        self.assertEqual(self.worker.state['control_message']['id'],'stop')

    def test_review_requires_clean_exact_commit_build_and_releases_to_review(self):
        self.tick();self.validation.value={'build_started_at':1000,'successful_build_at':1001,'successful_build_source_sha256':'source-fixture'}
        self.engine.recent=[{'info':{'role':'assistant'},'parts':[{'type':'text','text':'HARNESS_REVIEW '+'a'*40}]}]
        self.tick(1002)
        self.assertEqual(self.worker.state['phase'],'review')
        self.assertEqual(self.client.ticket_value['status'],'review')
        self.assertIsNone(self.client.ticket_value['claimant'])
        self.assertTrue(self.worker.state['checkpoint']['clean'])

    def test_step_cap_preserves_partial_checkpoint_and_never_enters_a_new_round(self):
        self.tick();self.engine.state['steps']=40;self.tick(1002)
        self.assertEqual(self.worker.state['stop_reason'],'step_cap')
        self.assertIsNotNone(self.journal.checkpoint('selected-task'))
        self.tick(1003);self.assertEqual(self.engine.posts,1)

    def test_trusted_stop_received_before_admission_prevents_claim_and_prompt(self):
        text='HARNESS_CONTROL STOP'
        entry=message('pre-stop',text);entry['author']={'agent_id':'manager'}
        self.journal.persist_page(None,{'session':{'id':IDENTITY.session_id,'owner_id':IDENTITY.owner_id,
            'agent_id':IDENTITY.agent_id,'device_id':IDENTITY.device_id},'messages':[entry],'cursor':'pre-stop'})
        self.journal.save_body('pre-stop',text,entry['text_fields']['body']['sha256'])
        self.tick()
        self.assertEqual(self.engine.posts,0);self.assertEqual(self.client.mutations,[])
        self.assertEqual(self.worker.state['stop_reason'],'manager_control_before_admission')

    def test_stale_build_cannot_be_used_for_review_of_different_source(self):
        self.tick();self.validation.value={'build_started_at':1000,'successful_build_at':1001,
            'successful_build_source_sha256':'old-source'}
        self.engine.recent=[{'info':{'role':'assistant'},'parts':[{'type':'text','text':'HARNESS_REVIEW '+'a'*40}]}]
        self.tick(1002)
        self.assertNotEqual(self.worker.state['phase'],'review')
        self.assertEqual(self.client.ticket_value['status'],'in_progress')
        self.assertEqual(self.store.get('worker_blocker')['reason'],'review_without_successful_build')

    def test_repeated_unchanged_reads_pause_before_step_budget(self):
        self.tick();self.validation.value={'build_started_at':1000,'source_sha256':'unchanged'}
        for i in range(8):
            self.engine.recent=[{'info':{'role':'assistant'},'parts':[{'type':'tool','tool':'read',
                'callID':'read-'+str(i),'state':{'status':'completed','input':{'filePath':'same.kt'}}}]}]
            self.tick(1002+i*2)
        self.assertEqual(self.worker.state['phase'],'waiting_for_manager')
        self.assertEqual(self.store.get('worker_blocker')['reason'],'repeated_unchanged_tool_action')
        self.assertEqual(self.engine.posts,1)

    def test_legitimate_bounded_job_liveness_does_not_extend_past_its_deadline(self):
        self.tick();self.engine.state['progress_at']=1000
        self.validation.activity=lambda:{'verified_thread_alive':True,'started_at':1000,'deadline_at':2805}
        self.assertEqual(self.worker.observe(2500).progress_at,2500)
        self.assertEqual(self.worker.observe(2806).progress_at,1000)

    def test_multiline_developer_blocker_escalates_once_and_user_quotation_does_not(self):
        self.tick()
        text='HARNESS_BLOCKER\nPrecise owned failure and attempts with evidence.'
        self.engine.recent=[{'info':{'role':'user'},'parts':[{'type':'text','text':text}]}]
        self.tick(1002);self.assertEqual(self.posts,[])
        self.engine.recent=[{'info':{'role':'assistant'},'parts':[{'type':'text','text':text}]}]
        self.tick(1004)
        self.assertEqual(self.worker.state['phase'],'waiting_for_manager')
        self.assertEqual(len(self.posts),1)
        self.assertEqual(self.store.get('worker_blocker')['reason'],'developer_reported_blocker')
        self.tick(1006);self.assertEqual(len(self.posts),1)

    def test_developer_notes_are_relayed_once_to_the_thread_without_pausing_work(self):
        self.tick()
        note='HARNESS_NOTE PermissionE2ETest: revoking inside instrumentation kills the host; use the denied fixture.'
        self.engine.recent=[{'info':{'role':'user'},'parts':[{'type':'text','text':note}]}]
        self.tick(1002)
        self.assertFalse(any('Developer note' in r[1] for r in self.reports))
        self.engine.recent=[{'info':{'role':'assistant'},'parts':[{'type':'text','text':note}]}]
        self.tick(1004);self.tick(1006)
        notes=[r for r in self.reports if 'Developer note' in r[1]]
        self.assertEqual(len(notes),1);self.assertIn('denied fixture',notes[0][1])
        self.assertEqual(self.worker.state['phase'],'developing');self.assertEqual(self.posts,[])

    def test_test_results_heartbeat_and_review_report_are_posted_for_other_agents(self):
        self.tick()
        self.assertTrue(any('heartbeat' in r[1] for r in self.reports))
        self.validation.value={'last_feedback':{'id':'job-1','finished_at':1001,'exit_code':1,'operation':'test',
            'summary':{'counts':{'passed':22,'failed':2,'skipped':0,'unrun':0,'declared_total':24,'crashed':False},
                       'failed':['WidgetE2ETest.places']}}}
        self.tick(1002)
        result=[r for r in self.reports if 'emulator run finished' in r[1]]
        self.assertEqual(len(result),1);self.assertIn('22 passed, 2 failed',result[0][1]);self.assertIn('WidgetE2ETest.places',result[0][1])
        self.tick(1004);self.assertEqual(len([r for r in self.reports if 'emulator run finished' in r[1]]),1)

    def test_report_failure_never_affects_execution(self):
        def broken(*args):raise RuntimeError('board down')
        self.client.post_selected_message=broken
        self.tick();self.assertEqual(self.worker.state['phase'],'developing')
        self.assertEqual(self.worker.state['report_error'],'RuntimeError')

    def test_explicit_operator_resume_reclaims_same_task_without_resubmitting_initial_prompt(self):
        self.tick();self.worker._stop('infrastructure_failure');self.coordinator.tick(self.now,'model_available')
        self.assertIsNone(self.client.ticket_value['claimant'])
        self.worker.state.update(phase='developing',stop_reason=None,previous_tick=None,
            active_seconds=10,manager_resume_claim={'session_id':'ses_fixture'})
        self.engine.state.update(phase='running',steps=7,active_started_at=1000)
        self.tick(1002)
        self.assertIsNotNone(self.client.ticket_value['claimant'])
        self.assertEqual(self.engine.posts,1);self.assertEqual(self.engine.state['steps'],7)
        self.assertGreaterEqual(self.worker.state['active_seconds'],10)
        self.assertNotIn('manager_resume_claim',self.worker.state)

    def test_real_early_build_attempt_survives_controller_restart_without_becoming_a_pass(self):
        self.tick();self.engine.state['steps']=13
        self.validation.value={'build_started_at':1000}
        self.tick(1002)
        self.assertEqual(self.worker.state['first_build_started_at'],1000)
        self.validation.value={}
        restored=ExecutionWorker(self.store,self.journal,self.client,self.engine,self.validation,self.selection,lambda:self.now)
        restored.attach_lease(self.lease)
        self.now=1004;restored.tick(self.now,'model_available')
        self.assertEqual(self.posts,[])
        self.assertFalse(restored.state['build_checkpoint'])
        self.assertEqual(restored.state['phase'],'developing')

    def test_build_reviews_have_separate_bounded_budget_and_still_pause_for_manager(self):
        self.tick()
        self.worker._escalate('repeated_validation_failure',{'attempts':2,'exit_code':1})
        self.resume_with_manager_guidance('failure-guidance')
        for index in range(3):
            evidence=[{'path':'app/build.gradle.kts','sha256':str(index)*64}]
            self.worker._escalate('build_control_review_required',evidence)
            self.assertEqual(self.worker.state['phase'],'waiting_for_manager')
            self.assertEqual(self.store.get('worker_blocker')['evidence'],evidence)
            self.assertIsNotNone(self.client.ticket_value['claimant'])
            self.resume_with_manager_guidance('review-guidance-'+str(index))
        self.assertEqual(self.worker.state['blocker_count'],4)
        self.assertEqual(self.worker.state['failure_blocker_count'],1)
        self.assertEqual(self.worker.state['build_review_count'],3)
        self.assertEqual(self.engine.posts,1)
        self.worker._escalate('build_control_review_required',
            [{'path':'app/build.gradle.kts','sha256':'f'*64}])
        self.coordinator.tick(self.now,'model_available')
        self.assertEqual(self.worker.state['stop_reason'],'build_review_budget_elapsed')
        self.assertIsNone(self.client.ticket_value['claimant'])
        self.tick(1002)
        self.assertEqual(self.engine.posts,1)

    def test_build_review_dedup_restart_and_deadline_do_not_grant_approval(self):
        self.tick()
        evidence=[{'path':'app/build.gradle.kts','sha256':'a'*64}]
        self.worker._escalate('build_control_review_required',evidence)
        restored=ExecutionWorker(self.store,self.journal,self.client,self.engine,self.validation,
            self.selection,lambda:self.now)
        restored.attach_lease(self.lease)
        restored._escalate('build_control_review_required',evidence)
        self.assertEqual(len(self.posts),1)
        self.assertEqual(restored.state['build_review_count'],1)
        self.assertEqual(restored.state['failure_blocker_count'],0)
        self.assertEqual(restored.state['phase'],'waiting_for_manager')
        self.now=1661;restored.tick(self.now,'model_available')
        self.coordinator.worker=lambda:restored.observe(self.now)
        self.coordinator.tick(self.now,'model_available')
        self.assertEqual(restored.state['stop_reason'],'manager_wait_elapsed')
        self.assertIsNone(self.client.ticket_value['claimant'])
        self.assertEqual(self.engine.posts,1)

    def test_failure_budget_remains_terminal_after_separate_build_reviews(self):
        self.tick()
        self.worker._escalate('build_control_review_required',
            [{'path':'app/build.gradle.kts','sha256':'a'*64}])
        self.resume_with_manager_guidance('approved-review')
        for index in range(3):
            self.worker._escalate('developer_reported_blocker',{'failure':index})
            self.assertEqual(self.worker.state['phase'],'waiting_for_manager')
            self.resume_with_manager_guidance('fix-guidance-'+str(index))
        self.worker._escalate('developer_reported_blocker',{'failure':3})
        self.coordinator.tick(self.now,'model_available')
        self.assertEqual(self.worker.state['stop_reason'],'blocker_budget_elapsed')
        self.assertEqual(self.worker.state['failure_blocker_count'],4)
        self.assertIsNone(self.client.ticket_value['claimant'])

    def test_legacy_budget_is_conservative_and_a_stopped_worker_is_not_resumed(self):
        self.tick()
        legacy=dict(self.worker.state,blocker_count=3)
        legacy.pop('failure_blocker_count',None);legacy.pop('build_review_count',None)
        self.store.save({'execution_worker':legacy})
        restored=ExecutionWorker(self.store,self.journal,self.client,self.engine,self.validation,
            self.selection,lambda:self.now)
        restored.attach_lease(self.lease)
        restored._escalate('developer_reported_blocker',{'legacy_failure':True})
        self.assertEqual(restored.state['stop_reason'],'blocker_budget_elapsed')
        self.store.save({'execution_worker':restored.state})
        stopped=ExecutionWorker(self.store,self.journal,self.client,self.engine,self.validation,
            self.selection,lambda:self.now)
        stopped.attach_lease(self.lease);stopped.tick(1002,'model_available')
        self.assertEqual(stopped.state['phase'],'stopped')
        self.assertEqual(self.engine.posts,1)


if __name__=='__main__':unittest.main()
