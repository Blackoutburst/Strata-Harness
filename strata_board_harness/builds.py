"""Fixed JDK17 Android builds in a network-isolated Linux namespace.

No SSH/ADB/Board credentials or host home are mounted. New build-control changes
require an explicit operator hash approval; source-only edits retain the baseline
build plan. Runtime device validation is a separate adapter.
"""

import base64
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import threading
from .managed_engine import write_private, group_members
import time


CONTROL_FILES = ('gradlew','gradlew.bat','gradle.properties','settings.gradle.kts','settings.gradle',
    'build.gradle.kts','build.gradle','app/build.gradle.kts','app/build.gradle','gradle/libs.versions.toml',
    'gradle/wrapper/gradle-wrapper.jar','gradle/wrapper/gradle-wrapper.properties')


def namespace_command(binary, workspace, sdk, gradle_home, *, online=False, java_home=None):
    java_home=Path(java_home) if java_home else Path.home()/'.local/share/strata-board-harness/tools/jdk17'
    if not java_home.exists(): java_home=Path('/usr/lib/jvm/java-17-openjdk-amd64')
    android_user=Path(gradle_home).parent/'android-user'
    android_user.mkdir(parents=True,exist_ok=True,mode=0o700)
    args=[str(binary),'--unshare-all','--die-with-parent','--new-session','--clearenv']
    if online: args+=['--share-net']  # Trusted baseline prefetch only, never model-created tasks.
    args+=['--ro-bind','/usr','/usr','--symlink','usr/bin','/bin','--symlink','usr/lib','/lib',
        '--symlink','usr/lib64','/lib64','--symlink','usr/sbin','/sbin','--proc','/proc','--dev','/dev',
        '--tmpfs','/tmp','--dir','/etc','--dir','/home','--dir','/home/worker','--dir','/opt',
        '--bind',str(workspace),'/work','--ro-bind',str(sdk),'/opt/android-sdk',
        '--ro-bind',str(java_home),'/opt/jdk17',
        '--bind',str(gradle_home),'/home/worker/.gradle',
        '--bind',str(android_user),'/home/worker/.android']
    for path in ('/etc/java-17-openjdk','/etc/ssl/certs','/etc/alternatives','/etc/localtime',
                 '/etc/resolv.conf','/etc/hosts','/etc/nsswitch.conf'):
        if Path(path).exists(): args+=['--ro-bind',path,path]
    args+=['--chdir','/work','--setenv','HOME','/home/worker','--setenv','PATH','/usr/bin:/bin',
        '--setenv','JAVA_HOME','/opt/jdk17','--setenv','ANDROID_HOME','/opt/android-sdk',
        '--setenv','GRADLE_USER_HOME','/home/worker/.gradle','--setenv','LANG','C.UTF-8',
        '--setenv','TMPDIR','/tmp','--setenv','JAVA_TOOL_OPTIONS','-Duser.home=/home/worker','--']
    return args


class AndroidBuilds:
    def __init__(self, workspace, baseline, bwrap, sdk, gradle_home, receipts, runtime_adapter=None,
                 approvals=None, clock=time.time):
        self.workspace=Path(workspace).resolve(strict=True)
        self.baseline=baseline;self.bwrap=Path(bwrap).resolve(strict=True);self.sdk=Path(sdk).resolve(strict=True)
        self.gradle_home=Path(gradle_home).resolve();self.gradle_home.mkdir(parents=True,exist_ok=True)
        self.receipts=Path(receipts).resolve();self.receipts.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.runtime_adapter,self.approvals,self.clock=runtime_adapter,approvals or {},clock
        self.lock=threading.Lock();self.source_lock=threading.Lock();self.source_cache={}
        self.child=None;self.last={};self.failures={}
        head=self.git('rev-parse','HEAD').strip()
        admission_file=self.receipts/'source-admission.json'
        if admission_file.exists():
            saved=json.loads(admission_file.read_text())
            if saved!={'baseline':baseline,'workspace':str(self.workspace)}:
                raise ValueError('Source admission changed')
            self.git('merge-base','--is-ancestor',baseline,head)
        else:
            if head!=baseline: raise ValueError('First pilot source must start at the exact selected baseline')
            write_private(admission_file,{'baseline':baseline,'workspace':str(self.workspace)})

    def git(self,*args):
        return subprocess.check_output(['git','-c','core.hooksPath=/dev/null','-c','commit.gpgsign=false',
            '-c','user.name=Strata Harness','-c','user.email=strata-harness@localhost',
            '-C',str(self.workspace),*args],text=True,env={
                'PATH':'/usr/bin:/bin','HOME':str(self.receipts),'GIT_CONFIG_NOSYSTEM':'1','GIT_CONFIG_GLOBAL':'/dev/null'},timeout=15)

    def control_changes(self):
        changed=[]
        for name in CONTROL_FILES:
            current=self.workspace/name
            before=subprocess.run(['git','-C',str(self.workspace),'show',self.baseline+':'+name],capture_output=True,timeout=10)
            exists=current.exists()
            if exists and (not current.is_file() or current.is_symlink()): raise ValueError('Unsafe build control path')
            data=current.read_bytes() if exists else None
            if data!=(before.stdout if before.returncode==0 else None):
                digest=hashlib.sha256(data).hexdigest() if data is not None else 'deleted'
                if self.approvals.get(name)!=digest: changed.append({'path':name,'sha256':digest})
        # Additional Gradle/init/buildSrc/included-build files may execute code.
        for path in self.workspace.rglob('*'):
            relative=path.relative_to(self.workspace)
            if any(part in ('.git','.gradle','build') for part in relative.parts): continue
            if path.is_symlink(): raise ValueError('Source symlink requires operator review')
            if path.is_file() and (path.suffix in ('.gradle',) or path.name.endswith('.gradle.kts')
                                  or relative.parts[0]=='buildSrc') and str(relative) not in CONTROL_FILES:
                before=subprocess.run(['git','-C',str(self.workspace),'show',self.baseline+':'+str(relative)],capture_output=True,timeout=10)
                if before.returncode or path.read_bytes()!=before.stdout:
                    digest=hashlib.sha256(path.read_bytes()).hexdigest()
                    if self.approvals.get(str(relative))!=digest:changed.append({'path':str(relative),'sha256':digest})
        return changed

    def checkpoint(self,include_runtime=True):
        status=self.git('status','--porcelain=v1','-z')
        files=set(filter(None,self.git('ls-files','-z').split('\x00')))
        files.update(filter(None,self.git('ls-files','--others','--exclude-standard','-z').split('\x00')))
        digest=hashlib.sha256()
        with self.source_lock:
            for name in sorted(files):
                path=self.workspace/name
                if not path.exists():continue
                if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(self.workspace):
                    raise ValueError('Unsafe source path')
                info=path.stat();signature=(info.st_mtime_ns,info.st_ctime_ns,info.st_size,info.st_ino)
                cached=self.source_cache.get(name)
                if cached is None or cached[0]!=signature:
                    content=hashlib.sha256(path.read_bytes()).hexdigest()
                    if signature!=(lambda st:(st.st_mtime_ns,st.st_ctime_ns,st.st_size,st.st_ino))(path.stat()):
                        raise RuntimeError('Source changed during checkpoint')
                    self.source_cache[name]=(signature,content)
                digest.update(name.encode());digest.update(b'\x00');digest.update(self.source_cache[name][1].encode())
                digest.update(str(info.st_mode&0o111).encode())
        return {'head':self.git('rev-parse','HEAD').strip(),'clean':not status,'source_sha256':digest.hexdigest(),
                'owned_effects':self.runtime_adapter.checkpoint() if self.runtime_adapter and include_runtime else {'runtime_observed':False}}

    def clean(self):
        with self.lock:
            process_clean=self.child is None or (self.child.poll() is not None and not group_members(self.child.pid))
        return process_clean and (self.runtime_adapter.clean() if self.runtime_adapter else True)

    def cancel(self):
        with self.lock: child=self.child
        if child is not None and child.poll() is None:
            os.killpg(child.pid,signal.SIGTERM)
            try:child.wait(timeout=5)
            except subprocess.TimeoutExpired:os.killpg(child.pid,signal.SIGKILL);child.wait(timeout=5)
        if self.runtime_adapter:self.runtime_adapter.cancel()

    def status(self):
        with self.lock:return dict(self.last)

    def build(self,argument,cancelled):
        changes=self.control_changes()
        if changes:
            return {'exit_code':78,'output':'Build control changes need manager review: '+json.dumps(changes),
                    'cleanup_verified':True,'requires_manager_review':changes}
        before=self.checkpoint()
        with self.lock:self.last['build_started_at']=self.clock()
        # local.properties is not source acceptance evidence; keep the SDK
        # namespace path generated by the harness and exclude it from commits.
        (self.workspace/'local.properties').write_text('sdk.dir=/opt/android-sdk\n')
        argv=namespace_command(self.bwrap,self.workspace,self.sdk,self.gradle_home)+[
            '/bin/sh','./gradlew','--offline','--no-daemon','--console=plain',
            'assembleDebug','assembleDebugAndroidTest','testDebugUnitTest']
        result=self.run(argv,cancelled,900)
        result['source_checkpoint']=before
        after=self.checkpoint(include_runtime=False)
        if after['source_sha256']!=before['source_sha256']:
            result.update(exit_code=75,source_changed_during_build=True,after_checkpoint=after)
        with self.lock:
            if result['exit_code']==0:
                self.last['successful_build_at']=self.clock()
                self.last['successful_build_source_sha256']=before['source_sha256']
                self.last['artifact_sha256']={str(p.relative_to(self.workspace)):hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in self.workspace.glob('app/build/outputs/apk/**/*.apk')}
                self.last.pop('repeated_failure',None)
            else:
                digest=hashlib.sha256((before['source_sha256']+result['output'][-12000:]).encode()).hexdigest()
                self.failures[digest]=self.failures.get(digest,0)+1
                if self.failures[digest]>=2:self.last['repeated_failure']={'failure_sha256':digest,
                    'attempts':self.failures[digest],'exit_code':result['exit_code'],'evidence':'full gateway build receipt'}
        return result

    def run(self,argv,cancelled,cap):
        path=self.receipts/('command-'+str(time.time_ns())+'.log')
        with path.open('w') as output:
            path.chmod(0o600)
            env={'PATH':'/usr/bin:/bin','HOME':str(self.receipts),'LANG':'C.UTF-8'}
            child=subprocess.Popen(argv,cwd=self.workspace,env=env,stdout=output,stderr=subprocess.STDOUT,start_new_session=True)
            with self.lock:self.child=child
            deadline=time.monotonic()+cap;reason=None
            while child.poll() is None:
                if cancelled.wait(.1):reason='cancelled';break
                if time.monotonic()>=deadline:reason='timeout';break
            if reason:self.cancel()
            code=child.wait(timeout=10)
        return {'exit_code':124 if reason=='timeout' else (130 if reason=='cancelled' else code),
                'process_exit_code':code,'stop_reason':reason,'output':path.read_text(errors='replace'),
                'cleanup_verified':self.clean(),'command_receipt':str(path)}

    def commit(self,argument,cancelled):
        if cancelled.is_set():return {'exit_code':130,'output':'cancelled','cleanup_verified':True}
        try:
            text=argument.replace('_',' ')
            if not text.strip() or len(text)>500 or '\x00' in text:raise ValueError('Invalid commit message')
        except (ValueError,TypeError):return {'exit_code':64,'output':'Use a short commit message with underscores for spaces','cleanup_verified':True}
        try:
            self.git('add','--all')
            if not self.git('status','--porcelain').strip():
                # Not an uncertain effect: there is simply nothing new to record.
                return {'exit_code':1,'output':'Nothing to commit; HEAD is already '+self.git('rev-parse','HEAD').strip()
                        +'\n'+json.dumps(self.checkpoint()),'cleanup_verified':True}
            output=self.git('commit','-m',text)
        except subprocess.CalledProcessError as error:
            return {'exit_code':error.returncode or 1,'output':'git commit failed: '+str(error.output or '')[-4000:],
                    'cleanup_verified':True}
        return {'exit_code':0,'output':output+'\n'+json.dumps(self.checkpoint()),'cleanup_verified':True}

    def test(self,argument,cancelled):
        if not self.runtime_adapter:return {'exit_code':78,'output':'No verified serial-restricted runtime adapter','cleanup_verified':True}
        current=self.checkpoint(include_runtime=False)
        if self.last.get('successful_build_source_sha256')!=current['source_sha256']:
            return {'exit_code':78,'output':'Current source does not match a successful build checkpoint','cleanup_verified':True}
        for name,digest in self.last.get('artifact_sha256',{}).items():
            if hashlib.sha256((self.workspace/name).read_bytes()).hexdigest()!=digest:
                return {'exit_code':78,'output':'APK changed after the successful build','cleanup_verified':True}
        return self.runtime_adapter.test(self.workspace,cancelled,scenario=argument or 'normal')
