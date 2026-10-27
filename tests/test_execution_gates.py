import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch
from strata_board_harness.execution import from_config


class ExecutionGateTests(unittest.TestCase):
    def test_partial_or_other_scope_readiness_cannot_construct_worker_or_call_bridge(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);work=root/'workspace';work.mkdir()
            p=root/'readiness.json'
            config={'selected_task':{'ticket_id':'selected','authorization_id':'human-pilot','baseline':'baseline'},
                'engine':{'workspace':str(work)},'readiness_file':str(p)}
            for value in ({'selection':{},'gates':{}}, {'selection':{'ticket_id':'selected','authorization_id':'human-pilot',
                    'baseline':'baseline','workspace':str(work.resolve())},'gates':{'fake_lifecycle':True}}):
                p.write_text(json.dumps(value));p.chmod(0o600)
                with patch('strata_board_harness.execution.EmulatorClient') as bridge:
                    with self.assertRaises(ValueError):from_config(config,SimpleNamespace(),SimpleNamespace())
                    bridge.assert_not_called()


if __name__=='__main__':unittest.main()
