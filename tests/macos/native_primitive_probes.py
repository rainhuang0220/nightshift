"""Owned, finite macOS primitive probes; observations are not release approval.

No persistent LaunchAgent is installed: one UUID label is bootstrapped from a
private temporary plist, then booted out. Images and mount points are temporary
and owned by this probe. No target is selected by PID/name/cwd scanning.
"""
from __future__ import annotations
import argparse
import json
import os
import platform
import plistlib
import select
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from nightshift.containment import build_read_policy, write_profile
from task_lifetime_gate import leader_code, wait_for, writer_code, size


def command(argv, timeout=15):
    return subprocess.run(argv, capture_output=True, timeout=timeout)


def launchd_probe():
    root=Path(tempfile.mkdtemp(prefix='nightshift-launchd-proof-'))
    work=root/'workspace'; work.mkdir()
    label='org.nightshift.lifetime-proof.'+uuid.uuid4().hex
    domain=f'gui/{os.getuid()}'
    service=domain+'/'+label
    profile=write_profile(root/'profile.sb',writable=[work],network=False,
                          read_policy=build_read_policy(runtime_roots=[work]))
    plist=root/'ephemeral.plist'
    plist.write_bytes(plistlib.dumps({'Label':label,'ProgramArguments':['/usr/bin/sandbox-exec','-f',str(profile),sys.executable,'-c',leader_code(detached=True)],
        'WorkingDirectory':str(work),'RunAtLoad':True,'KeepAlive':False,'AbandonProcessGroup':False,
        'StandardOutPath':str(root/'stdout'),'StandardErrorPath':str(root/'stderr')}))
    attempted=False
    try:
        attempted=True
        response=command(['/bin/launchctl','bootstrap',domain,str(plist)])
        if response.returncode:
            return {'conclusive':False,'bootstrap_exit':response.returncode,
                    'error':response.stderr.decode(errors='replace').strip()}
        wait_for(lambda:size(work/'writes')>0)
        # Human-readable print is a local experiment observation only. It is
        # not selected as a programmatic production-supervision contract.
        def exited():
            out=command(['/bin/launchctl','print',service]).stdout.decode(errors='replace')
            return 'state = not running' in out and 'last exit code = 0' in out
        wait_for(exited)
        before=size(work/'writes')
        wait_for(lambda:size(work/'writes')>before,timeout=.5)
        return {'conclusive':True,'leader_exit':0,'abandon_process_group':False,
                'writer':json.loads((work/'identity').read_text()),
                'writes_after_launchd_exit':[before,size(work/'writes')],
                'setsid_contained':False,'persistent_service_installed':False}
    finally:
        if attempted:
            status=command(['/bin/launchctl','print',service])
            if status.returncode==0:
                out=command(['/bin/launchctl','bootout',service])
                if out.returncode:
                    raise RuntimeError(f'owned temporary job bootout failed; retained {root}')
                status=command(['/bin/launchctl','print',service])
            missing=(status.returncode==113 and
                     b'Could not find service' in status.stderr and label.encode() in status.stderr)
            if not missing:
                raise RuntimeError(f'job removal cannot be confirmed; retained {root}')
        if (work/'identity').exists():
            remaining=json.loads((work/'identity').read_text())['expires_at']+.2-time.monotonic()
            if remaining>0:time.sleep(remaining)
        shutil.rmtree(root)


def kqueue_probe():
    with tempfile.TemporaryDirectory(prefix='nightshift-kqueue-proof-') as raw:
        root=Path(raw)
        code='import sys\nsys.stdin.read(1)\n'+leader_code(detached=True)
        child=subprocess.Popen([sys.executable,'-c',code],cwd=root,stdin=subprocess.PIPE,
                               stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        queue=select.kqueue()
        try:
            event=select.kevent(child.pid,filter=select.KQ_FILTER_PROC,
                                flags=select.KQ_EV_ADD|select.KQ_EV_ENABLE|select.KQ_EV_CLEAR,
                                fflags=select.KQ_NOTE_FORK|select.KQ_NOTE_EXIT|select.KQ_NOTE_TRACK)
            try:
                queue.control([event],0,0)
                registration='accepted'
            except OSError as error:
                registration=f'errno:{error.errno}'
            child.stdin.write(b'x'); child.stdin.flush()
            wait_for(lambda:size(root/'writes')>0)
            child.wait(timeout=5)
            events=queue.control(None,16,.5)
            identity=json.loads((root/'identity').read_text())
            return {'registration':registration,'registered_pid':child.pid,
                    'events':[{'ident':e.ident,'fflags':e.fflags,'data':e.data} for e in events],
                    'detached_writer':identity,
                    'child_automatically_registered':any(e.ident==identity['pid'] for e in events),
                    'api_kind':'notification, not termination or write revocation'}
        finally:
            queue.close()
            if child.poll() is None: child.kill()
            child.wait(timeout=5)
            child.stdin.close()
            time.sleep(4.5)


def volume_probe():
    root=Path(tempfile.mkdtemp(prefix='nightshift-volume-proof-'))
    image=root/'owned.dmg'; mount=root/'mount'; mount.mkdir()
    device=None; child=None; attachment_attempted=False
    try:
        created=command(['/usr/bin/hdiutil','create','-size','64m','-fs','APFS','-volname','NightshiftLifetimeProof','-type','UDIF',str(image)],timeout=30)
        if created.returncode:
            return {'conclusive':False,'create_exit':created.returncode,
                    'error':created.stderr.decode(errors='replace').strip()}
        attachment_attempted=True
        attach=command(['/usr/bin/hdiutil','attach','-nobrowse','-mountpoint',str(mount),'-plist',str(image)],timeout=30)
        if attach.returncode:
            return {'conclusive':False,'attach_exit':attach.returncode,
                    'error':attach.stderr.decode(errors='replace').strip()}
        (root/'attachment.plist').write_bytes(attach.stdout)
        entities=plistlib.loads(attach.stdout)['system-entities']
        # Attach returns /private/var while tempfile may use /var. Retain
        # this owned attachment's device before any later assertion.
        device=entities[0]['dev-entry']
        matching=[item for item in entities if item.get('mount-point') and Path(item['mount-point']).resolve()==mount.resolve()]
        assert len(matching)==1, 'cannot prove owned mount identity'
        device=matching[0]['dev-entry']
        # Paths are not a revocable capability: after forced unmount this
        # same allowed subpath may resolve to the host's mount directory.
        profile=write_profile(root/'profile.sb',writable=[mount],network=False,
                              read_policy=build_read_policy(runtime_roots=[mount]))
        code=("import os,time,sys,select,json\nfrom pathlib import Path\n"
              f"root=Path({str(mount)!r})\n"
              "fd=os.open(root/'held',os.O_WRONLY|os.O_CREAT,0o600)\n"
              "os.write(fd,b'initial'); (root/'ready').touch()\n"
              "if not select.select([sys.stdin],[],[],4)[0]: sys.exit(2)\n"
              "sys.stdin.read(1); result={}\n"
              "try: result['held_fd_bytes_written_after_detach']=os.write(fd,b'after')\n"
              "except OSError as e: result['held_fd_write_errno']=e.errno\n"
              "try:\n"
              " root.mkdir(exist_ok=True); (root/'reopened').write_text('write-after-detach')\n"
              " result['path_reopened_on_host_after_detach']=True\n"
              "except OSError as e: result['path_reopen_errno']=e.errno\n"
              "print(json.dumps(result),flush=True)\n")
        child=subprocess.Popen(['/usr/bin/sandbox-exec','-f',str(profile),sys.executable,'-c',code],cwd='/',
                               stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        wait_for(lambda:(mount/'ready').exists())
        detached=command(['/usr/bin/hdiutil','detach','-force',device],timeout=20)
        if detached.returncode:
            return {'conclusive':False,'force_detach_exit':detached.returncode,
                    'error':detached.stderr.decode(errors='replace').strip()}
        device=None
        # Avoid mistaking the still-mounted file for a post-detach write.
        child.stdin.write(b'x'); child.stdin.flush()
        stdout,stderr=child.communicate(timeout=5)
        result=json.loads(stdout)
        return {'conclusive':True,'forced_detach_exit':0,**result,
                'child_exit':child.returncode,
                'whole_process_lifetime_bound':False,'requires_new_authority_layout':True}
    finally:
        if child is not None:
            child.wait(timeout=6)  # Finite payload, no descendant PID signal.
        # A failed or timed-out attach can still have created an attachment.
        # Reconcile by the exact private image identity before deleting files.
        if attachment_attempted:
            info=command(['/usr/bin/hdiutil','info','-plist'])
            if info.returncode:
                raise RuntimeError(f'image ownership cannot be checked; retained {root}')
            images=[item for item in plistlib.loads(info.stdout).get('images',[])
                    if Path(item.get('image-path','')).resolve()==image.resolve()]
            if len(images)>1:
                raise RuntimeError(f'image ownership ambiguous; retained {root}')
            if images:
                entities=images[0].get('system-entities',[])
                if not entities or not entities[0].get('dev-entry'):
                    raise RuntimeError(f'owned attachment unidentified; retained {root}')
                cleanup=command(['/usr/bin/hdiutil','detach',entities[0]['dev-entry']],timeout=20)
                if cleanup.returncode:
                    raise RuntimeError(f'owned image still mounted; retained {root}')
                after=command(['/usr/bin/hdiutil','info','-plist'])
                if after.returncode or any(Path(item.get('image-path','')).resolve()==image.resolve()
                                          for item in plistlib.loads(after.stdout).get('images',[])):
                    raise RuntimeError(f'image removal unconfirmed; retained {root}')
        shutil.rmtree(root)


if __name__=='__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('--evidence',type=Path,required=True)
    parser.add_argument('--probe',choices=['launchd','kqueue','volume','all'],default='all')
    args=parser.parse_args()
    if sys.platform!='darwin':raise SystemExit('real macOS is required')
    probes={'launchd':launchd_probe,'kqueue':kqueue_probe,'volume':volume_probe}
    result={'platform':platform.platform(),'probe_results':{}}
    for name,probe in probes.items():
        if args.probe in ('all',name):result['probe_results'][name]=probe()
    args.evidence.write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result,indent=2))
