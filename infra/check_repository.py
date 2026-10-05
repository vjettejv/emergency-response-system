"""Fail closed on forbidden tracked files or recognizable secrets, without values."""
import argparse
import ast
from pathlib import PurePosixPath
import re
import subprocess
import sys

FORBIDDEN_DIRS = {'docs', 'reports', 'report', 'documents', 'tai-lieu', 'bao-cao',
                 'artifacts', 'output', 'node_modules', 'venv', '.venv', '__pycache__',
                 '.aws', '.ssh', '.certificates', 'staticfiles', 'media', 'build', 'dist', '.cache'}
FORBIDDEN_SUFFIXES = {'.md', '.doc', '.docx', '.pdf', '.ppt', '.pptx', '.pem', '.key', '.p12',
                      '.pfx', '.pyc', '.log', '.bak', '.tmp', '.backup', '.bundle', '.zip', '.sqlite3', '.dump'}
EXAMPLES = {'.env.example', '.env.production.example'}
PATTERNS = {
    'private-key': re.compile(rb'-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----'),
    'aws-access-key': re.compile(rb'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b'),
    'github-token': re.compile(rb'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})\b'),
    'credential-url': re.compile(rb'\b(?:postgres(?:ql)?|https?|redis)://[^\s/:]+:[^\s@/]+@'),
}
SECRET_ASSIGNMENT = re.compile(
    rb'''(?i)\b(?:password|passwd|secret_key|api_key|access_token|aws_secret_access_key|postgres_password|django_secret_key)\b["']?\s*[:=]\s*["']([^"'\r\n]+)["']''')

def git(*arguments):
    return subprocess.check_output(['git', *arguments], stderr=subprocess.DEVNULL)

def forbidden(name):
    path = PurePosixPath(name)
    return (bool(set(path.parts) & FORBIDDEN_DIRS) or path.suffix.lower() in FORBIDDEN_SUFFIXES
            or (path.name.startswith('.env') and name not in EXAMPLES)
            or path.name in {'credentials', '.deploy.lock', '.cd.lock'}
            or (name.startswith('infra/') and (path.name.startswith(('s3-cors', 'ec2-role-policy'))
                or path.name == 's3-lifecycle.example.json')))

def inspect(name, content):
    issues = []
    if forbidden(name):
        issues.append((name, 'forbidden-file'))
    for rule, expression in PATTERNS.items():
        if expression.search(content):
            issues.append((name, rule))
    if not name.startswith('backend/tests/'):
        validation_errors = []
        if name.endswith('.py'):
            for node in ast.walk(ast.parse(content, filename=name)):
                if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
                    function = node.exc.func
                    if isinstance(function, ast.Attribute) and function.attr == 'ValidationError':
                        validation_errors.append((node.lineno, node.end_lineno))
        for match in SECRET_ASSIGNMENT.finditer(content):
            line = content[:match.start()].count(b'\n') + 1
            if any(start <= line <= end for start, end in validation_errors):
                continue  # Serializer field-error messages are not authentication values.
            if not match.group(1).startswith((b'replace-with-', b'${', b'{{')):
                issues.append((name, 'literal-secret-assignment'))
    if name in EXAMPLES:
        for line in content.decode('utf-8').splitlines():
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, value = line.split('=', 1)
            if key in {'DJANGO_SECRET_KEY', 'POSTGRES_PASSWORD', 'AWS_SECRET_ACCESS_KEY', 'AWS_ACCESS_KEY_ID', 'AWS_SESSION_TOKEN', 'DEMO_PASSWORD'}:
                if value and not value.startswith('replace-with-'):
                    issues.append((name, 'non-placeholder-environment-secret'))
    return issues

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--history', action='store_true')
    options = parser.parse_args()
    issues = []
    paths = git('ls-files', '-z').decode().split('\0')
    for name in filter(None, paths):
        issues.extend(inspect(name, git('show', ':' + name)))
    if options.history:
        try:
            commits = git('rev-list', '--all').decode().splitlines()
        except subprocess.CalledProcessError:
            commits = []  # Unborn repository has no history.
        seen = set()
        for commit in commits:
            for entry in git('ls-tree', '-r', '-z', commit).split(b'\0'):
                if not entry:
                    continue
                metadata, name_bytes = entry.split(b'\t', 1)
                _, kind, oid = metadata.split()
                name = name_bytes.decode()
                if forbidden(name):
                    issues.append((name, 'forbidden-file-in-history'))
                if kind == b'blob' and oid not in seen:
                    issues.extend(inspect(name, git('cat-file', 'blob', oid.decode())))
                    seen.add(oid)
    if issues:
        for name, rule in sorted(set(issues)):
            print('REJECTED ' + name + ': ' + rule, file=sys.stderr)
        return 1
    print('Tracked/staged source and requested history scan passed; no secret values printed.')
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
