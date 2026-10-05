"""Repository mutation, exact-commit and migration-safe recovery tests."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ssm_remote


class RemoteDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / '.env.production').write_text("APP_TAG='" + 'a' * 40 + "'\nPUBLIC_HOST=example.invalid\n")
        self.deployment = ssm_remote.Deployment(self.root, 'b' * 40, 'vjettejv/emergency-response-system', Mock())
        self.calls = []
        def command(arguments, step, environment=None, pass_fds=()):
            self.calls.append(arguments)
            if arguments[:3] == ['git', 'status', '--porcelain']:
                return ''
            if arguments[:4] == ['git', 'remote', 'get-url', 'origin']:
                return 'https://github.com/vjettejv/emergency-response-system.git'
            if arguments[:3] == ['git', 'rev-parse', 'HEAD']:
                return 'a' * 40
            return ''
        self.deployment.command = Mock(side_effect=command)
        self.deployment.snapshot = Mock(return_value=[['incidents', '0001_initial']])
        self.deployment.run_compose_deploy = Mock()

    def execute(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return self.deployment.execute()

    def test_checks_out_requested_sha_even_when_main_can_advance(self):
        self.assertEqual(self.execute(), 0)
        self.assertIn(['git', 'fetch', '--no-tags', 'origin', 'main'], self.calls)
        self.assertIn(['git', 'switch', '--detach', '--no-overwrite-ignore', 'b' * 40], self.calls)
        self.assertFalse(any('reset' in call for call in self.calls))
        self.assertIn("APP_TAG='" + 'b' * 40 + "'", (self.root / '.env.production').read_text())
        state = json.loads((self.root / 'artifacts/deploy/state.json').read_text())
        self.assertEqual(state['status'], 'success')
        self.assertEqual(state['previous_sha'], 'a' * 40)

    def test_dirty_worktree_fails_before_checkout_without_reset(self):
        self.deployment.clean = Mock(side_effect=ssm_remote.DeploymentFailure('working_tree_dirty_no_files_reset'))
        with self.assertRaises(ssm_remote.DeploymentFailure):
            self.execute()
        self.assertEqual(self.calls, [])
        self.deployment.run_compose_deploy.assert_not_called()

    def test_build_failure_restores_previous_source_and_restarts_previous_version(self):
        self.deployment.run_compose_deploy.side_effect = [ssm_remote.DeploymentFailure('compose_deployment_failed'), None]
        self.assertEqual(self.execute(), 1)
        self.assertIn(['git', 'switch', '--detach', '--no-overwrite-ignore', 'a' * 40], self.calls)
        self.deployment.run_compose_deploy.assert_called_with('a' * 40, rollback=True)
        self.assertEqual(self.deployment.rollback, 'success')

    def test_new_or_partially_started_migrations_prevent_automatic_rollback(self):
        def failure(tag, rollback=False):
            self.deployment.phase_file.write_text('migrate')
            self.deployment.plan_file.write_text(json.dumps([['incidents', '0002_new', False]]))
            raise ssm_remote.DeploymentFailure('compose_deployment_failed')
        self.deployment.run_compose_deploy.side_effect = failure
        self.assertEqual(self.execute(), 1)
        self.assertEqual(self.deployment.migration_state, 'uncertain')
        self.assertNotIn(['git', 'switch', '--detach', '--no-overwrite-ignore', 'a' * 40], self.calls)
        self.assertEqual(self.deployment.run_compose_deploy.call_count, 1)
        self.assertEqual(self.deployment.rollback, 'skipped_schema_requires_manual_recovery')

    def test_container_failure_with_unchanged_schema_can_recover(self):
        def failure_then_recovery(tag, rollback=False):
            if not rollback:
                self.deployment.phase_file.write_text('services')
                self.deployment.plan_file.write_text('[]')
                raise ssm_remote.DeploymentFailure('compose_deployment_failed')
        self.deployment.run_compose_deploy.side_effect = failure_then_recovery
        self.assertEqual(self.execute(), 1)
        self.assertEqual(self.deployment.rollback, 'success')

    def test_records_applied_new_migrations_and_requests_manual_recovery(self):
        def failure(tag, rollback=False):
            self.deployment.phase_file.write_text('health')
            self.deployment.plan_file.write_text('[]')
            raise ssm_remote.DeploymentFailure('compose_deployment_failed')
        self.deployment.run_compose_deploy.side_effect = failure
        self.deployment.snapshot.side_effect = [[['incidents', '0001_initial']], [['incidents', '0001_initial'], ['incidents', '0002_new']]]
        self.assertEqual(self.execute(), 1)
        self.assertEqual(self.deployment.migration_state, 'changed')
        self.assertEqual(self.deployment.run_compose_deploy.call_count, 1)

    def test_changed_configured_image_is_not_silently_overwritten(self):
        (self.root / '.env.production').write_text("APP_TAG='other-image'\n")
        with self.assertRaises(ssm_remote.DeploymentFailure):
            self.execute()
        self.deployment.run_compose_deploy.assert_not_called()

    def test_recovery_failure_never_marks_deployment_successful(self):
        self.deployment.run_compose_deploy.side_effect = ssm_remote.DeploymentFailure('compose_deployment_failed')
        self.assertEqual(self.execute(), 1)
        self.assertEqual(self.deployment.rollback, 'failed_requires_manual_recovery')

    def test_unavailable_database_after_migrations_prevents_blind_rollback(self):
        def failure(tag, rollback=False):
            self.deployment.phase_file.write_text('services')
            self.deployment.plan_file.write_text('[]')
            raise ssm_remote.DeploymentFailure('compose_deployment_failed')
        self.deployment.run_compose_deploy.side_effect = failure
        self.deployment.snapshot.side_effect = [[['incidents', '0001_initial']], ssm_remote.DeploymentFailure('migration_snapshot')]
        self.assertEqual(self.execute(), 1)
        self.assertEqual(self.deployment.migration_state, 'unknown')
        self.assertEqual(self.deployment.run_compose_deploy.call_count, 1)

    def test_fetch_auth_failure_does_not_modify_source_or_create_tokens(self):
        original = self.deployment.command.side_effect
        def failed_fetch(arguments, step, environment=None, pass_fds=()):
            if step == 'git_fetch_auth_unavailable_no_PAT_created':
                raise ssm_remote.DeploymentFailure(step)
            return original(arguments, step, environment, pass_fds)
        self.deployment.command.side_effect = failed_fetch
        self.assertEqual(self.execute(), 1)
        self.assertFalse(self.deployment.changed)
        self.deployment.run_compose_deploy.assert_not_called()


if __name__ == '__main__':
    unittest.main()
