from pathlib import Path
import subprocess
import tempfile
import threading
import unittest

from strata_board_harness.builds import AndroidBuilds, namespace_command


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve();self.work=self.root/'work';self.work.mkdir()
        subprocess.run(['git','init','-q',str(self.work)],check=True)
        (self.work/'gradlew').write_text('baseline wrapper')
        (self.work/'.gitignore').write_text('local.properties\nbuild/\n.gradle/\n')
        subprocess.run(['git','-C',str(self.work),'add','.'],check=True)
        subprocess.run(['git','-C',str(self.work),'-c','user.name=Fixture','-c','user.email=fixture@localhost',
                        'commit','-qm','fixture baseline'],check=True)
        self.base=subprocess.check_output(['git','-C',str(self.work),'rev-parse','HEAD'],text=True).strip()
        self.binary=self.root/'bwrap-fixture';self.binary.write_text('#!/bin/sh\necho "BUILD FAILED: full diagnostic"\nexit 37\n');self.binary.chmod(0o700)
        self.sdk=self.root/'sdk';self.sdk.mkdir()
        self.builds=AndroidBuilds(self.work,self.base,self.binary,self.sdk,self.root/'gradle',self.root/'receipts')

    def test_commit_without_changes_is_an_ordinary_failure_not_an_uncertain_effect(self):
        import threading
        result=self.builds.commit('nothing_new',threading.Event())
        self.assertEqual(result['exit_code'],1);self.assertTrue(result['cleanup_verified'])
        self.assertIn('Nothing to commit; HEAD is already '+self.base,result['output'])
        (self.work/'Note.kt').write_text('class Note')
        done=self.builds.commit('add_note',threading.Event())
        self.assertEqual(done['exit_code'],0)
        self.assertNotEqual(subprocess.check_output(['git','-C',str(self.work),'rev-parse','HEAD'],text=True).strip(),self.base)

    def test_isolated_command_clears_env_and_omits_host_home_network_and_devices(self):
        args=namespace_command(self.binary,self.work,self.sdk,self.root/'gradle')
        self.assertIn('--unshare-all',args);self.assertIn('--clearenv',args)
        self.assertNotIn('--share-net',args)
        self.assertNotIn('/home/ubuntu',args)
        self.assertNotIn('/Users',args)
        self.assertNotIn('/dev/bus/usb',args)
        self.assertEqual(args[-1],'--')

    def test_nonzero_build_and_full_log_are_real_failure_evidence(self):
        result=self.builds.build(None,threading.Event())
        self.assertEqual(result['exit_code'],37);self.assertEqual(result['process_exit_code'],37)
        self.assertIn('full diagnostic',result['output']);self.assertTrue(result['cleanup_verified'])
        self.assertNotIn('successful_build_at',self.builds.status())

    def test_changed_build_control_requires_specific_manager_hash_approval(self):
        (self.work/'gradlew').write_text('changed wrapper')
        result=self.builds.build(None,threading.Event())
        self.assertEqual(result['exit_code'],78)
        self.assertEqual(result['requires_manager_review'][0]['path'],'gradlew')
        self.assertNotIn('build_started_at',self.builds.status())

    def test_new_gradle_init_file_and_symlink_cannot_evade_control_gate(self):
        (self.work/'evil.gradle.kts').write_text('new control script')
        self.assertEqual(self.builds.control_changes()[0]['path'],'evil.gradle.kts')
        (self.work/'evil.gradle.kts').unlink();(self.work/'link').symlink_to('/etc/passwd')
        with self.assertRaises(ValueError):self.builds.control_changes()

    def test_checkpoint_tracks_uncommitted_source_and_owned_admission_survives_local_commit(self):
        before=self.builds.checkpoint();(self.work/'test.kt').write_text('owned new source')
        after=self.builds.checkpoint()
        self.assertNotEqual(before['source_sha256'],after['source_sha256']);self.assertFalse(after['clean'])
        self.builds.git('add','.');self.builds.git('commit','-m','fixture implementation')
        restored=AndroidBuilds(self.work,self.base,self.binary,self.sdk,self.root/'gradle',self.root/'receipts')
        self.assertTrue(restored.checkpoint()['clean'])
        self.assertEqual(after['source_sha256'],restored.checkpoint()['source_sha256'])


if __name__=='__main__':unittest.main()
