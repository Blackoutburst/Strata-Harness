"""Persistent Board control plane, independent of model availability.

Only a locally selected task can attach an execution adapter. Merely pairing a
worker or receiving a Board message cannot start coding or discover backlog.
"""

from dataclasses import asdict
import json
import os
from pathlib import Path
import stat
import time
import traceback

from .board import BoardClient, BoardError, MCPHTTP, TaskLease
from .coordination import Identity, Journal, JournalConflict, required_text
from .coordinator import Coordinator, Policy, WorkerState


def private_json(path):
    path = Path(path).expanduser()
    # Do not follow credential symlinks or accept group/world-readable secrets.
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('Runtime configuration must be a private owned regular file')
    with path.open() as source:
        value = json.load(source)
    if not isinstance(value, dict):
        raise ValueError('Runtime configuration must be an object')
    return value


# How long a Board that does not answer at all is retried before the execution stops.
BOARD_UNAVAILABLE_GRACE_SECONDS = 180

class IdleWorker:
    """No worker process exists; this observation cannot release an old claim."""
    def observe(self, now):
        return WorkerState(False, now)

    def tick(self, now, backend):
        pass

    def quiesce(self, intention):
        raise JournalConflict('No execution adapter exists to verify cleanup')

    def close(self):
        pass


class Runtime:
    def __init__(self, store, configuration_file, client_factory=None, worker_factory=None, clock=time.time):
        self.store, self.path, self.clock = store, Path(configuration_file).expanduser(), clock
        self.client_factory, self.worker_factory = client_factory, worker_factory
        self.client = self.journal = self.coordinator = self.lease = None
        self.worker = IdleWorker()
        self.next_connect = 0
        self.failures = 0
        self.board_unavailable_since = None
        self.token_env = 'STRATA_HARNESS_OWN_BOARD_TOKEN'
        self.owns_token = False
        self.stopped = False

    def _connect(self):
        config = private_json(self.path)
        board = private_json(config['board_file'])
        identity = Identity(**board['identity'])
        selected = config.get('selected_task')
        if selected is not None:
            if not isinstance(selected, dict): raise ValueError('Explicit selected task required')
            for key in ('ticket_id', 'thread_id', 'authorization_id'):
                required_text(selected.get(key))
        tickets = [selected['ticket_id']] if selected else []
        threads = [selected['thread_id']] if selected else config.get('control_thread_ids', [])
        if self.client_factory:
            client = self.client_factory(board, identity, tickets, threads)
        else:
            if self.token_env in os.environ and not self.owns_token:
                raise ValueError('Reserved worker token variable already exists')
            os.environ[self.token_env] = required_text(board['token'])
            self.owns_token = True
            client = BoardClient(MCPHTTP(board['endpoint'], self.token_env), identity,
                board['identity_key'], tickets, board.get('label', 'Strata harness worker'), threads)
        client.register()
        if board.get('profile_version') != client.profile_version:
            raise BoardError('paired_profile_version_changed')
        journal = Journal(self.store, identity)
        # The immutable admission selection is local operator configuration, not
        # an instruction parsed from untrusted Board prose.
        admission = {'identity': asdict(identity), 'selected_task': selected}
        previous = self.store.get('runtime_admission')
        idle_intake = (previous is not None and previous.get('identity') == admission['identity']
                       and previous.get('selected_task') is None and selected is not None
                       and self.store.get('managed_engine') is None and self.store.get('execution_worker') is None)
        if previous is not None and previous != admission and not idle_intake:
            raise JournalConflict('Runtime identity/selection changed; explicit reconciliation required')
        self.store.save({'runtime_admission': admission})
        worker = IdleWorker()
        lease = None
        if selected:
            if not self.worker_factory:
                raise ValueError('Selected task has no verified execution adapter')
            worker = self.worker_factory(config, journal, client)
            lease = TaskLease(journal, client, selected['ticket_id'],
                              lambda: worker.observe(self.clock()).active, self.clock)
            worker.attach_lease(lease)
        coordinator = Coordinator(self.store, journal, client,
            lambda: worker.observe(self.clock()), worker.quiesce, lease,
            Policy(**config.get('coordination', {})))
        if (selected is None and coordinator.state.get('stop_reason') == 'supervisor_shutdown'
                and worker.observe(self.clock()).active is False):
            coordinator.state.update(stop_reason=None, quiesce_request=None, mode='idle')
            self.store.save({'coordination_control': coordinator.state})
        self.client, self.journal, self.worker = client, journal, worker
        self.lease, self.coordinator = lease, coordinator
        self.store.save({'runtime_identity': {**asdict(identity), 'profile_version': client.profile_version},
                         'runtime': {'state': 'connected', 'selected_ticket': tickets[0] if tickets else None}})

    def tick(self, backend):
        if self.stopped: return
        now = self.clock()
        if self.coordinator is None:
            if now < self.next_connect: return
            try:
                self._connect()
                self.failures = 0
            except FileNotFoundError:
                self._waiting('waiting_for_pairing_or_configuration', now)
                return
            except (BoardError, JournalConflict, RuntimeError, ValueError, KeyError, TypeError, OSError) as error:
                category = error.category if isinstance(error, BoardError) else type(error).__name__
                self._waiting('connection_requires_attention', now, category)
                return
        try:
            self.coordinator.tick(now, backend)
            self.worker.tick(now, backend)
            state = self.coordinator.tick(self.clock(), backend)
            self.store.save({'runtime': {'state': state['mode'], 'checked_at': now,
                'selected_ticket': self.lease.ticket_id if self.lease else None}})
            self.board_unavailable_since = None
        except BoardError as error:
            # A request that got no response at all (tunnel reconnecting, board restarting)
            # is retried on later ticks for a bounded window; every Board write is journaled
            # and idempotent, so the next tick reconciles it. Anything else, or a longer
            # outage, still stops the execution below.
            if error.category == 'unavailable' and now - (self.board_unavailable_since or now) < BOARD_UNAVAILABLE_GRACE_SECONDS:
                self.board_unavailable_since = self.board_unavailable_since or now
                self.store.save({'runtime': {'state': 'board_unavailable_retrying', 'checked_at': now,
                                             'since': self.board_unavailable_since}})
                return
            self._execution_error(error, now, backend)
        except (JournalConflict, RuntimeError, ValueError, OSError) as error:
            self._execution_error(error, now, backend)

    def _execution_error(self, error, now, backend):
        # No blind reconnect/restart after an execution-side error. The same
        # worker and intentions remain available to quiesce/reconcile.
        diagnostic=self.store.path.parent/('runtime-error-'+str(time.time_ns())+'.log')
        diagnostic.write_text(traceback.format_exc());diagnostic.chmod(0o600)
        receipt={'error_type':type(error).__name__,'category':getattr(error,'category',None),
                 'http_status':getattr(error,'status',None),'checked_at':now,'log':str(diagnostic)}
        self.store.save({'runtime': {'state': 'execution_requires_reconciliation',**receipt},
                         'last_runtime_error':receipt},('runtime_execution_error',receipt))
        self.coordinator.state['stop_reason'] = 'runtime_execution_error'
        self.coordinator.tick(now, backend)

    def _waiting(self, state, now, category=None):
        self.failures = min(self.failures + 1, 8)
        self.next_connect = now + min(300, 5 * 2 ** (self.failures - 1))
        self.store.save({'runtime': {'state': state, 'checked_at': now, 'error_type': category,
                                     'retry_at': self.next_connect}})

    def close(self):
        self.stopped = True
        try:
            self.worker.close()
            if self.coordinator and (self.lease is not None or self.worker.observe(self.clock()).active):
                self.coordinator.state['stop_reason'] = self.coordinator.state['stop_reason'] or 'supervisor_shutdown'
                self.coordinator.tick(self.clock(), 'waiting_for_model')
        finally:
            if self.owns_token:
                os.environ.pop(self.token_env, None)
