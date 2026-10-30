#!/usr/bin/env python3
"""Install explicit plugin releases from this collection's immutable Git tags."""
from __future__ import annotations

import argparse
import difflib
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import shlex
import stat
import subprocess
import sys
import tomllib

COLLECTION = Path(__file__).resolve().parent
GIT_LOCAL_ENV = {
    'GIT_ALTERNATE_OBJECT_DIRECTORIES', 'GIT_CONFIG', 'GIT_CONFIG_PARAMETERS', 'GIT_CONFIG_COUNT',
    'GIT_OBJECT_DIRECTORY', 'GIT_DIR', 'GIT_WORK_TREE', 'GIT_IMPLICIT_WORK_TREE', 'GIT_GRAFT_FILE',
    'GIT_INDEX_FILE', 'GIT_NO_REPLACE_OBJECTS', 'GIT_REPLACE_REF_BASE', 'GIT_PREFIX',
    'GIT_SHALLOW_FILE', 'GIT_COMMON_DIR',
}
DIGEST_ALGORITHM = 'bmad-loop-tree-v1'


def run(args, cwd=None):
    env = {k: v for k, v in os.environ.items() if k not in GIT_LOCAL_ENV
           and not k.startswith(('GIT_CONFIG_KEY_', 'GIT_CONFIG_VALUE_'))}
    result = subprocess.run(args, cwd=cwd, env=env, capture_output=True, timeout=120)
    if result.returncode:
        raise ValueError(result.stderr.decode(errors='replace').strip() or f'{args[0]} failed')
    return result.stdout


def git(*args):
    return run(['git', '-C', str(COLLECTION), *args])


def digest(files):
    rows = [[p, hashlib.sha256(data).hexdigest(), executable]
            for p, (data, executable) in sorted(files.items(), key=lambda item: os.fsencode(item[0]))]
    return hashlib.sha256(json.dumps({'v': 1, 'files': rows}, separators=(',', ':')).encode()).hexdigest()


def safe(path, directory=False):
    if path.is_symlink():
        raise ValueError(f'Refusing symlink: {path}')
    if path.exists() and not (path.is_dir() if directory else path.is_file()):
        raise ValueError(f'Unexpected file type: {path}')


def tree(root):
    safe(root, True)
    if not root.is_dir():
        raise ValueError(f'Missing managed folder: {root}')
    files = {}
    def scan_error(error):
        raise error
    for base, dirs, names in os.walk(root, followlinks=False, onerror=scan_error):
        dirs[:] = [name for name in dirs if name != "__pycache__"]
        names = [name for name in names if name != "__pycache__"]
        for name in dirs:
            safe(Path(base) / name, True)
        for name in names:
            path = Path(base) / name
            safe(path)
            if path.stat().st_size > 16 << 20 or len(files) >= 2048:
                raise ValueError('Plugin tree exceeds limits')
            files[path.relative_to(root).as_posix()] = (path.read_bytes(), bool(path.stat().st_mode & 0o111))
    if sum(len(data) for data, _ in files.values()) > 64 << 20:
        raise ValueError('Plugin tree exceeds limits')
    return files


def release(name, version):
    if not re.fullmatch(r'[a-z][a-z0-9-]*', name) or not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise ValueError('Unsafe plugin name or version; use a name and explicit X.Y.Z version')
    tag = f'{name}-v{version}'
    commit = git('rev-parse', '--verify', f'refs/tags/{tag}^{{commit}}').decode().strip()
    entries = git('ls-tree', '-rz', commit, '--', f'plugins/{name}/').split(b'\0')
    files = {}
    total = 0
    prefix = f'plugins/{name}/'
    for entry in filter(None, entries):
        header, raw_path = entry.split(b'\t', 1)
        mode, kind, oid = header.decode().split()
        path = raw_path.decode('utf-8')
        rel = path.removeprefix(prefix)
        if (not path.startswith(prefix) or not rel or any(p in ('', '.', '..') for p in rel.split('/'))
                or '__pycache__' in rel.split('/') or '\\' in rel or not PurePosixPath(rel).is_relative_to('.')
                or mode not in ('100644', '100755') or kind != 'blob'):
            raise ValueError(f'Unsafe released path or type: {path}')
        size = int(git('cat-file', '-s', oid))
        total += size
        if size > 16 << 20 or total > 64 << 20 or len(files) >= 2048:
            raise ValueError('Released tree exceeds limits')
        files[rel] = (git('cat-file', 'blob', oid), mode == '100755')
    if 'plugin.toml' not in files:
        raise ValueError('Release has no plugin manifest')
    manifest = tomllib.loads(files['plugin.toml'][0].decode())
    if (manifest['plugin']['name'] != name or manifest['plugin']['version'] != version
            or manifest['plugin']['api_version'] != 1):
        raise ValueError('Release manifest identity/version/API mismatch')
    metadata_path = f'compatibility/{name}.toml'
    entry = git('ls-tree', commit, '--', metadata_path).decode()
    if not entry.startswith('100644 blob '):
        raise ValueError('Missing regular compatibility metadata')
    compatibility = tomllib.loads(git('show', f'{commit}:{metadata_path}').decode())
    if compatibility.get('api_version') != manifest['plugin']['api_version']:
        raise ValueError('Compatibility API mismatch')
    return files, dict(name=name, version=version, tag=tag, commit=commit,
                       digest=digest(files), digest_algorithm=DIGEST_ALGORITHM), compatibility


def paths(project):
    safe(project, True)
    project = project.resolve(strict=True)
    loop = project / '.bmad-loop'
    plugins = loop / 'plugins'
    for path in (loop, plugins):
        safe(path, True)
    lock = loop / 'plugins.lock.json'
    safe(lock)
    transaction = loop / '.plugin-manager-transaction'
    safe(transaction, True)
    if transaction.exists():
        raise ValueError(f'Unfinished transaction: {transaction}. Preserve an audit copy and recover before retrying.')
    return project, loop, plugins, lock, transaction


def read_lock(path):
    if not path.exists():
        return {'schema': 1, 'plugins': {}}
    value = json.loads(path.read_text())
    if value.get('schema') != 1 or not isinstance(value.get('plugins'), dict):
        raise ValueError('Unsupported lock format')
    return value


def check_record(name, record, files):
    if (record.get('name') != name or record.get('tag') != f'{name}-v{record.get("version")}'
            or not re.fullmatch(r'[0-9a-f]{40}', record.get('commit', ''))
            or record.get('digest_algorithm') != DIGEST_ALGORITHM
            or record.get('digest') != digest(files)):
        raise ValueError('Managed identity or bytes differ from lock; preserve local changes')
    manifest = tomllib.loads(files['plugin.toml'][0].decode())['plugin']
    if manifest['name'] != name or manifest['version'] != record['version']:
        raise ValueError('Installed manifest identity differs from lock')


def compatible(project, metadata):
    cached = project / '.bmad-loop/cache/bin/bmad-loop'
    executable = str(cached) if cached.is_file() else shutil.which('bmad-loop')
    if not executable:
        raise ValueError('No cached bmad-loop or bmad-loop on PATH')
    version = run([executable, '--version']).decode().strip()
    if version not in [f'bmad-loop {v}' for v in metadata.get('bmad_loop_versions', [])]:
        raise ValueError(f'Unsupported loop: {version}; tested versions: {metadata.get("bmad_loop_versions")}')
    return executable


def write_lock(path, value):
    data = json.dumps(value, indent=2, sort_keys=True).encode() + b'\n'
    with path.open('xb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def show_diff(old, new):
    for path in sorted(old.keys() | new.keys()):
        if old.get(path) == new.get(path):
            continue
        print(f'{"added" if path not in old else "removed" if path not in new else "changed"}: {path}')
        a, b = old.get(path, (b'', False)), new.get(path, (b'', False))
        if a[1] != b[1]:
            print(f'  executable: {a[1]} -> {b[1]}')
        try:
            print(''.join(difflib.unified_diff(a[0].decode().splitlines(True), b[0].decode().splitlines(True),
                                             fromfile=f'old/{path}', tofile=f'new/{path}')), end='')
        except UnicodeDecodeError:
            print('  binary content changed')


def change(action, name, version, project):
    files, record, metadata = release(name, version)
    project, loop, plugins, lock, tx = paths(project)
    loop_executable = compatible(project, metadata)
    loop.mkdir(exist_ok=True)
    guard = loop / '.plugin-manager.lock'
    safe(guard)
    with guard.open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Check again under the manager lock; another process may have completed an update.
        project, loop, plugins, lock, tx = paths(project)
        state = read_lock(lock)
        dest = plugins / name
        safe(dest, True)
        existing = state['plugins'].get(name)
        had_runtime = dest.exists()
        replay = bool(existing) and not had_runtime and action == 'install'
        if replay:
            if existing != record:
                raise ValueError('Missing runtime can only be installed from the exact locked release identity')
            old = {}
        elif existing:
            old = tree(dest)
            check_record(name, existing, old)
            if existing.get('version') == version and existing != record:
                raise ValueError('Release tag identity differs from installed lock; tags must be immutable')
            if existing == record:
                print(f'{name} {version}: already installed; bytes and release identity match')
                return
            if action == 'install':
                raise ValueError('A different version is installed; use update for upgrade or rollback')
        else:
            if dest.exists():
                raise ValueError('Unmanaged existing plugin folder; preserve it before installation')
            if action == 'update':
                raise ValueError('No managed plugin; use install')
            old = {}
        show_diff(old, files)
        plugins.mkdir(exist_ok=True)
        tx.mkdir(mode=0o700)
        # The old runtime and exact lock bytes remain in this journal after interruption.
        lock_replaced = False
        try:
            (tx / 'journal.json').write_text(json.dumps({'plugin': name, 'previously_installed': had_runtime,
                                                       'had_lock': lock.exists(), 'release': record}, indent=2))
            if lock.exists():
                shutil.copyfile(lock, tx / 'old-lock.pending')
                os.replace(tx / 'old-lock.pending', tx / 'old-lock.json')
            stage = tx / 'new'
            stage.mkdir()
            for rel, (data, executable) in files.items():
                path = stage / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                path.chmod(0o755 if executable else 0o644)
            staged = tree(stage)
            if staged != files or digest(staged) != record['digest']:
                raise ValueError('Staged runtime differs from release; possible filesystem path collision or byte loss')
            state['plugins'][name] = record
            if replay:
                shutil.copyfile(lock, tx / 'new-lock.json')
            else:
                write_lock(tx / 'new-lock.json', state)
            if had_runtime:
                os.replace(dest, tx / 'old')
            os.replace(stage, dest)
            os.replace(tx / 'new-lock.json', lock)
            lock_replaced = True
        except Exception:
            # A synchronous failure restores both parts; keep the journal for audit.
            if (tx / 'old').exists():
                if dest.exists():
                    os.replace(dest, tx / 'failed-new')
                os.replace(tx / 'old', dest)
            elif not had_runtime and dest.exists() and not (tx / 'new').exists():
                os.replace(dest, tx / 'failed-new')
            if lock_replaced:
                if (tx / 'old-lock.json').exists():
                    shutil.copyfile(tx / 'old-lock.json', lock)
                else:
                    lock.unlink()
            raise
        shutil.rmtree(tx)
        command = 'accept' if had_runtime else 'enable'
        print(f'{name} {version}: {record["commit"]} {record["digest"]}')
        grant_command = [loop_executable, 'plugins', command, name, '--project', str(project)]
        print(f'After review: {shlex.join(grant_command)}')
        print('Installation does not grant code or start a loop. Keep private configuration outside the plugin folder.')


def status(project):
    _, _, plugins, lock, _ = paths(project)
    state = read_lock(lock)
    names = set(state['plugins'])
    if plugins.exists():
        names.update(path.name for path in plugins.iterdir())
    if not names:
        print('No installed plugins')
    for name in sorted(names):
        if not re.fullmatch(r'[a-z][a-z0-9-]*', name):
            print(f'{name!r}: unsafe plugin identifier')
            continue
        record = state['plugins'].get(name)
        if not record:
            print(f'{name}: unmanaged')
            continue
        try:
            check_record(name, record, tree(plugins / name))
            print(f'{name} {record["version"]}: clean ({record["tag"]}, {record["commit"]}, {record["digest"]})')
        except (ValueError, OSError, KeyError, TypeError) as exc:
            print(f'{name}: unsafe or modified: {exc}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    for action in ('install', 'update'):
        p = sub.add_parser(action)
        p.add_argument('plugin')
        p.add_argument('--version', required=True)
        p.add_argument('--project', type=Path, required=True)
    p = sub.add_parser('status')
    p.add_argument('--project', type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.action == 'status':
            status(args.project)
        else:
            change(args.action, args.plugin, args.version, args.project)
    except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        parser.exit(1, f'Refused: {exc}\n')


if __name__ == '__main__':
    main()
