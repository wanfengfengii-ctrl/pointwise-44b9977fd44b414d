from contextlib import closing, contextmanager
from pathlib import Path
import sqlite3


def connect(path):
    """Open a connection for the line's explicit transactions."""
    db = sqlite3.connect(path, isolation_level=None)
    try:
        db.execute('PRAGMA foreign_keys = ON')
    except BaseException:
        db.close()
        raise
    return db


@contextmanager
def write_transaction(db):
    if db.in_transaction:
        raise sqlite3.ProgrammingError('line operation requires an idle connection')
    db.execute('BEGIN IMMEDIATE')
    try:
        yield
        db.commit()
    except BaseException:
        db.rollback()
        raise


def valid_database_id(value):
    return type(value) is int and 0 < value <= (1 << 63) - 1


def initialize(db):
    from line.calibration.revisions import initialize_calibration
    from line.intake.registry import initialize_intake
    from line.capture.journal import initialize_capture
    from line.closing.store import initialize_closing
    from line.report.catalog import initialize_reports

    with write_transaction(db):
        initialize_calibration(db)
        initialize_intake(db)
        initialize_capture(db)
        initialize_closing(db)
        initialize_reports(db)


def _demonstrate(directory):
    import json
    from shutil import copyfile

    from line.calibration.revisions import calibrate
    from line.capture.admission import (
        ingest,
        ingest_recording,
        receive,
        release,
    )
    from line.closing.cut import seal
    from line.intake.shifts import open as open_shift, register
    from line.report.publish import publish
    from line.report.readback import read

    samples = Path(__file__).resolve().parents[1] / 'samples'
    recordings = [directory / name for name in ('left.csv', 'right.csv')]
    database = directory / 'workday.sqlite'
    for path in recordings:
        copyfile(samples / path.name, path)

    with closing(connect(database)) as db:
        initialize(db)
        roster = [('R-A', 'R'), ('L-A', 'L'), ('R-B', 'R'), ('L-B', 'L')]
        register(db, 'day', 'L', 'R', roster)
        left_version = calibrate(db, 'L', '3/2', 1, 0, 20, 16)
        right_version = calibrate(db, 'R', 2, -1, 0, 20, 10)
        open_shift(db, 'day', left_version, right_version)
        for station, path in zip(('L', 'R'), recordings, strict=True):
            with path.open(encoding='utf-8', newline='') as source:
                ingest_recording(db, 'day', station, source)
        left = seal(db, 'day', 'L', 2)
        right = seal(db, 'day', 'R', 1)
        original_id = publish(db, left.seal_id, right.seal_id)

    for path in recordings:
        path.unlink()

    with closing(connect(database)) as db:
        ingest(db, 'day', 'L', 'L-A', 'L3', 12, 5)
        later_left = seal(db, 'day', 'L', 3)
        later_id = publish(db, later_left.seal_id, right.seal_id)
        initialize(db)

    with closing(connect(database)) as db:
        original = read(db, original_id)
        later = read(db, later_id)

    original_left = original.items[1]
    expected = {'parents': [left.seal_id, right.seal_id], 'roster': ['R-A', 'L-A', 'R-B', 'L-B'],
                'maxima_um': ['7', '16', None, None], 'left_source_lines': [2, 4],
                'left_raw_codes': ['0', '10'], 'left_profile': left_version, 'left_status': 'within_limit'}
    actual = {'parents': [original.left_seal_id, original.right_seal_id],
              'roster': [item.specimen_id for item in original.items],
              'maxima_um': [None if item.maximum_um is None else str(item.maximum_um)
                            for item in original.items],
              'left_source_lines': [row.source_line for row in original_left.readings],
              'left_raw_codes': [str(row.raw_code) for row in original_left.readings],
              'left_profile': original_left.profile_version, 'left_status': original_left.status}
    downstream_expected = {'left_max_um': '19', 'left_status': 'over_limit', 'left_count': 3}
    downstream_actual = {'left_max_um': str(later.items[1].maximum_um),
                         'left_status': later.items[1].status, 'left_count': later.items[1].count}
    if (actual, downstream_actual) != (expected, downstream_expected):
        raise AssertionError((actual, downstream_actual))
    print(f'Issued reports {original_id} and {later_id}; retrieved both after removing the recordings.')
    print('POINTWISE_SCENARIO ' + json.dumps({
        'case_id': 'v7', 'input': {'initial_cuts': [2, 1], 'later_left_cut': 3,
                                   'added_raw_code': 12, 'recordings_available': any(p.exists() for p in recordings)},
        'expected': expected, 'actual': actual,
        'downstream_expected': downstream_expected, 'downstream_actual': downstream_actual,
        'code_paths': ['line/capture/admission.py', 'line/closing/cut.py',
                       'line/report/publish.py', 'line/report/readback.py'],
        'oracle': 'Left 10 times 3/2 plus 1 is 16, left 12 is 19, and right 4 times 2 minus 1 is 7. The first report retains its two issued seal numbers and rows 2 and 4.'
    }, sort_keys=True))
    _demonstrate_transfers(directory)


def _demonstrate_transfers(directory):
    import json

    from line.calibration.revisions import calibrate
    from line.capture.admission import ingest, receive, release
    from line.closing.cut import seal
    from line.intake.shifts import open as open_shift, register
    from line.report.publish import PublishRejected, publish
    from line.report.readback import read

    database = directory / 'transfers.sqlite'
    with closing(connect(database)) as db:
        initialize(db)
        register(db, 'day', 'L', 'R', [('U', 'R'), ('T', 'L')])
        left_version = calibrate(db, 'L', '3/2', 1, 0, 20, 16)
        right_version = calibrate(db, 'R', 2, -1, 0, 20, 10)
        open_shift(db, 'day', left_version, right_version)
        ingest(db, 'day', 'L', 'T', 'L1', 10, 2)
        l2 = ingest(db, 'day', 'L', 'T', 'L2', 4, 4)
        ingest(db, 'day', 'R', 'U', 'R1', 4, 2)

        left_two = seal(db, 'day', 'L', 2)
        right_one = seal(db, 'day', 'R', 1)
        home_id = publish(db, left_two.seal_id, right_one.seal_id)

        released = release(db, 'day', 'L', 'H1', 'T', 'L2')
        left_three = seal(db, 'day', 'L', 3)
        in_transit_id = publish(db, left_three.seal_id, right_one.seal_id)
        # A pair that includes the receive without its release must not sign.
        received = receive(db, 'day', 'R', 'H1')
        right_two = seal(db, 'day', 'R', 2)
        try:
            publish(db, left_two.seal_id, right_two.seal_id)
        except PublishRejected:
            pass
        else:  # pragma: no cover - defensive, assertions below carry the real failure
            raise AssertionError('receive-without-release pair was accepted')
        continued = ingest(db, 'day', 'R', 'T', 'T-R', 6, 5)
        right_three = seal(db, 'day', 'R', 3)
        right_id = publish(db, left_three.seal_id, right_three.seal_id)
        fixed = {'home': home_id, 'in_transit': in_transit_id, 'right': right_id}
        # Replaying the original actions reclaims the original local receipts.
        replay_release = release(db, 'day', 'L', 'H1', 'T', 'L2')
        replay_receive = receive(db, 'day', 'R', 'H1')

    # Source closed: reopen with a fresh connection and claim the fixed accounts by number.
    with closing(connect(database)) as reopened:
        views = {name: read(reopened, report_id) for name, report_id in fixed.items()}
        rows = {
            name: tuple((item.specimen_id, item.location, item.count,
                         None if item.maximum_um is None else str(item.maximum_um), item.status,
                         None if item.handover is None else (
                             item.handover.transfer_id, item.handover.release_sequence,
                             item.handover.receive_sequence, item.handover.origin.reading_id,
                             str(item.handover.origin.raw_code),
                             str(item.handover.origin.thickness_um),
                             item.handover.origin.source_line,
                             item.handover.origin.source_station))
                        for item in view.items)
            for name, view in views.items()
        }

    expected_rows = {
        'home': (('U', 'right', 1, '7', 'within_limit', None),
                 ('T', 'left', 2, '16', 'within_limit', None)),
        'in_transit': (('U', 'right', 1, '7', 'within_limit', None),
                       ('T', 'in_transit', 2, '16', 'within_limit',
                        ('H1', 3, None, 'L2', '4', '7', 4, 'L'))),
        'right': (('U', 'right', 1, '7', 'within_limit', None),
                  ('T', 'right', 1, '11', 'over_limit',
                   ('H1', 3, 2, 'L2', '4', '7', 4, 'L'))),
    }
    if rows != expected_rows:
        raise AssertionError((rows, expected_rows))
    if (released.sequence, received.sequence, replay_release.sequence, replay_receive.sequence,
            l2.sequence, continued.sequence) != (3, 2, 3, 2, 2, 3):
        raise AssertionError('transfer sequences were not preserved on replay')
    print('Transfer handover signed in-transit report', in_transit_id,
          'and right-station report', right_id, 'after continuation; both claimed after reopen.')
    print('POINTWISE_SCENARIO ' + json.dumps({
        'case_id': 'v8.transfers',
        'input': {'roster': [['U', 'R'], ['T', 'L']], 'transfer_id': 'H1',
                  'release_reading': 'L2', 'continued_reading': 'T-R'},
        'expected': expected_rows, 'actual': rows,
        'sequences': {'release': released.sequence, 'receive': received.sequence,
                      'continued': continued.sequence},
        'code_paths': ['line/capture/admission.py', 'line/capture/transfers.py',
                       'line/closing/prefix.py', 'line/report/assembly.py',
                       'line/report/provenance.py'],
        'oracle': 'Release occupies left sequence 3 and receive right sequence 2; the in-transit account keeps left reading L2 (raw 4, 7 um) as handover origin, while continuation T-R measures 11 um over the right limit of 10.'
    }, sort_keys=True))


def main():
    from tempfile import TemporaryDirectory

    with TemporaryDirectory(prefix='coating-line-') as directory:
        _demonstrate(Path(directory))


if __name__ == '__main__':
    main()
