"""Three reviewed deliveries, not Pi turns/checks, transfer implementation."""
import json
import os
import sys
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path

from runtime_helpers import RUNTIME, Repo, base_env, cleanup_repos, default_config, make_pi_trap, run_cli
sys.path.insert(0, str(RUNTIME))
import pi_board
import pi_task
from pi_takeover import review_policy


class TakeoverTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='pi-takeover-')
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(cleanup_repos)
        self.repo = Repo(Path(self.temp.name), config=default_config())
        self.wt = self.repo.worktree('worker')
        self.head = pi_task.git(self.wt, 'rev-parse', 'HEAD')
        self.card = pi_board._new_card('task', None, 'task', 'outcome', None, None,
                                      self.repo.root, self.repo.state_dir, str(self.wt),
                                      'offline', None, 1)
        self.board = {'schemaVersion': 1, 'revision': 1, 'createdAt': 1, 'updatedAt': 1,
                      'cards': {'task': self.card}}
        self.file = self.repo.state_dir / 'board.json'
        self.file.parent.mkdir(parents=True, exist_ok=True)

    def publish(self, number, phase='P1', kind='review_required', contract='a' * 64):
        self.card['pi'] = {'round': number, 'state': 'completed'}
        self.card['phase'] = {'phaseId': phase, 'contractHash': contract,
                              'candidate': self.head, 'status': 'review_ready'}
        event = pi_board.add_event(self.card, kind, number, str(number), 'delivery',
                                  {'head': self.head}, {}, 'review', number * 10)
        event.update(phaseId=phase, contractHash=contract)
        self.file.write_text(json.dumps(self.board))
        return event

    def reject(self, number, **kwargs):
        event = self.publish(number, **kwargs)
        out = pi_board.decide(self.repo.root, 'task', event['id'], 'changes_requested',
                              now=number * 10 + 1)
        self.board = json.loads(self.file.read_text())
        self.card = self.board['cards']['task']
        return event, out

    def test_third_distinct_review_latches_and_duplicate_cannot_double_count(self):
        first, out = self.reject(1)
        self.assertFalse(out['reviewPolicy']['takeoverRequired'])
        pi_board.decide(self.repo.root, 'task', first['id'], 'changes_requested')
        self.reject(2, contract='b' * 64)  # design revision does not erase failures
        third, out = self.reject(3)
        self.assertTrue(out['reviewPolicy']['takeoverRequired'])
        self.assertEqual(out['reviewPolicy']['failedDeliveries'], 3)
        self.assertEqual(len([e for e in self.card['events']
                              if e['kind'] == 'codex_takeover_required']), 1)
        pi_board.decide(self.repo.root, 'task', third['id'], 'changes_requested')
        self.assertEqual(review_policy(json.loads(self.file.read_text())['cards']['task'])
                         ['failedDeliveries'], 3)

    def test_two_events_on_one_round_count_once_and_external_blocker_does_not(self):
        self.reject(1)
        event = pi_board.add_event(self.card, 'phase_blocked', 1, 'another-event', 'gap',
                                  {'head': self.head}, {}, 'review', 12)
        event.update(phaseId='P1', contractHash='a' * 64)
        self.file.write_text(json.dumps(self.board))
        pi_board.decide(self.repo.root, 'task', event['id'], 'reject', now=13)
        self.board = json.loads(self.file.read_text());self.card = self.board['cards']['task']
        self.assertEqual(review_policy(self.card)['failedDeliveries'], 1)
        event = self.publish(2, kind='phase_blocked')
        with self.assertRaisesRegex(ValueError, 'require --note'):
            pi_board.decide(self.repo.root, 'task', event['id'], 'changes_requested',
                            failure_kind='external')
        out = pi_board.decide(self.repo.root, 'task', event['id'], 'changes_requested',
                              failure_kind='external', note='Needs user observation; unlock on receipt', now=21)
        self.assertEqual(out['reviewPolicy']['failedDeliveries'], 1)
        with self.assertRaisesRegex(ValueError, 'classification is immutable'):
            pi_board.decide(self.repo.root, 'task', event['id'], 'changes_requested',
                            failure_kind='quality')

    def test_accepted_outcome_and_different_phase_do_not_inherit_a_failed_streak(self):
        self.reject(1);self.reject(2)
        # Test the read-only policy fold; real acceptance authorization is owned
        # by decide and covered by its existing exact-candidate tests.
        self.card['handled']['accepted'] = {'decision': 'accepted', 'eventKind': 'review_required',
                                            'phaseId': 'P1', 'round': 3, 'at': 31}
        self.assertEqual(review_policy(self.card)['failedDeliveries'], 0)
        self.card['phase']['phaseId'] = 'P2'
        self.assertEqual(review_policy(self.card)['failedDeliveries'], 0)

    def test_legacy_phase_decisions_count_and_latch_survives_pruning_and_resume(self):
        self.card['phase'] = {'phaseId': 'P1'}
        self.card['handled'] = {str(n): {'decision': 'changes_requested', 'phaseId': 'P1',
                                        'round': n, 'at': n} for n in range(1, 4)}
        self.assertTrue(review_policy(self.card)['takeoverRequired'])
        self.card['codex']['takeover'] = {'required': True}
        self.card['handled'] = {};self.card['events'] = []
        self.file.write_text(json.dumps(self.board))
        pi_board.set_paused(self.repo.root, 'task', False)
        card = json.loads(self.file.read_text())['cards']['task']
        self.assertTrue(review_policy(card)['takeoverRequired'])

    def test_continue_refuses_before_mutating_rounds_or_launching_a_worker(self):
        env = base_env(CODEX_PI_HANDOFF_ROOT=str(Path(self.temp.name) / 'handoff'))
        self.repo.start('task', self.wt, env=env)
        self.repo.wait_terminal('task', env=env)
        for n in range(1, 4):self.reject(n)
        frozen = json.loads((self.repo.task_dir('task') / 'task.json').read_text())
        blocked, reason = pi_task.board_pause_active(frozen)
        self.assertTrue(blocked);self.assertIn('Codex takeover required', reason)
        trap, marker = make_pi_trap(Path(self.temp.name) / 'traps')
        env['PI_BIN'] = str(trap)
        before = sorted((self.repo.task_dir('task') / 'rounds').iterdir())
        result = run_cli('continue', '--repo', self.repo.root, '--task', 'task',
                         '--prompt', 'must not run', env=env, expect=2)
        self.assertIn('Codex takeover required', result.stderr)
        self.assertEqual(before, sorted((self.repo.task_dir('task') / 'rounds').iterdir()))
        self.assertFalse(marker.exists())
        task_dir = self.repo.task_dir('task')
        auto = pi_task.evaluate_auto_continue(frozen, task_dir, 1,
            {'contract': {'phaseId': 'P1'}, 'contractSha256': 'a' * 64},
            {'status': 'missing', 'items': [{'id': 'check', 'status': 'missing'}],
             'budget': {'remainingSeconds': 600}})
        self.assertEqual(auto['action'], 'escalate')
        self.assertIn('Codex takeover required', auto['detail'])
        with self.assertRaisesRegex(ValueError, 'Codex takeover required'):
            pi_task.run_worker(SimpleNamespace(task_dir=task_dir, round=1, timeout_seconds=30))

    def test_three_real_completed_rounds_cannot_launch_a_fourth(self):
        env = base_env(CODEX_PI_HANDOFF_ROOT=str(Path(self.temp.name) / 'handoff-real'))
        self.repo.start('real-task', self.wt, env=env)
        self.repo.wait_terminal('real-task', env=env)
        board_cli = RUNTIME / 'pi_board.py'
        def command(*args):
            result = subprocess.run([sys.executable, str(board_cli), *map(str, args)],
                                    env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)
        command('register', '--repo', self.repo.root, '--task', 'real-task', '--transport', 'offline')
        for number in range(1, 4):
            command('refresh', '--repo', self.repo.root, '--task', 'real-task')
            card = json.loads(self.file.read_text())['cards']['real-task']
            event = [e for e in card['events'] if e['kind'] == 'review_required'
                     and not e['handled'] and e['round'] == number][-1]
            result = command('decide', '--repo', self.repo.root, '--task', 'real-task',
                             '--event-id', event['id'], '--decision', 'changes_requested')
            self.assertEqual(result['reviewPolicy']['failedDeliveries'], number)
            if number < 3:
                run_cli('continue', '--repo', self.repo.root, '--task', 'real-task',
                        '--prompt', 'repair the complete outcome', env=env)
                self.repo.wait_terminal('real-task', env=env)
        trap, marker = make_pi_trap(Path(self.temp.name) / 'fourth-round-trap')
        env['PI_BIN'] = str(trap)
        result = run_cli('continue', '--repo', self.repo.root, '--task', 'real-task',
                         '--prompt', 'fourth attempt', env=env, expect=2)
        self.assertIn('Codex takeover required', result.stderr)
        self.assertFalse(marker.exists())
        self.assertFalse((self.repo.task_dir('real-task') / 'rounds' / '4').exists())

    def test_non_delivery_faults_and_progress_never_count(self):
        self.card['phase'] = {'phaseId': 'P1'}
        self.card['handled'] = {str(n): {'decision': 'changes_requested', 'phaseId': 'P1',
                                       'eventKind': 'ownership_unknown', 'round': n, 'at': n}
                                for n in range(1, 8)}
        self.assertEqual(review_policy(self.card)['failedDeliveries'], 0)


if __name__ == '__main__':
    unittest.main()
