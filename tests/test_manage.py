"""Release-manager checks use local disposable repositories, never GitHub."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('manage', ROOT / 'manage.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class ManagerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name).resolve()
        self.collection = root / 'collection'
        self.collection.mkdir()
        self.project = root / 'project'
        self.project.mkdir()
        self.patcher = patch.object(m, 'COLLECTION', self.collection)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        m.git('init', '-b', 'main')
        m.git('config', 'user.name', 'Disposable Test')
        m.git('config', 'user.email', 'test@example.invalid')
        shutil.copytree(ROOT / 'plugins', self.collection / 'plugins')
        shutil.copytree(ROOT / 'compatibility', self.collection / 'compatibility')
        self.tag('0.1.0')
        self.loop = self.project / '.bmad-loop/cache/bin/bmad-loop'
        self.loop.parent.mkdir(parents=True)
        self.loop.write_text('#!/bin/sh\nprintf "bmad-loop 0.13.2\\n"\n')
        self.loop.chmod(0o755)
        self.dest = self.project / '.bmad-loop/plugins/delivery'
        self.lock = self.project / '.bmad-loop/plugins.lock.json'
        self.tx = self.project / '.bmad-loop/.plugin-manager-transaction'

    def tag(self, version):
        manifest = self.collection / 'plugins/delivery/plugin.toml'
        text = manifest.read_text()
        import re
        manifest.write_text(re.sub(r'version = "\d+\.\d+\.\d+"', f'version = "{version}"', text))
        m.git('add', '.')
        m.git('commit', '-m', f'release {version}')
        m.git('tag', f'delivery-v{version}')

    def change(self, version='0.1.0', action='install'):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            m.change(action, 'delivery', version, self.project)
        return output.getvalue()

    def snapshot(self):
        return m.tree(self.dest), self.lock.read_bytes()

    def test_install_only_tag_bytes_lock_digest_and_idempotence(self):
        runtime = self.collection / 'plugins/delivery/delivery.py'
        runtime.write_text('uncommitted must never install')
        output = self.change()
        self.assertIn('plugins enable delivery', output)
        self.assertNotEqual((self.dest / 'delivery.py').read_bytes(), runtime.read_bytes())
        record = json.loads(self.lock.read_text())['plugins']['delivery']
        self.assertEqual(record['commit'], m.git('rev-parse', 'delivery-v0.1.0').decode().strip())
        self.assertEqual(record['digest'], m.digest(m.tree(self.dest)))
        before = self.snapshot()
        self.assertIn('already installed', self.change())
        self.assertEqual(self.snapshot(), before)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            m.status(self.project)
        self.assertIn('clean', output.getvalue())
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.tx.exists())

    def test_real_plugins_install_and_update_independently(self):
        m.git('tag', 'codex-notify-v1.0.0')
        self.change()
        with contextlib.redirect_stdout(io.StringIO()):
            m.change('install', 'codex-notify', '1.0.0', self.project)
        notify = self.dest.parent / 'codex-notify'
        before_tree = m.tree(notify)
        before_record = json.loads(self.lock.read_text())['plugins']['codex-notify']
        binding = self.project / '.bmad-loop/runs/fixture/codex-notify/state.json'
        binding.parent.mkdir(parents=True)
        binding.write_text('{"binding":"fixture-only"}')
        self.tag('0.1.1')
        self.change('0.1.1', 'update')
        self.assertEqual(m.tree(notify), before_tree)
        self.assertEqual(json.loads(self.lock.read_text())['plugins']['codex-notify'], before_record)
        self.assertEqual(binding.read_text(), '{"binding":"fixture-only"}')
        with contextlib.redirect_stdout(io.StringIO()) as output:
            m.status(self.project)
        self.assertIn('codex-notify 1.0.0: clean', output.getvalue())
        self.assertIn('delivery 0.1.1: clean', output.getvalue())
        delivery_tree = m.tree(self.dest)
        delivery_record = json.loads(self.lock.read_text())['plugins']['delivery']
        manifest = self.collection / 'plugins/codex-notify/plugin.toml'
        manifest.write_text(manifest.read_text().replace('version = "1.0.0"', 'version = "1.0.1"'))
        m.git('add', '.')
        m.git('commit', '-m', 'notify fixture release')
        m.git('tag', 'codex-notify-v1.0.1')
        with contextlib.redirect_stdout(io.StringIO()):
            m.change('update', 'codex-notify', '1.0.1', self.project)
        self.assertEqual(m.tree(self.dest), delivery_tree)
        self.assertEqual(json.loads(self.lock.read_text())['plugins']['delivery'], delivery_record)
        self.assertEqual(binding.read_text(), '{"binding":"fixture-only"}')
        self.assertEqual(json.loads(self.lock.read_text())['plugins']['codex-notify']['version'], '1.0.1')

    def test_update_and_rollback_show_diff_preserve_config_other_plugins(self):
        self.change()
        private = self.project / 'private.json'
        private.write_text('private')
        other = self.dest.parent / 'other'
        other.mkdir()
        (other / 'plugin.toml').write_text('unrelated')
        state = json.loads(self.lock.read_text())
        state['plugins']['other'] = {'preserve': True}
        self.lock.write_text(json.dumps(state))
        self.tag('0.1.1')
        output = self.change('0.1.1', 'update')
        self.assertIn('changed: plugin.toml', output)
        self.assertIn('plugins accept delivery', output)
        self.assertIn('-version = "0.1.0"', output)
        self.assertIn('+version = "0.1.1"', output)
        self.assertEqual(private.read_text(), 'private')
        self.assertEqual((other / 'plugin.toml').read_text(), 'unrelated')
        self.assertEqual(json.loads(self.lock.read_text())['plugins']['other'], {'preserve': True})
        self.change('0.1.0', 'update')
        self.assertEqual(json.loads(self.lock.read_text())['plugins']['delivery']['version'], '0.1.0')

    def test_edits_missing_files_and_manifest_edits_refuse_without_changing_lock(self):
        self.change()
        self.tag('0.1.1')
        target = self.dest / 'delivery.py'
        original = target.read_bytes()
        for operation in ('edit', 'missing', 'executable', 'extra'):
            with self.subTest(operation=operation):
                target.write_bytes(original)
                target.chmod(0o644)
                extra = self.dest / 'extra'
                if extra.exists():
                    extra.unlink()
                if operation == 'edit':
                    target.write_text('local edit')
                elif operation == 'missing':
                    target.unlink()
                elif operation == 'executable':
                    target.chmod(0o755)
                else:
                    extra.write_text('private')
                before = self.snapshot()
                with self.assertRaises(ValueError):
                    self.change('0.1.1', 'update')
                self.assertEqual(self.snapshot(), before)

    def test_unmanaged_missing_folder_symlinks_and_unsafe_identifiers(self):
        self.dest.mkdir(parents=True)
        with self.assertRaisesRegex(ValueError, 'Unmanaged'):
            self.change()
        self.dest.rmdir()
        self.change()
        shutil.rmtree(self.dest)
        before = self.lock.read_bytes()
        with self.assertRaisesRegex(ValueError, 'Missing'):
            self.change(action='update')
        self.assertEqual(self.lock.read_bytes(), before)
        self.dest.symlink_to(self.collection / 'plugins/delivery', target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            self.change(action='update')
        for name, version in (('../delivery', '0.1.0'), ('delivery', '../0.1.0'), ('delivery', 'latest')):
            with self.assertRaisesRegex(ValueError, 'Unsafe'):
                m.release(name, version)

    def test_reject_unknown_identity_compatibility_and_source_symlinks_before_install(self):
        with self.assertRaises(ValueError):
            self.change('9.9.9')
        m.git('tag', 'delivery-v0.2.0')
        with self.assertRaisesRegex(ValueError, 'mismatch'):
            self.change('0.2.0')
        self.loop.write_text('#!/bin/sh\necho "bmad-loop 0.99.0"\n')
        with self.assertRaisesRegex(ValueError, 'Unsupported loop'):
            self.change()
        self.assertFalse(self.dest.exists())
        self.assertFalse(self.lock.exists())
        (self.collection / 'plugins/delivery/link').symlink_to('delivery.py')
        self.tag('0.1.1')
        with self.assertRaisesRegex(ValueError, 'Unsafe released path'):
            m.release('delivery', '0.1.1')

    def test_failure_restores_old_runtime_and_lock_and_blocks_retry(self):
        self.change()
        before = self.snapshot()
        self.tag('0.1.1')
        original_replace = os.replace
        def fail(source, dest):
            if Path(source) == self.tx / 'new-lock.json':
                raise OSError('simulated lock failure')
            return original_replace(source, dest)
        with patch.object(m.os, 'replace', side_effect=fail), self.assertRaises(OSError):
            self.change('0.1.1', 'update')
        self.assertEqual(self.snapshot(), before)
        self.assertTrue((self.tx / 'journal.json').exists())
        with self.assertRaisesRegex(ValueError, 'Unfinished transaction'):
            self.change('0.1.1', 'update')

    def test_install_replays_exact_tracked_lock_without_rewriting_it(self):
        self.change()
        state = json.loads(self.lock.read_text())
        state['plugins']['other'] = {'preserve': True}
        self.lock.write_text(json.dumps(state, separators=(',', ':')))
        before = self.snapshot()
        shutil.rmtree(self.dest)
        with self.assertRaisesRegex(ValueError, 'Missing'):
            self.change(action='update')
        self.tag('0.1.1')
        with self.assertRaisesRegex(ValueError, 'exact locked release'):
            self.change('0.1.1')
        self.assertFalse(self.dest.exists())
        self.assertEqual(self.lock.read_bytes(), before[1])
        self.assertIn('plugins enable delivery', self.change())
        self.assertEqual(self.snapshot(), before)

    def test_moved_same_version_tag_refused_for_install_and_update(self):
        self.change()
        before = self.snapshot()
        (self.collection / 'release-note').write_text('different commit')
        m.git('add', '.')
        m.git('commit', '-m', 'different release identity')
        m.git('tag', '-f', 'delivery-v0.1.0')
        for action in ('install', 'update'):
            with self.assertRaisesRegex(ValueError, 'tag identity differs'):
                self.change(action=action)
        self.assertEqual(self.snapshot(), before)

    def test_fresh_install_backup_failure_preserves_unrelated_lock(self):
        self.lock.write_text(json.dumps({'schema': 1, 'plugins': {'other': {'preserve': True}}}))
        before = self.lock.read_bytes()
        def fail(source, dest):
            Path(dest).write_bytes(b'partial')
            raise OSError('simulated backup failure')
        with patch.object(m.shutil, 'copyfile', side_effect=fail), self.assertRaises(OSError):
            self.change()
        self.assertEqual(self.lock.read_bytes(), before)
        self.assertFalse(self.dest.exists())

    def test_partial_backup_failure_preserves_original_lock(self):
        self.change()
        before = self.snapshot()
        self.tag('0.1.1')
        original_copy = shutil.copyfile
        def partial_copy(source, dest):
            if Path(dest) == self.tx / 'old-lock.pending':
                Path(dest).write_bytes(b'partial')
                raise OSError('simulated backup failure')
            return original_copy(source, dest)
        with patch.object(m.shutil, 'copyfile', side_effect=partial_copy), self.assertRaises(OSError):
            self.change('0.1.1', 'update')
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.tx / 'old-lock.json').exists())

    def test_bytecode_cache_does_not_change_runtime_digest(self):
        self.change()
        before = self.snapshot()
        cache = self.dest / '__pycache__'
        cache.mkdir()
        (cache / 'delivery.cpython-test.pyc').write_bytes(b'cache')
        self.assertEqual(self.snapshot(), before)
        self.assertIn('already installed', self.change())

    def test_interruption_keeps_backup_and_journal(self):
        self.change()
        before = self.snapshot()
        self.tag('0.1.1')
        original_replace = os.replace
        def interrupt(source, dest):
            if Path(source) == self.tx / 'new':
                raise KeyboardInterrupt()
            return original_replace(source, dest)
        with patch.object(m.os, 'replace', side_effect=interrupt), self.assertRaises(KeyboardInterrupt):
            self.change('0.1.1', 'update')
        self.assertEqual(m.tree(self.tx / 'old'), before[0])
        self.assertEqual((self.tx / 'old-lock.json').read_bytes(), before[1])
        with self.assertRaisesRegex(ValueError, 'Unfinished transaction'):
            m.status(self.project)

    def test_poisoned_git_environment_never_changes_callers_repo(self):
        caller = self.project / 'caller'
        caller.mkdir()
        m.run(['git', 'init', '-b', 'main'], caller)
        config = caller / '.git/config'
        before = config.read_bytes()
        poisoned = {'GIT_DIR': str(caller / '.git'), 'GIT_WORK_TREE': str(caller),
                    'GIT_COMMON_DIR': str(caller / '.git'), 'GIT_INDEX_FILE': str(caller / '.git/index'),
                    'GIT_PREFIX': 'poisoned/', 'GIT_CONFIG_COUNT': '1',
                    'GIT_CONFIG_KEY_0': 'user.name', 'GIT_CONFIG_VALUE_0': 'Poison'}
        with patch.dict(os.environ, poisoned):
            self.change()
            self.assertEqual(m.git('config', 'user.name').decode().strip(), 'Disposable Test')
        self.assertEqual(config.read_bytes(), before)
        self.assertFalse((caller / '.git/index').exists())

    def test_local_subtree_scan_failure_never_replaces_runtime_or_lock(self):
        self.change()
        self.tag('0.1.1')
        subtree = self.dest / 'local-private'
        subtree.mkdir()
        (subtree / 'keep').write_text('local contents')
        before = self.snapshot()
        original_scandir = os.scandir
        def fail(path):
            if Path(path) == subtree:
                raise PermissionError('simulated subtree scan failure')
            return original_scandir(path)
        with patch.object(m.os, 'scandir', side_effect=fail), patch.object(m.os, 'replace') as replace:
            with self.assertRaisesRegex(PermissionError, 'subtree scan failure'):
                self.change('0.1.1', 'update')
            replace.assert_not_called()
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.tx.exists())

    def test_staged_byte_loss_refuses_before_runtime_replacement(self):
        self.change()
        self.tag('0.1.1')
        before = self.snapshot()
        original_write = Path.write_bytes
        original_replace = os.replace
        replacements = []
        def lose_byte(path, data):
            if path == self.tx / 'new/delivery.py':
                data = data[:-1]
            return original_write(path, data)
        def track(source, dest):
            replacements.append((Path(source), Path(dest)))
            return original_replace(source, dest)
        with patch.object(Path, 'write_bytes', lose_byte), patch.object(m.os, 'replace', side_effect=track):
            with self.assertRaisesRegex(ValueError, 'Staged runtime differs'):
                self.change('0.1.1', 'update')
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(any(source == self.dest or dest == self.dest for source, dest in replacements))
        self.assertTrue((self.tx / 'journal.json').exists())

    def test_actual_case_colliding_git_paths_refuse_on_case_insensitive_filesystem(self):
        probe = self.project / 'CaseProbe'
        probe.write_text('probe')
        if not (self.project / 'caseprobe').exists():
            self.skipTest('filesystem is case-sensitive; staged byte-loss test is deterministic')
        self.change()
        self.tag('0.1.1')
        for name, source in (('A.py', 'delivery.py'), ('a.py', 'plugin.toml')):
            oid = m.git('rev-parse', f'HEAD:plugins/delivery/{source}').decode().strip()
            m.git('update-index', '--add', '--cacheinfo', f'100644,{oid},plugins/delivery/{name}')
        m.git('commit', '-m', 'case-colliding release paths')
        m.git('tag', '-f', 'delivery-v0.1.1')
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, 'Staged runtime differs'):
            self.change('0.1.1', 'update')
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.tx / 'old').exists())

    def test_grant_command_uses_cached_loop_without_path_loop_and_quotes_spaces(self):
        project = self.project.parent / 'project with spaces'
        self.project.rename(project)
        self.project = project
        self.loop = project / '.bmad-loop/cache/bin/bmad-loop'
        self.dest = project / '.bmad-loop/plugins/delivery'
        self.lock = project / '.bmad-loop/plugins.lock.json'
        self.tx = project / '.bmad-loop/.plugin-manager-transaction'
        empty_path = project / 'empty-path'
        empty_path.mkdir()
        for version, action, grant in (('0.1.0', 'install', 'enable'), ('0.1.1', 'update', 'accept')):
            if version == '0.1.1':
                self.tag(version)
            release = m.release('delivery', version)
            with patch.object(m, 'release', return_value=release), patch.dict(os.environ, PATH=str(empty_path)):
                self.assertIsNone(shutil.which('bmad-loop'))
                output = self.change(version, action)
            command = next(line.removeprefix('After review: ') for line in output.splitlines()
                           if line.startswith('After review: '))
            self.assertEqual(shlex.split(command), [str(self.loop), 'plugins', grant, 'delivery',
                                                   '--project', str(project)])

    def test_status_is_read_only_on_empty_project(self):
        shutil.rmtree(self.project / '.bmad-loop')
        with contextlib.redirect_stdout(io.StringIO()):
            m.status(self.project)
        self.assertFalse((self.project / '.bmad-loop').exists())


if __name__ == '__main__':
    unittest.main()
