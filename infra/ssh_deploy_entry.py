"""Forced SSH command: accept deployment parameters, never client shell code."""
import argparse
import json
import os
import re
import subprocess
import sys


MAX_REQUEST_BYTES = 16384


def run(repository, stream, original_command):
    if original_command == 'ers-deploy-check':
        print('Dedicated deploy key ready.')
        return 0
    if original_command != 'ers-deploy':
        print('This key only permits deployment.', file=sys.stderr)
        return 1
    try:
        raw = stream.read(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES:
            raise ValueError
        request = json.loads(raw)
        if not isinstance(request, dict) or set(request) != {'repository', 'expected_sha', 'github_token'}:
            raise ValueError
        if request['repository'] != repository:
            raise ValueError
        sha, token = request['expected_sha'], request['github_token']
        if not isinstance(sha, str) or not re.fullmatch(r'[a-f0-9]{40}', sha):
            raise ValueError
        if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,4096}', token):
            raise ValueError
    except (ValueError, TypeError, UnicodeError):
        print('Invalid deployment request.', file=sys.stderr)
        return 1

    environment = {
        'PATH': '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
        'HOME': '/home/ubuntu', 'USER': 'ubuntu', 'LOGNAME': 'ubuntu', 'LANG': 'C.UTF-8',
        'GH_TOKEN': token, 'REPOSITORY': repository, 'EXPECTED_SHA': sha,
    }
    return subprocess.run(
        ['/bin/bash', '/opt/emergency/infra/remote_deploy.sh'],
        cwd='/opt/emergency', env=environment, stdin=subprocess.DEVNULL,
    ).returncode


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--repository', required=True)
    options = parser.parse_args()
    if not re.fullmatch(r'[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+', options.repository):
        return 1
    return run(options.repository, sys.stdin.buffer, os.environ.get('SSH_ORIGINAL_COMMAND', ''))


if __name__ == '__main__':
    raise SystemExit(main())
