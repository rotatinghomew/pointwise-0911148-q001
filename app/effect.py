"""排班分配效果分析。

分析对象是 ``randomization_set`` 抽取出的完整排班序列，而不是单条订单。同一序列内的
半小时时段不是独立抽签，因此效果按“排班把某片区某时段设为补贴开/关”这一随机化对比
估计，标准误按 ``randomization_set`` 聚类。

停投、覆盖漏报、观察截止之后、结果未报齐和设计不一致的时段只作为“不可用证据”列出，
不补零，也不按成交结果挑选时段。残留影响窗口用于展示结论对上一时段流量延续的敏感度。
"""
import math
from collections import defaultdict
from datetime import timedelta

import numpy as np
from scipy import stats

from app.contracts import instant, latest
from app.repository import digest

PERIOD_MINUTES = 30
DEFAULT_WINDOWS = [0, 1, 2, 3]
MAX_WINDOW = 48

EXCLUSION_REASONS = {
    'design_mismatch': '该 randomization_set 的排班与候选序列或抽取概率不一致，整组不进入估计。',
    'closure': '运营登记的停投区间。停投不等于没有流量，但该时段不能作为开/关证据。',
    'coverage_missing': '该片区时段没有确认报齐的覆盖记录，不能当作零曝光。',
    'after_observation_cutoff': '观察截止时该时段尚未结束，曝光和成交可能不完整。',
    'outcome_unreported': '有曝光记录但没有任何可见订单记录，无法区分未成交与未报齐。',
    'carryover_series_start': '窗口内的前序时段落在排班序列起点之前，无法确认延续状态。',
    'carryover_after_arm_change': '窗口内前序时段与本时段补贴状态不同，残留影响未消退。',
    'carryover_after_closure': '窗口内前序时段停投，延续状态不可确认。',
    'carryover_after_coverage_missing': '窗口内前序时段覆盖未报齐，延续状态不可确认。',
    'carryover_after_cutoff': '窗口内前序时段在观察截止时尚未结束。',
    'carryover_after_outcome_unreported': '窗口内前序时段结果未报齐，延续状态不可确认。',
    'carryover_after_design_mismatch': '窗口内前序时段设计校验失败。',
}

METHOD_NOTES = [
    '随机化单位是 randomization_set 抽取出的完整排班序列；同一序列内的 8 个半小时时段不是独立抽签，'
    '不能按订单或单个时段拆开做随机对照。',
    '分配效果是排班层面“补贴开/关”对曝光后成交率的平均影响：按 block_offset 分层，'
    '在相同序列位置跨排班方案比较开/关时段，并按 randomization_set 聚类估计不确定性。',
    '逐组（单个 randomization_set）的开/关差值是描述性的：候选序列互为镜像，臂与序列内位置共线，'
    '其差值混入位置差异，不能当作干净的分配效果。',
    '成交按 exposure_id 归入首次曝光时段；跨时段订单不改变归属，但计入跨界订单统计并受观察截止影响。',
    '按记录约定，每条曝光对应一条订单记录（含 converted=0），因此缺失订单记录按结果未报齐处理，'
    '不按未成交补零；受影响时段整体不进入估计，并同时报告涉及的两臂曝光数。',
    '停投（closures）、覆盖未确认报齐（coverage 非 observed）、观察截止时尚未结束的时段、'
    '结果明显未报齐的时段、设计校验不通过的时段不进入比较；不补零，也不按成交结果挑选时段。',
    '只有 coverage 为 observed 且没有曝光记录的时段才记为确认零曝光；缺覆盖记录的时段不是零曝光。'
    '已知观察截止时，截止前未报回数据的时段同样不记为确认零曝光。',
    '残留影响窗口 k 只使用“当前及此前 k 个时段都可用且同臂”的时段；切换臂、停投、漏报或观察截止'
    '之后的时段会落到不同窗口之外，因此不同窗口的可用证据和结论会变化。',
    '实际领券人群的对比是描述性的：领券发生在分配之后，领取者与未领取者的差异包含选择效应，'
    '不等于排班分配效果。',
    '结果保留观察截止、所用排班、候选序列、分析选项和各窗口排除理由，历史分析链接可还原当时的判断依据。',
]


def _validate_options(options):
    if options is None:
        options = {}
    if not isinstance(options, dict):
        raise ValueError('分析选项须为对象')
    unknown = set(options) - {'carryover_periods'}
    if unknown:
        raise ValueError('未支持的分析选项：' + ','.join(sorted(unknown)))
    windows = options.get('carryover_periods', list(DEFAULT_WINDOWS))
    if not isinstance(windows, list) or not windows:
        raise ValueError('carryover_periods 须为非空整数数组')
    cleaned = []
    for window in windows:
        if isinstance(window, bool) or not isinstance(window, int):
            raise ValueError('carryover_periods 须为非负整数')
        if window < 0 or window > MAX_WINDOW:
            raise ValueError('carryover_periods 超出支持范围')
        if window in cleaned:
            raise ValueError('carryover_periods 不能重复')
        cleaned.append(window)
    return {'carryover_periods': cleaned}


def _design_audit(schedule_rows, design_rows):
    candidates = defaultdict(dict)
    reasons = defaultdict(set)
    for row in design_rows:
        randomization_set = row['randomization_set']
        candidates[randomization_set][row['sequence']] = row
        assignments = row.get('assignments')
        probability = row.get('probability')
        if not isinstance(assignments, list) or not assignments:
            reasons[randomization_set].add('invalid_assignments')
        elif any(value not in (0, 1) for value in assignments):
            reasons[randomization_set].add('invalid_assignment_value')
        if isinstance(probability, bool) or not isinstance(probability, (int, float)) \
                or not 0 < float(probability) <= 1:
            reasons[randomization_set].add('invalid_sequence_probability')
    for randomization_set, sequences in candidates.items():
        total = 0.0
        for row in sequences.values():
            probability = row.get('probability')
            try:
                total += float(probability)
            except (TypeError, ValueError):
                reasons[randomization_set].add('invalid_sequence_probability')
        if abs(total - 1.0) > 1e-9:
            reasons[randomization_set].add('probabilities_not_normalized')

    rows_by_set = defaultdict(list)
    for row in schedule_rows:
        rows_by_set[row['randomization_set']].append(row)

    for randomization_set, rows in rows_by_set.items():
        if randomization_set not in candidates:
            reasons[randomization_set].add('unknown_randomization_set')
        sequences = {row.get('sequence') for row in rows}
        if len(sequences) != 1:
            reasons[randomization_set].add('multiple_sequences_in_set')
        regions = {row.get('region') for row in rows}
        if len(regions) != 1:
            reasons[randomization_set].add('multiple_regions_in_set')
        periods = [row.get('period') for row in rows]
        offsets = [row.get('block_offset') for row in rows]
        if len(periods) != len(set(periods)):
            reasons[randomization_set].add('duplicate_period')
        if len(offsets) != len(set(offsets)):
            reasons[randomization_set].add('duplicate_block_offset')
        for row in rows:
            if row.get('assigned') not in (0, 1):
                reasons[randomization_set].add('invalid_assigned_value')
                continue
            sequence = candidates.get(randomization_set, {}).get(row.get('sequence'))
            if sequence is None:
                reasons[randomization_set].add('unknown_sequence')
                continue
            offset = row.get('block_offset')
            assignments = sequence.get('assignments', [])
            if isinstance(offset, bool) or not isinstance(offset, int) \
                    or not 0 <= offset < len(assignments):
                reasons[randomization_set].add('block_offset_out_of_range')
                continue
            if assignments[offset] != row['assigned']:
                reasons[randomization_set].add('assigned_mismatch')
            try:
                schedule_probability = float(row.get('probability'))
            except (TypeError, ValueError):
                reasons[randomization_set].add('invalid_schedule_probability')
                continue
            if not 0 < schedule_probability <= 1:
                reasons[randomization_set].add('invalid_schedule_probability')
            try:
                if abs(float(sequence.get('probability', 0)) - schedule_probability) > 1e-9:
                    reasons[randomization_set].add('probability_mismatch')
            except (TypeError, ValueError):
                reasons[randomization_set].add('probability_mismatch')

    assignment_probability = {}
    for randomization_set, sequences in candidates.items():
        table = defaultdict(lambda: {'0': 0.0, '1': 0.0})
        for sequence in sequences.values():
            assignments = sequence.get('assignments') or []
            try:
                probability = float(sequence.get('probability', 0))
            except (TypeError, ValueError):
                probability = 0.0
            for offset, value in enumerate(assignments):
                if value in (0, 1):
                    table[offset][str(int(value))] += probability
        assignment_probability[randomization_set] = {offset: dict(values)
                                                     for offset, values in table.items()}

    return ({name: sorted(values) for name, values in reasons.items() if values},
            assignment_probability)


def _closure_periods(rows, schedule):
    period_rows = [row for row in rows if 'period' in row and 'start_at' not in row]
    interval_rows = [row for row in rows if 'start_at' in row and 'end_at' in row]
    closed = set()
    for row in latest(period_rows, ('region', 'period')):
        closed.add((row['region'], row['period']))
    for row in latest(interval_rows, ('region', 'start_at', 'end_at')):
        start, end = instant(row['start_at']), instant(row['end_at'])
        for (region, period), block in schedule.items():
            if region != row['region']:
                continue
            block_start = instant(block['event_at'])
            if block_start < end and start < block_start + timedelta(minutes=PERIOD_MINUTES):
                closed.add((region, period))
    return closed


def _fit(records):
    """按 randomization_set 聚类、以 block_offset 分层的分配效果估计。"""
    if not records:
        return {'status': 'not_estimable', 'reason': 'no_evaluable_exposures'}
    clusters = sorted({record['cluster'] for record in records})
    offsets = sorted({record['offset'] for record in records})
    if len(clusters) < 2:
        return {'status': 'not_estimable', 'reason': 'insufficient_randomization_sets',
                'clusters': len(clusters)}
    if {record['arm'] for record in records} != {0, 1}:
        return {'status': 'not_estimable', 'reason': 'one_arm_only', 'clusters': len(clusters)}

    columns = []
    for record in records:
        column = [1.0, float(record['arm'])]
        column.extend(1.0 if record['offset'] == value else 0.0 for value in offsets[1:])
        columns.append(column)
    matrix = np.asarray(columns, dtype=float)
    outcome = np.asarray([record['y'] for record in records], dtype=float)
    weights = np.asarray([record['weight'] for record in records], dtype=float)
    if not np.all(weights > 0):
        return {'status': 'not_estimable', 'reason': 'invalid_assignment_probability',
                'clusters': len(clusters)}
    weighted = matrix * np.sqrt(weights)[:, None]
    if np.linalg.matrix_rank(weighted) < matrix.shape[1]:
        return {'status': 'not_estimable', 'reason': 'design_not_identified',
                'clusters': len(clusters)}
    inverse = np.linalg.pinv(weighted.T @ weighted)
    coefficients = inverse @ (weighted.T @ (outcome * np.sqrt(weights)))
    residual = outcome - matrix @ coefficients

    meat = np.zeros((matrix.shape[1], matrix.shape[1]))
    for cluster in clusters:
        mask = np.asarray([record['cluster'] == cluster for record in records])
        score = (matrix[mask] * weights[mask, None]).T @ residual[mask]
        meat += np.outer(score, score)
    count = len(clusters)
    covariance = inverse @ meat @ inverse * (count / (count - 1))

    estimate = float(coefficients[1])
    variance = float(covariance[1, 1])
    result = {
        'status': 'ok',
        'estimate': estimate,
        'clusters': count,
        'cluster_by': 'randomization_set',
        'df': count - 1,
        'weights': 'inverse assignment probability within randomization_set and block_offset',
        'stratified_by': 'block_offset',
        'method': 'exposure-level weighted least squares of conversion on assignment with '
                  'block_offset fixed effects; cluster-robust standard errors by randomization_set',
    }
    if variance > 0:
        error = math.sqrt(variance)
        critical = float(stats.t.ppf(0.975, count - 1))
        result.update(std_error=error, ci_level=0.95,
                      ci_low=estimate - critical * error,
                      ci_high=estimate + critical * error)
    else:
        result.update(std_error=None, ci_level=None, ci_low=None, ci_high=None,
                      note='方差估计为零，无法给出置信区间')
    return result


def _robustness(windows):
    summary = []
    for window in windows:
        effect = window['assignment_effect']
        item = {
            'carryover_periods': window['carryover_periods'],
            'usable_periods': len(window['usable_periods']),
            'evaluable_exposures': window['evaluable_exposures'],
            'clusters': effect.get('clusters', 0),
            'estimable': effect.get('status') == 'ok',
        }
        if item['estimable']:
            item.update(estimate=effect['estimate'], ci_low=effect.get('ci_low'),
                        ci_high=effect.get('ci_high'), direction=_direction(effect['estimate']))
        else:
            item['reason'] = effect.get('reason')
            if effect.get('note'):
                item['reason_note'] = effect['note']
        summary.append(item)

    estimable = [item for item in summary if item['estimable']]
    if not estimable:
        return {
            'windows': summary,
            'signal': 'not_estimable',
            'estimable_windows': [],
            'note': '所有请求的残留影响窗口都没有足够可用证据，无法形成分配效果结论。',
        }
    excluding_zero = [item for item in estimable
                      if item.get('ci_low') is not None and item.get('ci_high') is not None
                      and (item['ci_low'] > 0 or item['ci_high'] < 0)]
    directions = {item['direction'] for item in estimable}
    if len(excluding_zero) == len(estimable):
        if directions == {'positive'}:
            signal = 'consistent_positive'
            note = '所有可估计窗口的区间都不跨零且方向为正。'
        elif directions == {'negative'}:
            signal = 'consistent_negative'
            note = '所有可估计窗口的区间都不跨零且方向为负。'
        else:
            signal = 'unstable_direction'
            note = '各窗口区间都不跨零，但方向不一致，不能合并成单一结论。'
    elif not excluding_zero:
        signal = 'inconclusive'
        note = '所有可估计窗口的 95% 区间都跨零，现有证据不足以区分补贴开与关。'
    else:
        signal = 'window_sensitive'
        note = '部分残留影响窗口的区间跨零、部分不跨零，结论对窗口选择敏感。'
    if len(directions) > 1:
        note += ' 点估计方向在不同窗口间发生变化（' + '、'.join(
            sorted(directions)) + '），应同时查看逐窗口结果。'
    return {
        'windows': summary,
        'signal': signal,
        'estimable_windows': [item['carryover_periods'] for item in estimable],
        'direction_by_window': {str(item['carryover_periods']): item['direction']
                                for item in estimable},
        'note': note,
    }


def _direction(value):
    if value > 0:
        return 'positive'
    if value < 0:
        return 'negative'
    return 'zero'


def schedule_effect(snapshot, options=None, as_of=None):
    """估计排班层面的补贴分配效果，并按残留影响窗口给出敏感性。"""
    analysis_options = _validate_options(options)
    windows = analysis_options['carryover_periods']
    cutoff = instant(as_of) if as_of is not None else None
    period = timedelta(minutes=PERIOD_MINUTES)

    schedule_rows = latest(snapshot.get('schedule', []), ('region', 'period'))
    design_rows = latest(snapshot.get('designs', []), ('randomization_set', 'sequence'))
    exposure_rows = latest(snapshot.get('exposures', []), ('exposure_id',))
    order_rows = latest(snapshot.get('orders', []), ('order_id',))
    coverage_rows = latest(snapshot.get('coverage', []), ('region', 'period'))

    schedule = {(row['region'], row['period']): row for row in schedule_rows}
    coverage = {(row['region'], row['period']): row.get('status') for row in coverage_rows}
    closed = _closure_periods(snapshot.get('closures', []), schedule)
    invalid, assignment_probability = _design_audit(schedule_rows, design_rows)

    exposures_by_period = defaultdict(list)
    for row in exposure_rows:
        exposures_by_period[(row['region'], row['event_period'])].append(row)
    orders_by_exposure = defaultdict(list)
    for row in order_rows:
        orders_by_exposure[row['exposure_id']].append(row)
    visible_exposures = {row['exposure_id']: row for row in exposure_rows}

    ordered_schedule = sorted(schedule_rows,
                              key=lambda row: (row['region'], row['period'],
                                               row.get('block_offset', 0)))
    ordered_designs = sorted(design_rows,
                             key=lambda row: (row['randomization_set'], row['sequence']))

    base_reason = {}
    for key, block in schedule.items():
        randomization_set = block['randomization_set']
        exposures = exposures_by_period.get(key, [])
        if randomization_set in invalid:
            base_reason[key] = 'design_mismatch'
        elif key in closed:
            base_reason[key] = 'closure'
        elif coverage.get(key) != 'observed':
            base_reason[key] = 'coverage_missing'
        elif cutoff is not None and instant(block['event_at']) + period > cutoff:
            base_reason[key] = 'after_observation_cutoff'
        elif any(not orders_by_exposure.get(exposure['exposure_id']) for exposure in exposures):
            base_reason[key] = 'outcome_unreported'

    unlinked = {'total': 0, 'by_arm': {'0': 0, '1': 0}, 'periods': set()}
    for key, exposures in exposures_by_period.items():
        block = schedule.get(key)
        if block is None:
            continue
        for exposure in exposures:
            if orders_by_exposure.get(exposure['exposure_id']):
                continue
            arm = str(block['assigned'])
            unlinked['total'] += 1
            unlinked['by_arm'][arm] = unlinked['by_arm'].get(arm, 0) + 1
            unlinked['periods'].add(key)

    cross_period = {'orders': 0, 'converted': 0, 'within_period_orders': 0,
                    'within_period_converted': 0, 'orders_without_visible_exposure': 0,
                    'event_before_exposure_period': 0}
    cross_region = {'orders': 0, 'converted': 0}
    lags = []
    for row in order_rows:
        exposure = visible_exposures.get(row['exposure_id'])
        if exposure is None:
            cross_period['orders_without_visible_exposure'] += 1
            continue
        if row.get('region') != exposure['region']:
            cross_region['orders'] += 1
            cross_region['converted'] += int(row.get('converted', 0))
        start = instant(schedule[(exposure['region'], exposure['event_period'])]['event_at'])
        event_at = instant(row['event_at'])
        lag = (event_at - start).total_seconds() / 60
        lags.append(lag)
        if lag < 0:
            cross_period['event_before_exposure_period'] += 1
        elif lag < PERIOD_MINUTES:
            cross_period['within_period_orders'] += 1
            cross_period['within_period_converted'] += int(row.get('converted', 0))
        else:
            cross_period['orders'] += 1
            cross_period['converted'] += int(row.get('converted', 0))
    total_orders = cross_period['within_period_orders'] + cross_period['orders']
    lags.sort()
    cross_period.update({
        'total_orders': total_orders,
        'share': round(cross_period['orders'] / total_orders, 6) if total_orders else None,
        'median_lag_minutes': round(lags[len(lags) // 2], 3) if lags else None,
        'max_lag_minutes': round(lags[-1], 3) if lags else None,
        'min_lag_minutes': round(lags[0], 3) if lags else None,
        'attribution': '按 exposure_id 归入首次曝光时段，跨时段或跨片区不改变归属',
    })

    coverage_missing = sorted(key for key in schedule if coverage.get(key) != 'observed')
    after_cutoff = sorted(key for key in schedule if base_reason.get(key) == 'after_observation_cutoff')
    confirmed_zero = sorted(key for key in schedule
                            if coverage.get(key) == 'observed' and key not in exposures_by_period)
    outcome_unreported = sorted(key for key in schedule
                                if base_reason.get(key) == 'outcome_unreported')
    design_mismatch = sorted(key for key in schedule if base_reason.get(key) == 'design_mismatch')

    evidence = {
        'observation_cutoff': as_of,
        'periods_in_schedule': len(schedule),
        'closures': [list(key) for key in sorted(closed)],
        'closure_exposures': sum(len(exposures_by_period.get(key, [])) for key in closed),
        'closure_note': '停投不意味着没有流量：停投时段仍可能有曝光记录，但这些时段不作为开/关证据。',
        'coverage_missing': [list(key) for key in coverage_missing],
        'coverage_missing_note': '缺覆盖记录或未确认报齐的时段不能当作零曝光，相关时段不进入估计。',
        'confirmed_zero_exposure': [list(key) for key in confirmed_zero],
        'confirmed_zero_note': '只有 coverage=observed 且无曝光记录的时段才记为确认零曝光。',
        'periods_after_observation_cutoff': [list(key) for key in after_cutoff],
        'outcome_unreported_periods': [list(key) for key in outcome_unreported],
        'outcome_unreported_note': '曝光可见但没有任何可见订单记录的时段无法判断是否成交，'
                                   '按结果未报齐排除，不按未成交补零。',
        'unlinked_exposures': {
            'total': unlinked['total'],
            'by_arm': unlinked['by_arm'],
            'periods': [list(key) for key in sorted(unlinked['periods'])],
            'note': '这些曝光没有可见订单记录，无法区分“未成交”和“未报齐”。',
        },
        'orders_without_visible_exposure': cross_period['orders_without_visible_exposure'],
        'cross_period_orders': cross_period,
        'cross_region_orders': cross_region,
        'design_mismatch_periods': design_mismatch,
        'invalid_design_sets': [{'randomization_set': name, 'reasons': reasons}
                                for name, reasons in sorted(invalid.items())],
    }

    result = {
        'analysis_version': 'schedule_effect/v1',
        'request': {'operation': 'schedule_effect', 'as_of': as_of,
                    'options': analysis_options},
        'randomization_unit': 'randomization_set',
        'estimand': '完整排班序列被随机设为补贴开或关时，对曝光后成交率的平均分配效果',
        'analysis_options': {**analysis_options, 'period_length_minutes': PERIOD_MINUTES},
        'design': {
            'randomization_sets': len({row['randomization_set'] for row in schedule_rows}),
            'candidate_sequences': ordered_designs,
            'schedule_digest': digest(ordered_schedule),
            'schedule_used': ordered_schedule,
            'checks': {
                'sets_checked': len({row['randomization_set'] for row in schedule_rows}),
                'sets_valid': len({row['randomization_set'] for row in schedule_rows}) - len(invalid),
                'invalid_sets': [{'randomization_set': name, 'reasons': reasons}
                                 for name, reasons in sorted(invalid.items())],
            },
        },
        'evidence': evidence,
        'windows': [],
        'robustness': None,
        'method_notes': list(METHOD_NOTES),
    }

    for window in windows:
        excluded = defaultdict(list)
        usable = []
        for key, block in schedule.items():
            reason = base_reason.get(key)
            if reason is not None:
                excluded[reason].append(list(key))
                continue
            if window:
                for lag in range(1, window + 1):
                    previous = schedule.get((key[0], key[1] - lag))
                    if previous is None:
                        reason = 'carryover_series_start'
                        break
                    previous_reason = base_reason.get((key[0], key[1] - lag))
                    if previous_reason is not None:
                        reason = ('carryover_after_cutoff'
                                  if previous_reason == 'after_observation_cutoff'
                                  else 'carryover_after_' + previous_reason)
                        break
                    if previous['assigned'] != block['assigned']:
                        reason = 'carryover_after_arm_change'
                        break
            if reason is not None:
                excluded[reason].append(list(key))
                continue
            usable.append(block)

        arm_exposures = {'0': 0, '1': 0}
        arm_evaluable = {'0': 0, '1': 0}
        arm_conversions = {'0': 0, '1': 0}
        claim = {arm: {'exposures': 0, 'claimed': 0} for arm in ('0', '1')}
        claim_conversion = {True: {'exposures': 0, 'conversions': 0, 'amount_cents': 0,
                                   'converted_amount_cents': 0},
                            False: {'exposures': 0, 'conversions': 0, 'amount_cents': 0,
                                    'converted_amount_cents': 0}}
        per_set = defaultdict(lambda: {'exposures': {'0': 0, '1': 0},
                                       'evaluable': {'0': 0, '1': 0},
                                       'conversions': {'0': 0, '1': 0},
                                       'periods': []})
        records = []
        for block in usable:
            key = (block['region'], block['period'])
            arm = str(block['assigned'])
            randomization_set = block['randomization_set']
            summary = per_set[randomization_set]
            summary['periods'].append(list(key))
            probability = assignment_probability.get(randomization_set, {}) \
                .get(block['block_offset'], {}).get(arm, 0.0)
            for exposure in exposures_by_period.get(key, []):
                arm_exposures[arm] += 1
                claim[arm]['exposures'] += 1
                claimed = bool(exposure.get('coupon_claimed'))
                if claimed:
                    claim[arm]['claimed'] += 1
                orders = orders_by_exposure.get(exposure['exposure_id'], [])
                if not orders:
                    continue
                converted = int(any(int(order.get('converted', 0)) for order in orders))
                amount = sum(int(order.get('amount_cents', 0)) for order in orders)
                arm_evaluable[arm] += 1
                arm_conversions[arm] += converted
                summary['exposures'][arm] += 1
                summary['evaluable'][arm] += 1
                summary['conversions'][arm] += converted
                if arm == '1':
                    bucket = claim_conversion[claimed]
                    bucket['exposures'] += 1
                    bucket['conversions'] += converted
                    bucket['amount_cents'] += amount
                    bucket['converted_amount_cents'] += amount if converted else 0
                if probability > 0:
                    records.append({'y': converted, 'arm': int(block['assigned']),
                                    'offset': block['block_offset'],
                                    'cluster': randomization_set,
                                    'weight': 1.0 / probability})

        rates = {arm: (round(arm_conversions[arm] / arm_evaluable[arm], 6)
                       if arm_evaluable[arm] else None) for arm in ('0', '1')}
        unadjusted = (rates['1'] - rates['0']) if rates['1'] is not None and rates['0'] is not None else None
        set_details = []
        for randomization_set, summary in sorted(per_set.items()):
            rate_off = (summary['conversions']['0'] / summary['evaluable']['0']
                        if summary['evaluable']['0'] else None)
            rate_on = (summary['conversions']['1'] / summary['evaluable']['1']
                       if summary['evaluable']['1'] else None)
            set_details.append({
                'randomization_set': randomization_set,
                'usable_periods': sorted(summary['periods']),
                'evaluable_exposures': summary['evaluable'],
                'conversions': summary['conversions'],
                'conversion_rate': {'off': rate_off, 'on': rate_on},
                'raw_within_set_contrast': (rate_on - rate_off)
                if rate_on is not None and rate_off is not None else None,
                'note': '描述性：候选序列互为镜像，臂与序列内位置共线，逐组差值混入位置差异。',
            })

        claimed = claim_conversion[True]
        unclaimed = claim_conversion[False]
        claimed_rate = (claimed['conversions'] / claimed['exposures']
                        if claimed['exposures'] else None)
        unclaimed_rate = (unclaimed['conversions'] / unclaimed['exposures']
                          if unclaimed['exposures'] else None)
        claim_contrast = {
            'descriptive_only': True,
            'note': '领券发生在分配之后；领取者与未领取者的成交差异包含选择效应，不是随机分配效果。',
            'claim_rate_by_arm': {
                arm: {'exposures': claim[arm]['exposures'], 'claimed': claim[arm]['claimed'],
                      'rate': round(claim[arm]['claimed'] / claim[arm]['exposures'], 6)
                      if claim[arm]['exposures'] else None}
                for arm in ('0', '1')
            },
            'claims_in_off_arm': claim['0']['claimed'],
            'claims_in_off_arm_note': '关补贴时段出现领券属于分配与执行不一致，按原排班保留，'
                                      '不按领券结果重分时段。',
            'within_assigned_on': {
                'claimed': {'exposures': claimed['exposures'],
                            'conversions': claimed['conversions'],
                            'conversion_rate': claimed_rate,
                            'amount_cents': claimed['amount_cents'],
                            'converted_amount_cents': claimed['converted_amount_cents']},
                'not_claimed': {'exposures': unclaimed['exposures'],
                                'conversions': unclaimed['conversions'],
                                'conversion_rate': unclaimed_rate,
                                'amount_cents': unclaimed['amount_cents'],
                                'converted_amount_cents': unclaimed['converted_amount_cents']},
                'difference': (claimed_rate - unclaimed_rate)
                if claimed_rate is not None and unclaimed_rate is not None else None,
            },
        }

        result['windows'].append({
            'carryover_periods': window,
            'usable_periods': sorted([block['region'], block['period']] for block in usable),
            'usable_period_count': len(usable),
            'excluded_counts': {reason: len(items) for reason, items in sorted(excluded.items())},
            'excluded_periods': {reason: sorted(items) for reason, items in sorted(excluded.items())},
            'exclusion_reasons': EXCLUSION_REASONS,
            'exposures': arm_exposures,
            'evaluable_exposures': arm_evaluable,
            'conversions': arm_conversions,
            'descriptive_arm_conversion_rate': rates,
            'descriptive_arm_rate_difference': unadjusted,
            'descriptive_arm_note': '按实际分配臂汇总的成交率差异，未做序列位置调整；'
                                    '仅用于描述样本，不作为分配效果。',
            'assignment_effect': _fit(records),
            'per_randomization_set': set_details,
            'descriptive_claim_contrast': claim_contrast,
        })

    result['robustness'] = _robustness(result['windows'])
    return result
