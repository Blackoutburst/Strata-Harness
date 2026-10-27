import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('harness_tool', Path(__file__).resolve().parents[1] / 'scripts/harness_tool.py')
harness_tool = importlib.util.module_from_spec(spec);spec.loader.exec_module(harness_tool)


class DigestTests(unittest.TestCase):
    def test_test_digest_keeps_failures_and_evidence_but_drops_the_raw_dump(self):
        dump = '\n'.join('    RTC_WAKEUP #%d: Alarm{x}' % i for i in range(5000))
        output = ('INSTRUMENTATION_STATUS: stack=java.lang.AssertionError: boot restore must reschedule\n'
                  '\tat tech.primal.Foo.test(Foo.kt:12)\n' + dump +
                  '\n10-27 D/E2E_DUMP( 1): restore ran\nTests run: 5,  Failures: 1\n')
        view = harness_tool.digest({'id': 'job-1', 'operation': 'test', 'state': 'completed', 'exit_code': 1,
                                    'output_tail': output, 'instrumentation': {'counts': {'failed': 1}}})
        self.assertNotIn('output_tail', view)
        self.assertIn('boot restore must reschedule', view['output_excerpt'])
        self.assertIn('restore ran', view['output_excerpt'])
        self.assertIn('Tests run: 5', view['output_excerpt'])
        self.assertLessEqual(len(view['output_excerpt']), harness_tool.DIGEST_CHARS + 40)
        self.assertEqual(view['instrumentation']['counts']['failed'], 1)
        self.assertIn('harness-tool logs job-1:0', view['full_log'])

    def test_build_digest_keeps_compiler_errors_and_the_tail(self):
        output = 'noise\n' * 3000 + 'e: file:///w/Foo.kt:3:1 Unresolved reference: bar\n' + 'tail\n' * 10 + 'BUILD FAILED in 9s\n'
        view = harness_tool.digest({'id': 'job-2', 'operation': 'build', 'state': 'completed', 'exit_code': 1,
                                    'output_tail': output})
        self.assertIn('Unresolved reference: bar', view['output_excerpt'])
        self.assertIn('BUILD FAILED', view['output_excerpt'])
        self.assertLess(len(view['output_excerpt']), 2000)


if __name__ == '__main__': unittest.main()
