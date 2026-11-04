import unittest

from strata_board_harness.gateway import JobGateway
from strata_board_harness.local.cli import simulator_config
from strata_board_harness.local.ios_tools import CLASS_FILTER, parse_test_results, summarize

DOCUMENT = {'testNodes': [{'nodeType': 'Test Plan', 'name': 'App', 'children': [
    {'nodeType': 'UI test bundle', 'name': 'AppUITests', 'children': [
        {'nodeType': 'Test Suite', 'name': 'FirstRunTests', 'children': [
            {'nodeType': 'Test Case', 'name': 'test1_Wizard()', 'result': 'Passed', 'children': []},
            {'nodeType': 'Test Case', 'name': 'test3_Gate()', 'result': 'Failed', 'children': [
                {'nodeType': 'Failure Message', 'name': 'XCTAssertTrue failed - unlock screen'},
                {'nodeType': 'Failure Message', 'name': 'Test crashed with signal kill.'}]}]},
        {'nodeType': 'Test Suite', 'name': 'AutoFillTests', 'children': [
            {'nodeType': 'Test Case', 'name': 'test1_Fill()', 'result': 'Skipped', 'children': [
                {'nodeType': 'Skip Message', 'name': 'provider never enabled'}]}]}]}]}]}


class IosToolsTests(unittest.TestCase):
    def test_result_bundle_is_summarised_like_the_android_tool(self):
        cases = parse_test_results(DOCUMENT)
        self.assertEqual([(c['class'], c['test'], c['result']) for c in cases],
                         [('FirstRunTests', 'test1_Wizard', 'Passed'), ('FirstRunTests', 'test3_Gate', 'Failed'),
                          ('AutoFillTests', 'test1_Fill', 'Skipped')])
        counts, lines = summarize(cases)
        self.assertEqual((counts['passed'], counts['failed'], counts['skipped'], counts['crashed']), (1, 1, 1, True))
        self.assertIn('FAIL FirstRunTests/test3_Gate: XCTAssertTrue failed - unlock screen | Test crashed with signal kill.',
                      lines)
        self.assertIn('SKIP AutoFillTests/test1_Fill: provider never enabled', lines)
        self.assertEqual(lines[-1], 'Tests run: 3, Failures: 1, Skipped: 1')
        self.assertEqual(summarize(cases[:1])[1][-1], 'OK (1 tests, 0 skipped)')

    def test_class_filters(self):
        self.assertTrue(CLASS_FILTER.fullmatch('class:AppUITests/FirstRunTests'))
        self.assertTrue(CLASS_FILTER.fullmatch('class:AppUITests/FirstRunTests/test1_Wizard'))
        self.assertFalse(CLASS_FILTER.fullmatch('class:AppUITests/FirstRunTests; rm -rf /'))
        with self.subTest('the gateway accepts them'):
            import tempfile
            with tempfile.TemporaryDirectory() as directory:
                done = {'exit_code': 0, 'output': 'OK', 'cleanup_verified': True}
                gateway = JobGateway(directory, {'test': lambda argument, cancelled: done})
                with self.assertRaises(ValueError):
                    gateway.submit('test', 'class:../../etc')
                job = gateway.submit('test', 'class:AppUITests/FirstRunTests/test1_Wizard')
                import time
                for _ in range(100):
                    if gateway.status(job['id'])['state'] not in ('prepared', 'running'):
                        break
                    time.sleep(.05)
                self.assertEqual(gateway.status(job['id'])['state'], 'completed')

    def test_simulator_setting(self):
        config = simulator_config('9B9C2B7A-28FF-4C17-BE0F-A7D57AC05C9F,project=App.xcodeproj,scheme=App,'
                                  'helper_start=scripts/a.sh,max_test_seconds=240')
        self.assertEqual(config, {'udid': '9B9C2B7A-28FF-4C17-BE0F-A7D57AC05C9F', 'project': 'App.xcodeproj',
                                  'scheme': 'App', 'helper_start': 'scripts/a.sh', 'max_test_seconds': 240})
        with self.assertRaises(ValueError):
            simulator_config('not-a-udid,project=A,scheme=B')


if __name__ == '__main__':
    unittest.main()
