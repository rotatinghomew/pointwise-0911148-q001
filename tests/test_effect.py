import copy
import json
import tempfile
import unittest
from collections import Counter
from pathlib import Path

from app.contracts import instant
from app.domain import OPERATIONS, exposure_coverage, seed_dataset
from app.effect import DEFAULT_WINDOWS, schedule_effect
from app.repository import digest
from app.service import Service

AS_OF = '2026-09-30T23:59:59Z'


def run(dataset=None, options=None, as_of=AS_OF):
    return schedule_effect(dataset if dataset is not None else seed_dataset(),
                           options, as_of)


class ScheduleEffectTests(unittest.TestCase):
    def setUp(self):
        self.data = seed_dataset()
        self.result = run(self.data, {'carryover_periods': [0, 1, 2, 3]})

    def test_analysis_unit_is_randomization_set_not_order(self):
        result = self.result
        self.assertEqual(result['randomization_unit'], 'randomization_set')
        self.assertEqual(result['design']['randomization_sets'], 12)
        self.assertEqual(len(result['design']['candidate_sequences']), 24)
        effect = result['windows'][0]['assignment_effect']
        self.assertEqual(effect['cluster_by'], 'randomization_set')
        self.assertEqual(effect['clusters'], 12)
        self.assertEqual(effect['stratified_by'], 'block_offset')
        self.assertIn('排班', ' '.join(result['method_notes']))

    def test_default_windows_are_reported(self):
        default = run(self.data)
        self.assertEqual(default['analysis_options']['carryover_periods'], DEFAULT_WINDOWS)
        self.assertEqual([window['carryover_periods'] for window in default['windows']],
                         DEFAULT_WINDOWS)

    def test_carried_flow_shrinks_usable_evidence(self):
        counts = [window['usable_period_count'] for window in self.result['windows']]
        self.assertEqual(counts, sorted(counts, reverse=True))
        self.assertGreater(counts[0], counts[-1])
        for window in self.result['windows'][1:]:
            self.assertTrue(any(reason.startswith('carryover_after_arm_change')
                                for reason in window['excluded_counts']))

    def test_arm_change_periods_are_excluded_under_carryover(self):
        window = self.result['windows'][1]
        excluded = {(region, period)
                    for reason, periods in window['excluded_periods'].items()
                    if reason == 'carryover_after_arm_change'
                    for region, period in periods}
        self.assertIn(('r0', 4), excluded)
        self.assertIn(('r1', 4), excluded)
        usable = {tuple(period) for period in window['usable_periods']}
        self.assertNotIn(('r0', 4), usable)

    def test_closure_missing_and_zero_are_kept_distinct(self):
        evidence = self.result['evidence']
        self.assertEqual(evidence['closures'], [['r0', 14], ['r0', 15]])
        self.assertGreater(evidence['closure_exposures'], 0)
        self.assertEqual(evidence['coverage_missing'], [['r1', 10], ['r1', 11]])
        self.assertEqual(evidence['confirmed_zero_exposure'], [['r2', 17]])
        self.assertNotIn(['r2', 17], evidence['coverage_missing'])
        no_carryover = {tuple(period) for period in self.result['windows'][0]['usable_periods']}
        self.assertIn(('r2', 17), no_carryover)
        self.assertNotIn(('r1', 10), no_carryover)
        self.assertNotIn(('r0', 14), no_carryover)

    def test_closure_periods_are_not_used_as_evidence(self):
        usable = {tuple(period) for period in self.result['windows'][0]['usable_periods']}
        self.assertNotIn(('r0', 14), usable)
        self.assertNotIn(('r0', 15), usable)
        self.assertNotIn(('r1', 10), usable)

    def test_cross_period_orders_stay_with_their_exposure(self):
        cross = self.result['evidence']['cross_period_orders']
        self.assertEqual(cross['total_orders'], len(self.data['orders']))
        self.assertEqual(cross['orders'], len(self.data['orders']))
        self.assertEqual(cross['within_period_orders'], 0)
        self.assertEqual(cross['share'], 1.0)
        self.assertEqual(cross['attribution'], '按 exposure_id 归入首次曝光时段，跨时段或跨片区不改变归属')
        self.assertEqual(self.result['evidence']['cross_region_orders']['orders'], 0)

    def test_observation_cutoff_marks_unobserved_periods(self):
        early = run(self.data, {'carryover_periods': [0]}, as_of='2026-09-01T02:00:00Z')
        after = {tuple(period) for period in early['evidence']['periods_after_observation_cutoff']}
        self.assertIn(('r0', 5), after)
        self.assertNotIn(('r0', 3), after)
        usable = {tuple(period) for period in early['windows'][0]['usable_periods']}
        self.assertFalse(after & usable)

    def test_unreported_outcomes_are_excluded_not_zero_filled(self):
        dataset = copy.deepcopy(self.data)
        dataset['orders'] = []
        result = run(dataset, {'carryover_periods': [0]})
        window = result['windows'][0]
        self.assertGreater(window['excluded_counts']['outcome_unreported'], 0)
        self.assertEqual(window['usable_periods'], [['r2', 17]])
        self.assertEqual(window['evaluable_exposures'], {'0': 0, '1': 0})
        self.assertEqual(window['assignment_effect']['status'], 'not_estimable')
        self.assertEqual(result['robustness']['signal'], 'not_estimable')

    def test_confirmed_zero_period_is_not_treated_as_missing(self):
        dataset = copy.deepcopy(self.data)
        result = run(dataset, {'carryover_periods': [0]})
        self.assertIn(['r2', 17], result['evidence']['confirmed_zero_exposure'])
        usable = {tuple(period) for period in result['windows'][0]['usable_periods']}
        self.assertIn(('r2', 17), usable)

    def test_design_mismatch_removes_the_whole_randomization_set(self):
        dataset = copy.deepcopy(self.data)
        dataset['schedule'][0]['assigned'] = 1 - dataset['schedule'][0]['assigned']
        result = run(dataset, {'carryover_periods': [0]})
        invalid = result['design']['checks']['invalid_sets']
        self.assertEqual(len(invalid), 1)
        self.assertEqual(invalid[0]['randomization_set'], 'r0-b0')
        self.assertIn('assigned_mismatch', invalid[0]['reasons'])
        self.assertEqual(result['windows'][0]['excluded_counts']['design_mismatch'], 8)
        self.assertEqual(result['design']['checks']['sets_valid'], 11)

    def test_probability_audit_detects_denormalized_set(self):
        dataset = copy.deepcopy(self.data)
        for row in dataset['designs']:
            if row['randomization_set'] == 'r1-b0':
                row['probability'] = 0.4
        result = run(dataset, {'carryover_periods': [0]})
        invalid = {item['randomization_set']: item['reasons']
                   for item in result['design']['checks']['invalid_sets']}
        self.assertIn('probabilities_not_normalized', invalid['r1-b0'])

    def test_result_retains_schedule_cutoff_and_choices(self):
        self.assertEqual(self.result['request']['as_of'], AS_OF)
        self.assertEqual(self.result['evidence']['observation_cutoff'], AS_OF)
        self.assertEqual(self.result['request']['options'], {'carryover_periods': [0, 1, 2, 3]})
        ordered = sorted(self.data['schedule'],
                         key=lambda row: (row['region'], row['period'], row['block_offset']))
        self.assertEqual(self.result['design']['schedule_digest'], digest(ordered))
        self.assertEqual(self.result['design']['schedule_used'], ordered)
        self.assertEqual(len(self.result['design']['candidate_sequences']), len(self.data['designs']))

    def test_assignments_effect_is_separate_from_claim_descriptives(self):
        window = self.result['windows'][0]
        effect = window['assignment_effect']
        self.assertEqual(effect['status'], 'ok')
        self.assertNotEqual(effect['estimate'], window['descriptive_arm_rate_difference'])
        claim = window['descriptive_claim_contrast']
        self.assertTrue(claim['descriptive_only'])
        self.assertEqual(claim['claims_in_off_arm'], 0)
        self.assertIn('not_claimed', claim['within_assigned_on'])
        self.assertNotIn('assignment_effect', claim)

    def test_robustness_summarises_every_window(self):
        robustness = self.result['robustness']
        self.assertEqual(len(robustness['windows']), 4)
        self.assertIn(robustness['signal'],
                      {'consistent_positive', 'consistent_negative', 'inconclusive',
                       'window_sensitive', 'unstable_direction', 'not_estimable'})
        for item in robustness['windows']:
            if item['estimable']:
                self.assertLessEqual(item['ci_low'], item['estimate'])
                self.assertLessEqual(item['estimate'], item['ci_high'])

    def test_deterministic_and_input_not_mutated(self):
        before = copy.deepcopy(self.data)
        first = run(self.data, {'carryover_periods': [0, 2]})
        second = run(self.data, {'carryover_periods': [0, 2]})
        self.assertEqual(first, second)
        self.assertEqual(self.data, before)

    def test_empty_records_do_not_crash(self):
        dataset = {table: [] for table in self.data}
        result = run(dataset)
        self.assertEqual(result['design']['randomization_sets'], 0)
        self.assertEqual(result['robustness']['signal'], 'not_estimable')
        self.assertEqual(result['evidence']['periods_in_schedule'], 0)

    def test_option_validation(self):
        for options in ({'carryover_periods': []}, {'carryover_periods': [1, 1]},
                        {'carryover_periods': [-1]}, {'carryover_periods': ['1']},
                        {'carryover_periods': [1.5]}, {'unknown': 1}):
            with self.assertRaises(ValueError):
                run(self.data, options)
        with self.assertRaises(ValueError):
            run(self.data, [])

    def test_revision_keeps_latest_record(self):
        dataset = copy.deepcopy(self.data)
        revised = copy.deepcopy(dataset['orders'][0])
        revised['revision'] = 1
        revised['converted'] = 1 - revised['converted']
        revised['received_at'] = '2026-09-10T00:00:00Z'
        dataset['orders'].append(revised)
        result = run(dataset, {'carryover_periods': [0]})
        self.assertEqual(result['evidence']['cross_period_orders']['total_orders'],
                         len(dataset['orders']) - 1)

    def test_coverage_operation_still_works(self):
        coverage = exposure_coverage(self.data)
        self.assertEqual(coverage['periods'], 96)
        self.assertIn(('r1', 10), coverage['missing_periods'])
        self.assertIn('schedule_effect', OPERATIONS)
        self.assertIn('exposure_coverage', OPERATIONS)


class ScheduleEffectWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.source = Path(self.temporary.name) / 'source.json'
        self.source.write_text(Path('data/source.json').read_text())
        self.service = Service(self.source, Path(self.temporary.name) / 'state.sqlite3')
        self.request = {'request_id': 'effect-check', 'operation': 'schedule_effect',
                        'as_of': AS_OF, 'options': {'carryover_periods': [0, 1, 2, 3]}}

    def test_history_replays_the_same_judgement(self):
        result = self.service.run(self.request)
        self.assertEqual(result, self.service.repository.analysis('effect-check'))
        self.source.write_text('{}')
        self.assertEqual(result, self.service.run(self.request))
        self.assertEqual(result['request']['options'], self.request['options'])
        self.assertEqual(result['request']['as_of'], AS_OF)
        self.assertTrue(result['design']['schedule_used'])

    def test_different_options_with_same_id_conflict(self):
        from app.repository import Conflict
        self.service.run(self.request)
        with self.assertRaises(Conflict):
            self.service.run({**self.request, 'options': {'carryover_periods': [0]}})

    def test_catalog_lists_both_operations(self):
        self.assertEqual(self.service.describe()['operations'],
                         ['exposure_coverage', 'schedule_effect'])

    def test_saved_options_survive_without_source(self):
        result = self.service.run(self.request)
        stored = self.service.repository.dataset(result['dataset_digest'])
        self.assertIn('schedule', stored)
        self.assertEqual(result['analysis_options']['carryover_periods'], [0, 1, 2, 3])


if __name__ == '__main__':
    unittest.main()
