"""Git subprocesses with no inherited configuration, hooks or credential env."""
import os
import subprocess


def git(root,*args):
    env={k:os.environ[k] for k in ('PATH','SYSTEMROOT','TMPDIR') if k in os.environ}
    env.update(GIT_CONFIG_NOSYSTEM='1',GIT_CONFIG_GLOBAL='/dev/null',GIT_TERMINAL_PROMPT='0',
               GIT_TEMPLATE_DIR='/nonexistent')
    return subprocess.check_output(['git','-c','core.fsmonitor=false','-c','core.hooksPath=/dev/null','-c','init.templateDir=',
        '-c','protocol.file.allow=always','-c','credential.helper=','-C',str(root),*args],
        env=env,stderr=subprocess.DEVNULL)
