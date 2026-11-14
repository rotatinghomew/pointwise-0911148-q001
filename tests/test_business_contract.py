import tempfile
import unittest
from collections import Counter
from datetime import datetime
from pathlib import Path

from app.contracts import visible
from app.domain import OPERATIONS, seed_dataset
from app.repository import digest
from app.service import Service


class BusinessContractTests(unittest.TestCase):
    def setUp(self):
        self.data = seed_dataset()
        self.operation = next(iter(OPERATIONS))

    def run_check(self):
        return OPERATIONS[self.operation](self.data, {})

    def test_randomization_design_matches_observed_schedule(self):
        designs = {(row['randomization_set'], row['sequence']): row for row in self.data['designs']}
        totals = Counter()
        for row in self.data['designs']:
            totals[row['randomization_set']] += row['probability']
        self.assertTrue(all(value == 1 for value in totals.values()))
        for row in self.data['schedule']:
            self.assertEqual(row['assigned'], designs[row['randomization_set'], row['sequence']]['assignments'][row['block_offset']])

    def test_zero_and_missing_are_different_region_periods(self):
        result = self.run_check()
        self.assertEqual(result['periods'], 96)
        self.assertIn(('r2', 17), result['confirmed_zero_periods'])
        self.assertIn(('r1', 10), result['missing_periods'])
        self.assertNotIn(('r1', 10), result['confirmed_zero_periods'])


    def test_fixture_timestamps_do_not_arrive_before_events(self):
        from app.contracts import instant
        for table, records in self.data.items():
            if table in {'schedule', 'designs', 'closures'}:
                continue
            for row in records:
                if 'received_at' not in row:
                    continue
                for key in ('event_at', 'assigned_at', 'signed_at', 'paid_at', 'issued_at'):
                    if row.get(key):
                        self.assertLessEqual(instant(row[key]), instant(row['received_at']))

    def test_seed_and_live_source_have_one_contract(self):
        from app.contracts import load
        self.assertEqual(self.data, load('data/source.json'))
