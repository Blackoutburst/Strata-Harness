"""Selected-task execution state machine; deterministic control, bounded coding.

Engine and validation observations are injected, never inferred from Board lease
age. A stopped/capped attempt is terminal until the manager selects a new run.
"""

import hashlib
import json
import re
from pathlib import Path

from .board import BoardError, recoverable_own_claim
from .coordination import JournalConflict, encoded
from .coordinator import WorkerState
from .engine import EngineError


GUARD_UNAVAILABLE_GRACE_SECONDS = 120
EVIDENCE_NOTICE = (
    'The same test(s) failed the same way twice in a row: %(failed)s (last job %(job)s). Before editing '
    'any code again: (1) read the failure evidence (the digest, or `harness-tool logs %(job)s:0` and later '
    'pages); (2) reply with one line starting `EVIDENCE:` that quotes the exact assertion/log/E2E_DUMP line '
    'explaining the failure and what it proves; (3) classify it as test bug, environment limit or APP bug; '
    '(4) post it as a HARNESS_NOTE. Only then change code, and only what that evidence supports.')
CLAIM_WAIT_SECONDS = 2400  # longer than one 30-minute Board lease
REPORT_SECONDS = 900  # manager-visible heartbeat cadence on the ticket thread
MAX_NOTES = 40  # developer HARNESS_NOTE posts relayed per execution
EARLY_BUILD_STEPS = 12
EARLY_BUILD_GRACE_STEPS = 8  # steps allowed after manager guidance before re-escalating


class ExecutionWorker:
    def __init__(self, store, journal, client, engine, validation, selection, clock):
        self.store, self.journal, self.client, self.engine = store, journal, client, engine
        self.validation, self.selection, self.clock = validation, selection, clock
        self.lease = None
        self.state = store.get('execution_worker', {'phase': 'preparing', 'active_seconds': 0,
            'previous_tick': None, 'last_backend': None, 'stop_reason': None, 'build_checkpoint': False})
        # Older journals counted all escalations as failures. Preserve that
        # consumed allowance conservatively; never infer/reset it on upgrade.
        self.state.setdefault('failure_blocker_count', self.state.get('blocker_count', 0))
        self.state.setdefault('build_review_count', 0)
        self.next_history = 0

    def attach_lease(self, lease): self.lease = lease

    def observe(self, now):
        state = self.engine.state
        active = (self.state['phase'] in ('developing', 'waiting_for_manager')
                  and self.engine.process_active() and state.get('phase') == 'running')
        progress=state.get('progress_at',now)
        activity=self.validation.activity() if hasattr(self.validation,'activity') else None
        if (active and activity and activity.get('verified_thread_alive') is True
                and activity.get('started_at',now)<=now<=activity.get('deadline_at',0)):
            progress=max(progress,now)
        return WorkerState(active, progress, state.get('session_id', ''),
            quiescent=state.get('phase') == 'stopped', cleanup_verified=state.get('cleanup_verified') is True,
            checkpoint=self.state.get('checkpoint'), release_status='review' if self.state['phase'] == 'review' else 'todo')

    def _save(self): self.store.save({'execution_worker': self.state})

    def _stop(self, reason):
        self.state['stop_reason'] = reason;self._save()
        self.validation.cancel()
        clean = self.engine.stop(reason)
        checkpoint = self.validation.checkpoint()
        self.state.update(phase='stopped' if clean else 'reconciliation_required', checkpoint=checkpoint)
        if self.engine.state.get('session_id'):
            self.journal.save_checkpoint(self.selection['ticket_id'], self.engine.state['session_id'], checkpoint)
        self._save()

    def quiesce(self, intention): self._stop(intention['reason'])

    def tick(self, now, backend):
        if self.state['phase'] in ('stopped', 'review', 'reconciliation_required'): return
        if self.state['phase']=='preparing':
            for entry in self.journal.pending():
                if (entry['stage']=='ready' and entry['envelope'].get('thread_id')==self.selection['thread_id']
                        and entry['body'].splitlines()[0] in ('HARNESS_CONTROL STOP','HARNESS_CONTROL PAUSE')
                        and self.engine.authorize(entry['envelope'],entry['body'],self.selection['ticket_id'])):
                    self.state['control_message']={'id':entry['id'],'received_at':now}
                    self._stop('manager_control_before_admission');return
        previous = self.state['previous_tick']
        if previous is not None and now < previous:
            self._stop('clock_reconciliation_required');return
        if (previous is not None and self.state['last_backend'] == 'model_available'
                and self.state['phase'] == 'developing'):
            self.state['active_seconds'] += now - previous
        self.state.update(previous_tick=now, last_backend=backend);self._save()
        if self.state.get('manager_resume_claim'):
            # Only an explicit operator reconciliation can set this durable
            # intention. It resumes the same session/budget after a released
            # infrastructure failure, never an automatic new attempt/prompt.
            if backend!='model_available':return
            expected=self.state['manager_resume_claim']
            if (self.state['active_seconds']>=self.selection.get('max_active_seconds',5400)
                    or self.engine.state['steps']>=self.selection['step_budget']):
                self._stop('resume_budget_exhausted');return
            if not self.engine.prepare():return
            saved=self.lease.state()
            if (saved['pending'] or saved['fenced'] or (saved['snapshot'] and saved['snapshot'].get('claimant'))
                    or self.engine.state.get('session_id')!=expected['session_id']
                    or self.engine.state.get('active_started_at') is None):
                self._stop('resume_claim_reconciliation_required');return
            self.lease.claim()
            self.state.pop('manager_resume_claim');self.state['phase']='developing';self._save()
        if self.state['phase'] == 'preparing':
            if backend != 'model_available': return
            if not self.engine.prepare(): return
            # A pre-created API is not a coding turn. Only now acquire the exact
            # selected ticket, before a prompt can trigger any model/tool work.
            snapshot = self.lease.state()
            if snapshot['pending'] or snapshot['fenced']:
                self._stop('lease_reconciliation_required');return
            if snapshot['snapshot'] is not None:
                # Crash between claim and prompt must not launch a second turn.
                self._stop('admission_reconciliation_required');return
            current=self.client.ticket(self.selection['ticket_id'])
            if self.selection.get('description_sha256'):
                if hashlib.sha256(current.get('description','').encode()).hexdigest()!=self.selection['description_sha256']:
                    self._stop('selected_description_changed');return
            evidence = self.selection.get('recovery_evidence', '')
            recoverable = bool(evidence.strip()) and recoverable_own_claim(current, self.client.identity)
            if (current.get('claimant') or current.get('status') != 'todo') and not recoverable:
                # A previous attempt's lease (often our own interrupted run) must
                # expire or be released first. Waiting is safe: no prompt, no
                # model call and no claim happen until the ticket is free.
                first = self.state.setdefault('claim_wait_since', now);self._save()
                if now - first >= CLAIM_WAIT_SECONDS:
                    self._stop('selected_task_not_claimable')
                return
            self.state.pop('claim_wait_since', None)
            self.lease.claim(evidence if recoverable else '')
            self.state['phase'] = 'developing';self._save()
            self.engine.start_prompt(self.selection['prompt'])
        if self.engine.session is None and self.state['phase'] == 'developing':
            if not self.engine.prepare(): return
        self._guidance(now, backend == 'model_available')
        if self.state['phase'] in ('stopped', 'review', 'reconciliation_required'): return
        if self.state['phase'] == 'waiting_for_manager':
            if now >= self.state['manager_wait_deadline']:
                self._stop('manager_wait_elapsed')
            return
        if backend != 'model_available': return
        if self.state['active_seconds'] >= self.selection.get('max_active_seconds', 5400):
            self._stop('active_time_cap');return
        try:
            guarded = self.engine.guard()
        except RuntimeError:
            # An unreachable bridge (tunnel hiccup, Mac briefly busy/asleep) is not
            # evidence the reservation changed: pause model-side work and retry, and
            # stop only if it stays unreachable. A definite mismatch stops at once.
            first = self.state.setdefault('guard_unavailable_since', now)
            self.store.save({'execution_worker': self.state})
            if now - first >= GUARD_UNAVAILABLE_GRACE_SECONDS:
                self._stop('cross_host_guard_unavailable')
            return
        if self.state.pop('guard_unavailable_since', None) is not None:
            self.store.save({'execution_worker': self.state})
        if guarded is not True:
            self._stop('cross_host_guard_lost');return
        if not self.engine.process_active():
            self._stop('worker_process_lost');return
        if now < self.next_history: return
        self.next_history = now + 2
        if self._engine_error():return
        recent = self.engine.observe_history()
        if self.engine.state['steps'] >= self.selection['step_budget']:
            self._stop('step_cap');return
        self._guidance(now, True)
        if self.state['phase'] in ('stopped', 'review', 'reconciliation_required'): return
        validation = self.validation.status()
        feedback=validation.get('last_feedback')
        if feedback and feedback['id']!=self.state.get('last_feedback_id'):
            self.state['last_feedback_id']=feedback['id']
            self.engine.state['progress_at']=max(self.engine.state.get('progress_at',0),feedback['finished_at'])
            self.state['last_feedback']=feedback
            if feedback.get('operation')=='test' and feedback.get('summary'):
                counts=feedback['summary']['counts']
                self._report('test-'+feedback['id'], 'Harness progress — emulator run finished (exit %s): %s passed, %s failed, '
                    '%s skipped, %s unrun of %s%s.%s' % (feedback['exit_code'], counts.get('passed'), counts.get('failed'),
                    counts.get('skipped'), counts.get('unrun'), counts.get('declared_total'),
                    ', PROCESS CRASHED' if counts.get('crashed') else '',
                    ('\nFailing: ' + ', '.join(feedback['summary']['failed'])) if feedback['summary']['failed'] else ''))
                self._evidence_gate(feedback)
        if now >= self.state.get('next_report_at', 0):
            self.state['next_report_at'] = now + REPORT_SECONDS
            last = self.state.get('last_feedback') or {}
            self._report('heartbeat-%d' % int(now), 'Harness heartbeat — %s steps of %s, %d of %d active minutes; last '
                'job: %s exit %s; source %s.' % (self.engine.state.get('steps'), self.selection['step_budget'],
                self.state['active_seconds'] // 60, self.selection.get('max_active_seconds', 5400) // 60,
                last.get('operation', 'none'), last.get('exit_code', '-'), (validation.get('source_sha256') or '')[:12]))
        source=validation.get('source_sha256')
        if source is not None and source!=self.state.get('source_sha256'):
            if self.state.get('source_sha256') is not None:self.engine.state['progress_at']=now
            self.state['source_sha256']=source;self.state['recent_actions']=[]
        seen=self.state.setdefault('observed_calls',[])
        actions=self.state.setdefault('recent_actions',[])
        for message in recent:
            for part in message.get('parts',[]):
                if part.get('type')!='tool' or part.get('state',{}).get('status') not in ('completed','error'):continue
                call=part.get('callID')
                if not call or call in seen:continue
                seen.append(call)
                signature=encoded({'tool':part.get('tool'),'input':part.get('state',{}).get('input')})
                actions.append(signature)
                if actions[-12:].count(signature)>=8:
                    self._escalate('repeated_unchanged_tool_action',{'action':json.loads(signature),'occurrences':8,
                        'source_sha256':source,'last_call_id':call});return
        self.state['observed_calls']=seen[-1200:];self.state['recent_actions']=actions[-12:]
        if validation.get('successful_build_at'):
            self.state['build_checkpoint'] = True
            self.engine.state['progress_at'] = max(self.engine.state.get('progress_at', 0), validation['successful_build_at'])
        if validation.get('build_started_at'):
            self.state.setdefault('first_build_started_at',validation['build_started_at'])
        # The deadline re-arms after each escalation: acknowledging guidance costs steps
        # too, so a fixed threshold would re-fire before the worker can start the build.
        deadline = max(self.selection.get('early_build_steps', EARLY_BUILD_STEPS), self.state.get('early_build_deadline_steps', 0))
        if self.engine.state['steps'] >= deadline and not self.state.get('first_build_started_at'):
            self.state['early_build_deadline_steps'] = self.engine.state['steps'] + EARLY_BUILD_GRACE_STEPS
            self._escalate('mandatory_early_build_missing', {'steps': self.engine.state['steps']});return
        if validation.get('requires_manager_review'):
            self._escalate('build_control_review_required', validation['requires_manager_review']);return
        repeated = validation.get('repeated_failure')
        if repeated:
            self._escalate('repeated_validation_failure', repeated);return
        for message in recent:
            if message.get('info',{}).get('role')!='assistant':continue
            for part in message.get('parts', []):
                if part.get('type') != 'text' or not isinstance(part.get('text'), str): continue
                text = part['text']
                # This marker requests a handoff. It never establishes acceptance.
                match = re.search(r'^HARNESS_REVIEW ([0-9a-f]{40})$', text, re.M)
                if match:
                    checkpoint = self.validation.checkpoint()
                    if checkpoint.get('head') != match.group(1) or checkpoint.get('clean') is not True:
                        self._escalate('invalid_review_commit', checkpoint);continue
                    if (not self.state['build_checkpoint'] or validation.get('successful_build_source_sha256') != checkpoint.get('source_sha256')):
                        self._escalate('review_without_successful_build', checkpoint);continue
                    self.state['review_requested'] = match.group(1);self._save()
                    self._report('review-' + match.group(1), 'HARNESS_REVIEW ' + match.group(1)
                        + ' — the worker requests review. Its final report:\n\n' + text[:12000])
                    self._stop('review_requested')
                    # Independent manager acceptance and source status remain open.
                    self.state['phase'] = 'review';self._save()
                note = re.match(r'^HARNESS_NOTE(?:\s+|$)(.*)', text, re.S)
                if note and note.group(1).strip():
                    # The developer's own findings, relayed verbatim to the ticket thread so
                    # other agents/managers can reuse them; the worker never holds a Board token.
                    digest = hashlib.sha256(text.encode()).hexdigest()
                    posted = self.state.setdefault('notes_posted', [])
                    if digest not in posted and len(posted) < MAX_NOTES:
                        posted.append(digest)
                        self._report('note-' + digest, 'Developer note (Strata worker):\n\n' + note.group(1).strip()[:8000])
                if re.match(r'^HARNESS_BLOCKER(?:\s|$)',text):
                    self._escalate('developer_reported_blocker', {'body': text[:6000]})
        self._save()

    def _guidance(self, now, allow_delivery=True):
        for entry in self.journal.pending():
            if entry['envelope'].get('thread_id') != self.selection['thread_id']: continue
            if entry['stage'] == 'ready':
                if not self.engine.authorize(entry['envelope'], entry['body'], self.selection['ticket_id']): continue
                # Reserved structured control line from an authenticated author;
                # ordinary quoted text containing stop is never an interrupt.
                if entry['body'].splitlines()[0] in ('HARNESS_CONTROL STOP', 'HARNESS_CONTROL PAUSE'):
                    self.state['control_message'] = {'id': entry['id'], 'received_at': now}
                    self._stop('manager_control');return
                if allow_delivery:
                    if self.state['phase'] == 'waiting_for_manager' and hasattr(self.validation, 'resume'):
                        self.validation.resume()
                    self.engine.session.deliver(entry['id'])
                    if self.state['phase'] == 'waiting_for_manager':
                        self.state['phase'] = 'developing';self.state['previous_tick'] = now;self._save()
            elif entry['stage'] == 'prepared':
                self.engine.session.reconcile_delivery(entry['id'])
            elif entry['stage'] == 'submitted':
                rows = self.journal.db.execute('SELECT body FROM engine_messages').fetchall()
                for (body,) in rows:
                    value = json.loads(body)
                    if value.get('info', {}).get('role') == 'assistant' and value['info'].get('parentID') == entry['request_id']:
                        try: self.engine.session.acknowledge(entry['id'], value['info']['id'])
                        except EngineError as error:
                            if error.category != 'acknowledgement_not_observed': raise
            elif entry['stage']=='acknowledged' and entry['body'].splitlines()[0]=='HARNESS_EXPECT_CHECKPOINT':
                for (body,) in self.journal.db.execute('SELECT body FROM engine_messages'):
                    value=json.loads(body)
                    if value.get('info',{}).get('parentID')!=entry['request_id']:continue
                    for part in value.get('parts',[]):
                        state=part.get('state',{})
                        if (part.get('type')=='tool' and part.get('tool')=='bash' and state.get('status')=='completed'
                                and state.get('input',{}).get('command')=='harness-tool checkpoint'
                                and state.get('metadata',{}).get('exit')==0):
                            try:job=json.loads(state.get('output',''))
                            except (ValueError,TypeError):continue
                            if job.get('operation')=='checkpoint' and job.get('state')=='completed' and job.get('cleanup_verified') is True:
                                self.journal.record_receipt(entry['id'],'applied',{'engine_session':entry['engine_session'],
                                    'call_id':part.get('callID'),'job_id':job['id'],'observation':'owned_checkpoint_command_completed'})
                                return
            # Other guidance still needs an independent semantic application receipt.

    def _evidence_gate(self, feedback):
        """The same failing set twice in a row: require evidence before more edits.

        The model tends to rewrite on a guess when a test keeps failing, even when its
        own log already holds the answer. One automatic notice per distinct failing set.
        """
        failed = sorted(feedback['summary'].get('failed') or [])
        previous, self.state['last_failed_set'] = self.state.get('last_failed_set'), failed
        if not failed or failed != previous:return
        key = 'evidence-' + hashlib.sha256(json.dumps(failed).encode()).hexdigest()[:16]
        if key in self.state.setdefault('notices', []):return
        self.state['notices'].append(key)
        text = (EVIDENCE_NOTICE % {'failed': ', '.join(failed), 'job': feedback['id']})
        try:self.engine.session.notice(key, text)
        except Exception as error:  # a notice is advisory; never stop the execution for it
            self.state['notice_error'] = type(error).__name__;return
        self._report(key, 'Harness notice sent to the worker: same failing set twice (%s); evidence required '
                     'before further edits.' % ', '.join(failed))

    def _engine_error(self):
        """Stop at once on a fatal engine error instead of waiting for stale progress."""
        root = getattr(self.engine, 'root', None)
        if root is None:return False
        path = root / 'server.log'
        try:size = path.stat().st_size
        except OSError:return False
        offset = self.state.get('engine_log_offset', self.engine.state.get('server_log_offset', 0))
        if size < offset:offset = 0
        with path.open('rb') as stream:
            stream.seek(max(offset, size - 1024 * 1024));text = stream.read().decode(errors='replace')
        self.state['engine_log_offset'] = size
        match = re.search(r'level=ERROR[^\n]*?(request exceeds the context window[^"\n]*)', text)
        if not match:return False
        self._report('engine-error-%d' % size, 'Harness stop: the model engine failed fatally (%s). The workspace '
                     'is kept; the manager will checkpoint and re-dispatch.' % match.group(1)[:300])
        self._stop('engine_context_exhausted')
        return True

    def _report(self, key, body):
        """Best-effort visibility for the manager; never changes the execution itself."""
        try:
            self.client.post_selected_message(self.selection['thread_id'], body, 'report-' + key,
                                              self.selection['manager_agent_id'])
            self.state.pop('report_error', None)
        except (BoardError, OSError, RuntimeError, ValueError) as error:
            self.state['report_error'] = type(error).__name__

    def _escalate(self, reason, evidence):
        digest = hashlib.sha256(encoded([reason, evidence]).encode()).hexdigest()
        saved = self.store.get('worker_blocker')
        if saved and saved.get('digest') == digest: return
        payload = {'digest': digest, 'reason': reason, 'evidence': evidence,
                   'created_at': self.clock(), 'state': 'prepared'}
        self.store.save({'worker_blocker': payload})
        body = ('Harness blocker: ' + reason + '\n' + json.dumps(evidence, ensure_ascii=False)[:8000]
            + '\nCoding is paused with a durable checkpoint; please send scoped guidance or stop.')
        self.client.post_selected_message(self.selection['thread_id'], body, 'blocker-' + digest,
                                          self.selection['manager_agent_id'])
        payload['state'] = 'posted';self.store.save({'worker_blocker': payload})
        self.state['blocker_count'] = self.state.get('blocker_count', 0) + 1
        # A new build-control hash is an approval boundary, not a failed
        # implementation attempt. Bound both lanes independently while keeping
        # the same pause, exact-hash validation, time/step caps and stop path.
        review = reason == 'build_control_review_required'
        counter = 'build_review_count' if review else 'failure_blocker_count'
        self.state[counter] += 1
        if self.state[counter] > 3:
            self._stop('build_review_budget_elapsed' if review else 'blocker_budget_elapsed');return
        self.engine.session.abort({'id': 'blocker-' + digest, 'reason': reason})
        if hasattr(self.validation, 'pause'): self.validation.pause()
        self.state.update(phase='waiting_for_manager', manager_wait_deadline=self.clock()+660,
                          checkpoint=self.validation.checkpoint())
        self.engine.state['progress_at'] = self.clock()
        self.journal.save_checkpoint(self.selection['ticket_id'], self.engine.state['session_id'], self.state['checkpoint'])
        self._save()

    def close(self):
        try:
            if self.state['phase'] not in ('stopped', 'review'):
                self._stop('supervisor_shutdown')
        finally:
            if hasattr(self.validation, 'close'): self.validation.close()
