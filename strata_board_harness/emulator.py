"""Owning-host adapter for the single explicitly selected silent emulator.

Every ADB call includes its pinned serial. An owned VM snapshot brackets each
run so settings, roles, permission flags, records, alarms and widgets are restored
without broad content-provider deletion. Snapshot failure fences further runs.
"""

import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import threading
import time
import zipfile

from .managed_engine import process_stamp, write_private


PACKAGE='tech.primal.attentive_os.phone'
TEST_PACKAGE=PACKAGE+'.test'
DEFAULT_RUNNER='androidx.test.runner.AndroidJUnitRunner'
# logcat filter specs appended to a run's output: test diagnostics, crashes, runner errors.
LOGCAT_TAGS=('E2E_DUMP:V','AndroidRuntime:E','TestRunner:E')
LOGCAT_MAX_CHARS=16000
# The first pilot's Phone fixture. Other selected tasks supply their own target in
# the bridge configuration; nothing outside an explicit target is ever touched.
PHONE_TARGET={'package':PACKAGE,'role':'android.app.role.DIALER',
    'denied_permissions':['android.permission.CALL_PHONE','android.permission.READ_CALL_LOG',
                          'android.permission.ANSWER_PHONE_CALLS']}


def selected_target(configuration):
    target=dict(configuration.get('target') or PHONE_TARGET)
    package=target.get('package','')
    if not re.fullmatch(r'tech\.primal\.attentive_os\.[a-z_]+(?:\.[a-z_]+)*',package):
        raise ValueError('Selected target package is outside AttentiveOS')
    role=target.get('role')
    if role is not None and not re.fullmatch(r'android\.app\.role\.[A-Z_]+',role):raise ValueError('Unselected role fixture')
    denied=list(target.get('denied_permissions') or [])
    if any(not re.fullmatch(r'android\.permission\.[A-Z_]+',p) for p in denied):raise ValueError('Unselected permission fixture')
    return {'package':package,'test_package':package+'.test','role':role,'denied_permissions':denied,
            'denied_marker':target.get('denied_marker') or package+'.e2e.RequiresDeniedPermissions'}


def instrumentation_runner(listing,test_package,package):
    """Runner declared by the installed selected test APK for the selected target."""
    for line in listing.splitlines():
        match=re.match(r'instrumentation:([^/\s]+)/(\S+) \(target=([^)\s]+)\)',line.strip())
        if match and match.group(1)==test_package and match.group(3)==package:
            runner=match.group(2)
            return test_package+runner if runner.startswith('.') else runner
    return DEFAULT_RUNNER


def instrumentation_summary(output):
    counts={'passed':0,'failed':0,'errors':0,'skipped':0,'started':0}
    statuses=[];bundle={};declared=None
    for line in output.splitlines():
        match=re.match(r'INSTRUMENTATION_STATUS: ([^=]+)=(.*)',line)
        if match:
            bundle[match.group(1)]=match.group(2)
            if match.group(1)=='numtests' and match.group(2).isdigit():declared=max(declared or 0,int(match.group(2)))
        match=re.match(r'INSTRUMENTATION_STATUS_CODE: (-?\d+)',line)
        if not match:continue
        code=int(match.group(1))
        if 'test' in bundle:
            if code==1:counts['started']+=1
            elif code in (0,-1,-2,-3,-4):
                counts[{0:'passed',-1:'errors',-2:'failed',-3:'skipped',-4:'skipped'}[code]]+=1
                statuses.append({'class':bundle.get('class'),'test':bundle['test'],'code':code})
        bundle={}
    completed=sum(counts[k] for k in ('passed','failed','errors','skipped'))
    counts['started_without_result']=max(0,counts['started']-completed)
    counts['declared_total']=declared
    counts['unrun']=max(0,declared-completed-counts['started_without_result']) if declared is not None else None
    counts['runner_success']=bool(re.search(r'^OK \(\d+ tests?\)',output,re.M)) and counts['passed']>0 and not counts['failed'] and not counts['errors']
    counts['crashed']='Process crashed' in output or 'INSTRUMENTATION_FAILED' in output
    return {'counts':counts,'tests':statuses}


class EmulatorRuntime:
    def __init__(self, configuration, receipts, runner=subprocess.run, popen=subprocess.Popen, clock=time.time):
        if configuration.get('serial')!='emulator-5556' or configuration.get('avd')!='caregiver35':
            raise ValueError('Only the selected isolated emulator is authorized')
        self.config,self.runner,self.popen,self.clock=configuration,runner,popen,clock
        self.directory=Path(receipts).resolve();self.directory.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.adb=str(Path(configuration['adb']).resolve(strict=True))
        self.serial='emulator-5556';self.child=None;self.lock=threading.Lock()
        self.target=selected_target(configuration)
        self.package,self.test_package=self.target['package'],self.target['test_package']
        self.state_file=self.directory/'runtime.json'
        self.state=json.loads(self.state_file.read_text()) if self.state_file.exists() else {'phase':'idle','cleanup_verified':True}
        if self.state.get('phase')!='idle':raise RuntimeError('Prior emulator effects require exact reconciliation')

    def call(self,*args,timeout=15,binary=False):
        return self.runner([self.adb,'-s',self.serial,*args],capture_output=True,text=not binary,timeout=timeout)

    def verify(self):
        pid=self.config['emulator_pid'];stamp=process_stamp(pid)
        if stamp!=self.config['emulator_stamp'] or not all(x in stamp for x in
            ('-avd caregiver35','-ports 5556,5557','-no-window','-no-audio','-no-snapshot')):
            raise RuntimeError('Owned silent emulator process identity changed')
        avd_home=Path(self.config['avd_home']).expanduser().resolve()
        files=self.runner(['lsof','-p',str(pid),'-Fn'],capture_output=True,text=True,timeout=10)
        if files.returncode or not any(line.startswith('n'+str(avd_home)+'/') for line in files.stdout.splitlines()):
            raise RuntimeError('Emulator is not using the owned AVD directory')
        current=self.call('emu','avd','name')
        users=self.call('shell','dumpsys','user')
        if current.returncode or not current.stdout.startswith('caregiver35') or 'RUNNING_UNLOCKED' not in users.stdout:
            raise RuntimeError('Selected emulator is not running unlocked')
        instrumentation=self.call('shell','dumpsys','activity','instrumentation')
        if instrumentation.returncode or re.search(r'ActiveInstrumentation\{|mActiveInstrumentation=InstrumentationRecord',instrumentation.stdout):
            raise RuntimeError('Another instrumentation execution may be active')

    def _save(self):write_private(self.state_file,self.state)

    def fingerprint(self):
        settings={}
        for namespace in ('system','secure','global'):
            result=self.call('shell','settings','list',namespace)
            if result.returncode:raise RuntimeError('Cannot record selected-emulator settings')
            settings[namespace]=dict(sorted(line.split('=',1) for line in result.stdout.splitlines() if '=' in line))
        role=''
        if self.target['role']:
            role=self.call('shell','cmd','role','get-role-holders','--user','0',self.target['role']).stdout.strip()
        package=self.call('shell','dumpsys','package',self.package)
        permissions=sorted(line.strip() for line in package.stdout.splitlines()
            if re.search(r'granted=(?:true|false)',line))
        return {'settings':settings,'role_holders':role,'package_permissions':permissions}

    def _snapshot(self):
        name='strata_harness_'+secrets.token_hex(12)
        before=self.fingerprint()
        self.state={'phase':'snapshot_prepared','snapshot':name,'cleanup_verified':False,'started_at':self.clock(),
                    'before':before};self._save()
        result=self.call('emu','avd','snapshot','save',name,timeout=60)
        if result.returncode or not result.stdout.strip().endswith('OK'):
            raise RuntimeError('Owned snapshot save failed')
        self.state['phase']='snapshot_saved';self._save()
        return name

    def _restore(self):
        name=self.state['snapshot']
        if process_stamp(self.config['emulator_pid']) != self.config['emulator_stamp']: return False
        # This is our exact snapshot; never select/delete a prefix-matched image.
        result=self.call('emu','avd','snapshot','load',name,timeout=60)
        if result.returncode or not result.stdout.strip().endswith('OK'):return False
        deadline=time.monotonic()+45
        while time.monotonic()<deadline:
            try:
                users=self.call('shell','dumpsys','user',timeout=5)
                if users.returncode==0 and 'RUNNING_UNLOCKED' in users.stdout:break
            except (OSError,subprocess.TimeoutExpired):pass
            time.sleep(.2)
        else:return False
        after=self.fingerprint()
        if after!=self.state.get('before'):
            self.state.update(phase='reconciliation_required',cleanup_verified=False,after=after,restore_error='fingerprint_changed')
            self._save();return False
        deletion=self.call('emu','avd','snapshot','delete',name,timeout=30)
        clean=deletion.returncode==0 and deletion.stdout.strip().endswith('OK')
        self.state.update(phase='idle' if clean else 'reconciliation_required',cleanup_verified=clean,
                          restored_at=self.clock());self._save()
        return clean

    def checkpoint(self):
        value={k:v for k,v in self.state.items() if k not in ('before','after')}
        if 'before' in self.state:value['before_sha256']=hashlib.sha256(json.dumps(self.state['before'],sort_keys=True).encode()).hexdigest()
        return value
    def clean(self):return self.state.get('cleanup_verified') is True and (self.child is None or self.child.poll() is not None)

    def cancel(self):
        with self.lock:child=self.child
        if child and child.poll() is None:
            self._signal_group(child,signal.SIGTERM)
            try:child.wait(timeout=3)
            except subprocess.TimeoutExpired:self._signal_group(child,signal.SIGKILL);child.wait(timeout=3)
            # Stopping the ADB client does not stop instrumentation on Android.
            for package in (self.test_package,self.package):self.call('shell','am','force-stop',package)

    @staticmethod
    def _signal_group(child,sig):
        # The ADB client can exit between poll() and the signal; macOS then answers
        # EPERM (zombie group) or ESRCH. Only an exited child makes that harmless.
        try:os.killpg(child.pid,sig)
        except (ProcessLookupError,PermissionError):
            try:child.wait(timeout=3)
            except subprocess.TimeoutExpired:raise RuntimeError('ADB client survived a refused signal') from None

    def screenshot(self,path):
        result=self.call('exec-out','screencap','-p',timeout=15,binary=True)
        if result.returncode or not result.stdout.startswith(b'\x89PNG\r\n\x1a\n') or len(result.stdout)>10*1024*1024:
            raise RuntimeError('Invalid selected-emulator screenshot')
        path.write_bytes(result.stdout);path.chmod(0o600)
        return {'path':str(path),'sha256':hashlib.sha256(result.stdout).hexdigest(),'bytes':len(result.stdout)}

    def test_apks(self,app_apk,test_apk,cancelled,scenario='normal'):
        PACKAGE,TEST_PACKAGE=self.package,self.test_package
        if scenario not in ('normal','denied') and not re.fullmatch(r'class:'+re.escape(PACKAGE)+r'\.[A-Za-z0-9_.#]+',scenario):
            raise ValueError('Unselected test filter')
        deadline=time.monotonic()+1500
        self.verify()
        root=self.directory/('run-'+secrets.token_hex(12));root.mkdir(mode=0o700)
        self._snapshot();log=root/'instrumentation.log';code=1;output='';result={}
        try:
            aapt=Path(self.config['aapt2']).resolve(strict=True)
            # Only the two selected app packages, inside our saved VM state.
            # Each host may have a different debug certificate; restore the VM
            # afterward rather than masking that with an unchecked install.
            for package in (TEST_PACKAGE,PACKAGE):
                installed=self.call('shell','pm','path',package)
                if installed.returncode==0 and installed.stdout.startswith('package:'):
                    removed=self.call('uninstall',package,timeout=60)
                    if removed.returncode or 'Success' not in removed.stdout:
                        raise RuntimeError('Selected package removal failed inside snapshot')
            for apk,package in ((Path(app_apk),PACKAGE),(Path(test_apk),TEST_PACKAGE)):
                info=self.runner([str(aapt),'dump','badging',str(apk)],capture_output=True,text=True,timeout=15)
                if info.returncode or not re.search(r"^package: name='"+re.escape(package)+r"'",info.stdout,re.M):
                    raise ValueError('APK package does not match selected target')
                installed=self.call('install','-r','-d','-t',str(apk),timeout=min(120,max(1,deadline-time.monotonic())))
                if installed.returncode or 'Success' not in installed.stdout:raise RuntimeError('APK install failed')
            ce=self.call('shell','run-as',PACKAGE,'test','-d','/data/user/0/'+PACKAGE)
            activity=self.call('shell','cmd','package','resolve-activity','--brief','-a','android.intent.action.MAIN',
                               '-c','android.intent.category.LAUNCHER',PACKAGE)
            if ce.returncode or activity.returncode or PACKAGE+'/' not in activity.stdout:
                raise RuntimeError('CE directory or target launcher activity unavailable after install')
            if self.target['role']:
                role=self.call('shell','cmd','role','add-role-holder','--user','0',self.target['role'],PACKAGE,timeout=30)
                if role.returncode:raise RuntimeError('Selected role fixture failed')
            permission_fixture={}
            if scenario=='denied':
                if not self.target['denied_permissions']:raise ValueError('Selected target has no denied-permission fixture')
                for permission in self.target['denied_permissions']:
                    revoked=self.call('shell','pm','revoke',PACKAGE,permission)
                    if revoked.returncode:raise RuntimeError('External denied-permission fixture failed')
                dump=self.call('shell','dumpsys','package',PACKAGE)
                for permission in self.target['denied_permissions']:
                    line=next((line.strip() for line in dump.stdout.splitlines() if line.strip().startswith(permission+':') and 'granted=' in line),'')
                    if 'granted=false' not in line:raise RuntimeError('Denied state not independently observed before host launch')
                    permission_fixture[permission]=line
            listing=self.call('shell','pm','list','instrumentation')
            runner=instrumentation_runner(listing.stdout if listing.returncode==0 else '',TEST_PACKAGE,PACKAGE)
            if not re.fullmatch(r'[A-Za-z0-9_.$]+',runner):raise ValueError('Unexpected instrumentation runner name')
            marker=self.target['denied_marker']
            descriptor=('L'+marker.replace('.','/')+';').encode()
            with zipfile.ZipFile(test_apk) as archive:
                marker_present=any(descriptor in archive.read(info) for info in archive.infolist()
                    if re.fullmatch(r'classes[0-9]*\.dex',info.filename) and info.file_size<=64*1024*1024)
            if scenario=='denied' and not marker_present:
                code=78
                result.update(exit_code=code,output='No denied-state annotation in this test APK; no tests executed',
                    scenario=scenario,external_permission_fixture=permission_fixture,
                    instrumentation={'counts':{'passed':0,'failed':0,'errors':0,'skipped':0,'started':0,
                        'unrun':None,'runner_success':False},'tests':[]})
                return result
            filter_args=['-e','annotation',marker] if scenario=='denied' else (
                ['-e','class',scenario[len('class:'):]] if scenario.startswith('class:') else (['-e','notAnnotation',marker] if marker_present else []))
            self.state['phase']='instrumenting';self._save()
            self.call('logcat','-c',timeout=15)  # so the dump below holds only this run's evidence
            with log.open('w') as stream:
                log.chmod(0o600)
                child=self.popen([self.adb,'-s',self.serial,'shell','am','instrument','-w','-r',
                    '-e','timeout_msec','300000',*filter_args,TEST_PACKAGE+'/'+runner],
                    stdout=stream,stderr=subprocess.STDOUT,start_new_session=True)
                with self.lock:self.child=child
                reason=None
                while child.poll() is None:
                    if cancelled.wait(.1):reason='cancelled';break
                    if time.monotonic()>=deadline:reason='connected_timeout';break
                if reason:self.cancel()
                adb_code=child.wait(timeout=10)
            output=log.read_text(errors='replace');summary=instrumentation_summary(output)
            # Test-side diagnostics (E2E_DUMP) and crashes only reach logcat; give the worker
            # this run's evidence instead of leaving it to guess from the assertion text.
            try:
                logcat=self.call('logcat','-d','-v','time','-s',*LOGCAT_TAGS,timeout=30)
                logcat_text=(logcat.stdout or '')[-LOGCAT_MAX_CHARS:] if logcat.returncode==0 else ''
            except (OSError,subprocess.TimeoutExpired):logcat_text=''
            if logcat_text.strip():
                (root/'logcat.txt').write_text(logcat_text);(root/'logcat.txt').chmod(0o600)
                output+='\n--- logcat (this run; tags '+' '.join(LOGCAT_TAGS)+') ---\n'+logcat_text
            code=124 if reason=='connected_timeout' else (130 if reason=='cancelled' else
                (0 if adb_code==0 and summary['counts']['runner_success'] and not summary['counts']['crashed'] else 1))
            result={'instrumentation':summary,'adb_exit_code':adb_code,'stop_reason':reason,'runner':runner,'target_package':PACKAGE,
                    'log_path':str(log),'scenario':scenario,'external_permission_fixture':permission_fixture,'connected_cap_seconds':1500,'per_test_timeout_seconds':300}
            if code:
                try:result['screenshot']=self.screenshot(root/'failure.png')
                except (OSError,RuntimeError,subprocess.TimeoutExpired):result['screenshot_error']='capture_unavailable'
            result.update(exit_code=code,output=output)
            return result
        except (OSError, RuntimeError, ValueError, zipfile.BadZipFile, subprocess.TimeoutExpired) as error:
            code=78
            result.update(exit_code=code, output='Runtime setup/adapter failure: '+type(error).__name__+': '+str(error)[:6000])
            return result
        finally:
            self.cancel()
            clean=self._restore()
            # Mutate the returned dictionary only after the finally cleanup.
            result['cleanup_verified']=clean
            write_private(root/'receipt.json',{'exit_code':code,**result})
