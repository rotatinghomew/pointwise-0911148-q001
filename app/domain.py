from collections import Counter
from pathlib import Path

from app.contracts import latest, load
from app.repository import digest

TABLES = ['schedule', 'designs', 'exposures', 'orders', 'closures', 'coverage']

def exposure_coverage(snapshot, options):
    schedules = snapshot['schedule']
    observed = {(row['region'], row['event_period']) for row in snapshot['exposures']}
    expected = {(row['region'], row['period']) for row in schedules}
    complete = {(row['region'], row['period']) for row in snapshot['coverage'] if row['status'] == 'observed'}
    return {'periods': len(expected), 'sequences': len({row['sequence'] for row in schedules}),
            'exposed_periods': len(observed & expected),
            'unobserved_periods': sorted(expected - observed),
            'confirmed_zero_periods': sorted(complete - observed),
            'missing_periods': sorted(expected - complete),
            'assigned': dict(Counter(str(row['assigned']) for row in schedules)),
            'orders': len(snapshot['orders']), 'closures': len(snapshot['closures'])}

def action(repository, request):
    raise ValueError('尚未登记该业务的可写操作')

OPERATIONS = {'exposure_coverage': exposure_coverage}


def seed_dataset():
    return load(Path(__file__).resolve().parents[1] / 'data/source.json')
