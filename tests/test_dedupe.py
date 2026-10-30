from flare_record.dedupe import merge_duplicates

SNAP = 'https://www.lmsal.com/solarsoft/ssw/last_events-2026/last_events_{}/index.html'


def E(id, start, stop, peak, cls, pos, snaps):
    return {'event_id': id, 'event_start': start, 'event_stop': stop, 'event_peak': peak, 'event_GOES': cls,
            'event_position': pos, 'seen_in_dates': [SNAP.format(s) for s in snaps]}


def test_pair_listed_together_keeps_the_earlier_start_and_the_ar_number():
    out = merge_duplicates([
        E('gev_2121', '2026/10/01 21:21:00', '21:56:16', '21:37:00', 'C1.5', 'N22E64', ['20261002_1201']),
        E('gev_2120', '2026/10/01 21:20:00', '21:58:00', '21:37:00', 'C1.5', 'N22E64( 4545 )', ['20261002_1201'])])
    assert [e['event_id'] for e in out] == ['gev_2120']
    assert out[0]['event_position'] == 'N22E64( 4545 )'
    assert [m['event_id'] for m in out[0]['merged_from']] == ['gev_2121']


def test_replaced_entry_keeps_lmsal_latest_version():
    # LMSAL listed 0104 until 04-12, then only 0146 from 04-13: 0146 is its correction
    out = merge_duplicates([
        E('gev_0104', '2020/04/07 01:04:00', '02:21:00', '01:59:00', 'B1.0', 'N05W10', ['20200407_1202', '20200412_1202']),
        E('gev_0146', '2020/04/07 01:46:00', '02:26:00', '01:59:00', 'B1.0', 'N05W10', ['20200413_1202'])])
    assert [e['event_id'] for e in out] == ['gev_0146']


def test_different_peak_class_or_position_are_not_merged():
    snaps = ['20230527_0000']
    out = merge_duplicates([
        E('a', '2023/05/26 00:58:00', '02:02:00', '01:35:00', 'C1.3', 'S18E19( 3315 )', snaps),
        E('b', '2023/05/26 01:02:00', '01:43:53', '01:22:00', 'C1.3', 'S18E19( 3315 )', snaps),   # peak differs
        E('c', '2023/05/26 00:59:00', '02:00:00', '01:35:00', 'C1.4', 'S18E19( 3315 )', snaps),   # class differs
        E('d', '2023/05/26 01:00:00', '02:00:00', '01:35:00', 'C1.3', 'S19E19( 3315 )', snaps),   # position differs
        E('e', '2023/05/26 01:01:00', '02:00:00', '01:35:00', 'C1.3', 'N11E36( 3317 )', snaps)])  # position and region differ
    assert len(out) == 5 and all('merged_from' not in e for e in out)


def test_running_again_changes_nothing():
    events = [E('a', '2026/10/01 21:21:00', '21:56:16', '21:37:00', 'C1.5', 'N22E64', ['20261004_0001']),
              E('b', '2026/10/01 21:20:00', '21:58:00', '21:37:00', 'C1.5', 'N22E64( 4545 )', ['20261004_0001'])]
    once = merge_duplicates(events)
    assert merge_duplicates(once) == once
