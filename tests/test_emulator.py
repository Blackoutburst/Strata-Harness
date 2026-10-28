from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from strata_board_harness.emulator import EmulatorRuntime, PACKAGE, TEST_PACKAGE, instrumentation_summary


FAILED='''INSTRUMENTATION_STATUS: class=OwnedJourney
INSTRUMENTATION_STATUS: test=works
INSTRUMENTATION_STATUS_CODE: 1
INSTRUMENTATION_STATUS: class=OwnedJourney
INSTRUMENTATION_STATUS: test=works
INSTRUMENTATION_STATUS_CODE: 0
INSTRUMENTATION_STATUS: class=OwnedJourney
INSTRUMENTATION_STATUS: test=fails
INSTRUMENTATION_STATUS_CODE: 1
INSTRUMENTATION_STATUS: class=OwnedJourney
INSTRUMENTATION_STATUS: test=fails
INSTRUMENTATION_STATUS_CODE: -2
FAILURES!!!
Tests run: 2, Failures: 1
INSTRUMENTATION_CODE: -1
'''


class EmulatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve();self.calls=[];self.restore_ok=True
        import zipfile
        for name in ('app.apk','test.apk'):
            with zipfile.ZipFile(self.root/name,'w') as archive:archive.writestr('classes.dex',b'owned dex fixture')
        self.config={'serial':'emulator-5556','avd':'caregiver35','adb':'/usr/bin/true','aapt2':'/usr/bin/true',
            'emulator_pid':12345,'emulator_stamp':'owned -avd caregiver35 -ports 5556,5557 -no-window -no-audio -no-snapshot',
            'avd_home':str(self.root/'avd')}
        def runner(argv,**kwargs):
            self.calls.append(argv)
            if argv[0]=='lsof':return SimpleNamespace(returncode=0,stdout='n'+self.config['avd_home']+'/caregiver35.avd/userdata.img')
            if 'badging' in argv:
                package=TEST_PACKAGE if str(argv[-1]).endswith('test.apk') else PACKAGE
                return SimpleNamespace(returncode=0,stdout="package: name='"+package+"' versionCode='1'")
            self.assertEqual(argv[1:3],['-s','emulator-5556'])
            if 'snapshot' in argv:
                return SimpleNamespace(returncode=0 if self.restore_ok or 'load' not in argv else 1,
                                       stdout='OK\n' if self.restore_ok or 'load' not in argv else 'KO',stderr='')
            if argv[-3:]==['emu','avd','name']:out='caregiver35\nOK\n'
            elif argv[-3:]==['shell','dumpsys','user']:out='State: RUNNING_UNLOCKED'
            elif 'instrumentation' in argv:out='No active instrumentation'
            elif 'install' in argv:out='Success'
            elif 'resolve-activity' in argv:out=PACKAGE+'/.MainActivity'
            elif 'get-role-holders' in argv:out=PACKAGE+'\n'
            elif 'screencap' in argv:out=b'\x89PNG\r\n\x1a\nowned-fixture'
            elif argv[3:5]==['logcat','-d']:out='10-26 14:00:00.000 D/E2E_DUMP( 42): picker texts: [Gmail, Photos]\n'
            else:out=''
            return SimpleNamespace(returncode=0,stdout=out,stderr='')
        class Child:
            pid=98765
            def poll(self):return 0
            def wait(self,timeout=None):return 0
        def popen(argv,**kwargs):
            self.calls.append(argv);self.assertEqual(argv[1:3],['-s','emulator-5556'])
            self.assertIn('300000',argv);kwargs['stdout'].write(FAILED);return Child()
        self.patch=patch('strata_board_harness.emulator.process_stamp',return_value=self.config['emulator_stamp'])
        self.patch.start();self.addCleanup(self.patch.stop)
        sleeper=patch('strata_board_harness.emulator.time.sleep');sleeper.start();self.addCleanup(sleeper.stop)
        self.runtime=EmulatorRuntime(self.config,self.root/'receipts',runner=runner,popen=popen)

    def test_failed_instrumentation_is_not_passed_for_zero_adb_exit_and_rollback_is_returned(self):
        result=self.runtime.test_apks(self.root/'app.apk',self.root/'test.apk',threading.Event())
        self.assertEqual(result['adb_exit_code'],0);self.assertEqual(result['exit_code'],1)
        self.assertEqual(result['instrumentation']['counts']['passed'],1)
        self.assertEqual(result['instrumentation']['counts']['failed'],1)
        self.assertTrue(result['cleanup_verified']);self.assertTrue(self.runtime.clean())
        snapshots=[args for args in self.calls if 'snapshot' in args]
        self.assertEqual([args[-2] for args in snapshots],['save','load','delete'])
        self.assertEqual(len({args[-1] for args in snapshots}),1)
        self.assertTrue(all('emulator-5556' in args for args in self.calls if args[0]!='lsof' and 'badging' not in args))

    def test_run_output_carries_this_runs_logcat_diagnostics(self):
        result=self.runtime.test_apks(self.root/'app.apk',self.root/'test.apk',threading.Event())
        self.assertIn('picker texts: [Gmail, Photos]',result['output'])
        clear=next(i for i,a in enumerate(self.calls) if a[3:7]==['logcat','-b','all','-c'])
        scales=[i for i,a in enumerate(self.calls) if a[3:6]==['shell','settings','put'] and a[-1]=='0']
        self.assertEqual(len(scales),3)
        launch=next(i for i,a in enumerate(self.calls) if 'instrument' in a)
        self.assertLess(clear,launch);self.assertLess(max(scales),launch)
        self.assertEqual(result['instrumentation']['counts']['failed'],1)

    def test_cancel_tolerates_a_child_that_exits_before_the_group_signal(self):
        class Racing:
            pid=4242;polls=0
            def poll(self):
                self.polls+=1;return None if self.polls==1 else 0
            def wait(self,timeout=None):return 0
        self.runtime.child=Racing()
        with patch('strata_board_harness.emulator.os.killpg',side_effect=PermissionError(1,'Operation not permitted')):
            self.runtime.cancel()
        class Stuck(Racing):
            def poll(self):return None
            def wait(self,timeout=None):
                import subprocess;raise subprocess.TimeoutExpired('adb',timeout)
        self.runtime.child=Stuck()
        with patch('strata_board_harness.emulator.os.killpg',side_effect=PermissionError(1,'Operation not permitted')):
            with self.assertRaises(RuntimeError):self.runtime.cancel()

    def test_process_deaths_report_crash_buffer_and_kills_for_the_target_only(self):
        from strata_board_harness.emulator import process_deaths
        def call(*args,timeout=15):
            if args[:4]==('logcat','-d','-b','crash'):
                return SimpleNamespace(returncode=0,stdout='F/libc ( 42): Fatal signal 11 (SIGSEGV) in '+PACKAGE+'\n')
            if args[:4]==('logcat','-d','-b','events'):
                return SimpleNamespace(returncode=0,stdout=('I/am_kill ( 1): [0,42,'+PACKAGE+',900,stop]\n'
                                                            'I/am_kill ( 1): [0,43,com.other,900,stop]\n'))
            return SimpleNamespace(returncode=0,stdout='I/ActivityManager( 1): Process '+PACKAGE+' (pid 42) has died\n')
        text=process_deaths(call,PACKAGE)
        self.assertIn('SIGSEGV',text);self.assertIn('am_kill',text);self.assertIn('has died',text)
        self.assertNotIn('com.other',text)
        self.assertEqual(process_deaths(lambda *a,**k:SimpleNamespace(returncode=0,stdout=''),PACKAGE),'')

    def test_role_settles_only_when_held_and_permissions_stop_changing(self):
        from strata_board_harness.emulator import settle_role
        dumps=iter(['READ_CONTACTS: granted=false','READ_CONTACTS: granted=true','READ_CONTACTS: granted=true',
                    'READ_CONTACTS: granted=true']+['READ_CONTACTS: granted=true']*10)
        holders=iter(['','com.other']+[PACKAGE]*20)
        def call(*args,timeout=15):
            if 'get-role-holders' in args:return SimpleNamespace(returncode=0,stdout=next(holders))
            return SimpleNamespace(returncode=0,stdout='    android.permission.'+next(dumps)+'\n')
        now=[0.0]
        self.assertTrue(settle_role(call,'android.app.role.DIALER',PACKAGE,lambda:now[0],lambda s:now.__setitem__(0,now[0]+s)))
        self.assertGreaterEqual(now[0],3)
        never=lambda *a,**k:SimpleNamespace(returncode=0,stdout='com.other')
        self.assertFalse(settle_role(never,'android.app.role.DIALER',PACKAGE,lambda:now[0],lambda s:now.__setitem__(0,now[0]+s),timeout=5))

    def test_failed_rollback_fences_runtime_even_if_test_process_stopped(self):
        self.restore_ok=False
        result=self.runtime.test_apks(self.root/'app.apk',self.root/'test.apk',threading.Event())
        self.assertFalse(result['cleanup_verified']);self.assertFalse(self.runtime.clean())
        with self.assertRaises(RuntimeError):EmulatorRuntime(self.config,self.root/'receipts')

    def test_wrong_process_or_physical_serial_is_rejected_before_adb(self):
        with patch('strata_board_harness.emulator.process_stamp',return_value='different process'):
            with self.assertRaises(RuntimeError):self.runtime.verify()
        self.assertEqual(self.calls,[])
        config=dict(self.config,serial='47081JEKB09462')
        with self.assertRaises(ValueError):EmulatorRuntime(config,self.root/'other')

    def test_skipped_and_crashed_or_started_without_result_are_not_passes(self):
        output=FAILED.replace('INSTRUMENTATION_STATUS_CODE: -2','INSTRUMENTATION_STATUS_CODE: -3')
        counts=instrumentation_summary(output)['counts']
        self.assertEqual(counts['skipped'],1);self.assertEqual(counts['passed'],1)
        self.assertFalse(counts['runner_success'])
        output='INSTRUMENTATION_STATUS: test=crashes\nINSTRUMENTATION_STATUS_CODE: 1\nINSTRUMENTATION_RESULT: shortMsg=Process crashed\n'
        counts=instrumentation_summary(output)['counts']
        self.assertEqual(counts['passed'],0);self.assertEqual(counts['started_without_result'],1);self.assertTrue(counts['crashed'])

    def test_empty_success_text_does_not_count_as_a_runtime_pass(self):
        counts=instrumentation_summary('OK (0 tests)\n')['counts']
        self.assertEqual(counts['passed'],0);self.assertFalse(counts['runner_success'])

    def test_phone_default_keeps_dialer_role_fixture_and_default_runner(self):
        self.runtime.test_apks(self.root/'app.apk',self.root/'test.apk',threading.Event())
        self.assertTrue(any('add-role-holder' in args and 'android.app.role.DIALER' in args for args in self.calls))
        instrument=next(args for args in self.calls if 'instrument' in args)
        self.assertEqual(instrument[-1],TEST_PACKAGE+'/androidx.test.runner.AndroidJUnitRunner')

    def test_selected_non_phone_target_uses_its_packages_declared_runner_and_no_role(self):
        clock='tech.primal.attentive_os.clock'
        config=dict(self.config,target={'package':clock})
        calls=[]
        def runner(argv,**kwargs):
            calls.append(argv)
            if argv[0]=='lsof':return SimpleNamespace(returncode=0,stdout='n'+config['avd_home']+'/x')
            if 'badging' in argv:
                package=clock+'.test' if str(argv[-1]).endswith('test.apk') else clock
                return SimpleNamespace(returncode=0,stdout="package: name='"+package+"'")
            if 'snapshot' in argv:return SimpleNamespace(returncode=0,stdout='OK\n',stderr='')
            if argv[-3:]==['emu','avd','name']:out='caregiver35\nOK\n'
            elif argv[-3:]==['shell','dumpsys','user']:out='State: RUNNING_UNLOCKED'
            elif argv[-3:]==['pm','list','instrumentation']:
                out=('instrumentation:'+PACKAGE+'.test/androidx.test.runner.AndroidJUnitRunner (target='+PACKAGE+')\n'
                     'instrumentation:'+clock+'.test/'+clock+'.e2e.E2eTestRunner (target='+clock+')\n')
            elif 'instrumentation' in argv:out='No active instrumentation'
            elif 'install' in argv:out='Success'
            elif 'resolve-activity' in argv:out=clock+'/.MainActivity'
            elif 'screencap' in argv:out=b'\x89PNG\r\n\x1a\nowned-fixture'
            else:out=''
            return SimpleNamespace(returncode=0,stdout=out,stderr='')
        launched=[]
        class Child:
            pid=4
            def poll(self):return 0
            def wait(self,timeout=None):return 0
        def popen(argv,**kwargs):launched.append(argv);kwargs['stdout'].write(FAILED);return Child()
        runtime=EmulatorRuntime(config,self.root/'clock-receipts',runner=runner,popen=popen)
        result=runtime.test_apks(self.root/'app.apk',self.root/'test.apk',threading.Event())
        self.assertEqual(launched[0][-1],clock+'.test/'+clock+'.e2e.E2eTestRunner')
        self.assertEqual(result['target_package'],clock)
        self.assertFalse(any('add-role-holder' in args or 'get-role-holders' in args for args in calls))
        self.assertTrue(result['cleanup_verified'])
        denied=runtime.test_apks(self.root/'app.apk',self.root/'test.apk',threading.Event(),scenario='denied')
        self.assertEqual(denied['exit_code'],78)
        with self.assertRaises(ValueError):runtime.test_apks(self.root/'app.apk',self.root/'test.apk',threading.Event(),
                                                              scenario='class:'+PACKAGE+'.e2e.Foreign')

    def test_target_outside_attentiveos_or_unknown_role_is_rejected(self):
        for target in ({'package':'com.android.contacts'},{'package':'tech.primal.attentive_os.clock','role':'dialer'},
                       {'package':'tech.primal.attentive_os.clock','denied_permissions':['../x']}):
            with self.assertRaises(ValueError):EmulatorRuntime(dict(self.config,target=target),self.root/'bad')

    def test_declared_but_unrun_cases_remain_explicit_after_crash(self):
        output='INSTRUMENTATION_STATUS: numtests=26\nINSTRUMENTATION_STATUS: test=crashes\nINSTRUMENTATION_STATUS_CODE: 1\nProcess crashed\n'
        counts=instrumentation_summary(output)['counts']
        self.assertEqual(counts['declared_total'],26);self.assertEqual(counts['unrun'],25)
        self.assertEqual(counts['started_without_result'],1)


if __name__=='__main__':unittest.main()
