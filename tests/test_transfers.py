import json
import sqlite3
from contextlib import closing
from fractions import Fraction
from io import StringIO

from line.calibration.revisions import calibrate
from line.capture.admission import (
    IngestRejected,
    TransferRejected,
    ingest,
    ingest_recording,
    receive,
    release,
)
from line.capture.transfers import TransferJournal, local_head
from line.closing.cut import SealRejected, seal
from line.closing.store import SealStore
from line.report.publish import PublishRejected, publish
from line.report.readback import ReportNotFound, read
from line.workday import connect, initialize
from tests.support import SAMPLES, LineCase, report_counts


TRANSFER_ROSTER = (('U', 'R'), ('T', 'L'))


class TransferCase(LineCase):
    def open_transfer_shift(self, shift_id='day'):
        from line.intake.shifts import open as open_shift, register

        register(self.db, shift_id, 'L', 'R', TRANSFER_ROSTER)
        left = calibrate(self.db, 'L', '3/2', 1, 0, 20, 16)
        right = calibrate(self.db, 'R', 2, -1, 0, 20, 10)
        open_shift(self.db, shift_id, left, right)
        return left, right

    def seed_measurements(self):
        l1 = ingest(self.db, 'day', 'L', 'T', 'L1', 10, 2)
        l2 = ingest(self.db, 'day', 'L', 'T', 'L2', 4, 4)
        r1 = ingest(self.db, 'day', 'R', 'U', 'R1', 4, 2)
        return l1, l2, r1


class JointScenarioTests(TransferCase):
    def test_station_events_fixture(self):
        events = json.loads((SAMPLES / 'station_events.json').read_text(encoding='utf-8'))
        self.open_transfer_shift()
        self.seed_measurements()

        left_two = seal(self.db, 'day', 'L', 2)
        right_one = seal(self.db, 'day', 'R', 1)
        report_two_one = publish(self.db, left_two.seal_id, right_one.seal_id)

        released = release(self.db, 'day', 'L', 'H1', 'T', 'L2')
        self.assertEqual((released.transfer_id, released.sequence),
                         (events['events'][0]['transfer_id'], events['events'][0]['position']))
        left_three = seal(self.db, 'day', 'L', 3)

        in_transit_id = publish(self.db, left_three.seal_id, right_one.seal_id)
        received = receive(self.db, 'day', 'R', 'H1')
        self.assertEqual((received.transfer_id, received.sequence),
                         (events['events'][1]['transfer_id'], events['events'][1]['position']))
        continued = ingest(self.db, 'day', 'R', 'T', 'T-R', 6, 5)
        self.assertEqual((continued.sequence, continued.thickness_um), (3, Fraction(11)))
        right_two = seal(self.db, 'day', 'R', 2)
        right_three = seal(self.db, 'day', 'R', 3)
        right_one_late = seal(self.db, 'day', 'R', 1)

        expectations = {
            (2, 1): ('left', report_two_one),
            (3, 1): ('in_transit', in_transit_id),
        }
        views = {
            (2, 1): read(self.db, report_two_one),
            (3, 1): read(self.db, in_transit_id),
            (3, 2): read(self.db, publish(self.db, left_three.seal_id, right_two.seal_id)),
            (3, 3): read(self.db, publish(self.db, left_three.seal_id, right_three.seal_id)),
        }
        # The late right cutoff 1 paired with left 3 must still adjudicate in transit.
        late_view = read(self.db, publish(self.db, left_three.seal_id, right_one_late.seal_id))

        fixture_locations = {
            tuple(check['cuts']): {'L': 'left', 'R': 'right'}.get(
                check.get('location'), check.get('location', check.get('outcome')))
            for check in events['manual_checks']
        }
        for cuts, view in views.items():
            if cuts not in fixture_locations:
                continue
            item = next(item for item in view.items if item.specimen_id == 'T')
            self.assertEqual(item.location, fixture_locations[cuts], cuts)
        self.assertEqual(next(i for i in late_view.items if i.specimen_id == 'T').location, 'in_transit')
        for cuts, (location, _) in expectations.items():
            self.assertEqual(fixture_locations[cuts], location)

        detail = next(i for i in views[(3, 1)].items if i.specimen_id == 'T')
        self.assertEqual((detail.count, detail.maximum_um, detail.status), (2, Fraction(16), 'within_limit'))
        self.assertEqual(detail.handover.origin.reading_id, 'L2')
        self.assertEqual((detail.handover.origin.raw_code, detail.handover.origin.thickness_um,
                          detail.handover.origin.source_line, detail.handover.origin.source_station,
                          detail.handover.origin.profile_version),
                         (Fraction(4), Fraction(7), 4, 'L', 1))
        self.assertEqual((detail.handover.transfer_id, detail.handover.release_sequence,
                          detail.handover.receive_sequence), ('H1', 3, None))

        right_empty = next(i for i in views[(3, 2)].items if i.specimen_id == 'T')
        self.assertEqual((right_empty.count, right_empty.maximum_um, right_empty.status),
                         (0, None, 'missing'))
        self.assertEqual(right_empty.handover.receive_sequence, 2)
        right_measured = next(i for i in views[(3, 3)].items if i.specimen_id == 'T')
        self.assertEqual((right_measured.count, right_measured.maximum_um, right_measured.status),
                         (1, Fraction(11), 'over_limit'))

        with self.assertRaises(PublishRejected):
            publish(self.db, left_two.seal_id, right_two.seal_id)
        self.assertEqual(report_counts(self.db)[0], 5)

    def test_each_specimen_released_once_and_replays_keep_sequences(self):
        self.open_transfer_shift()
        _, l2, _ = self.seed_measurements()
        first = release(self.db, 'day', 'L', 'H1', 'T', 'L2')
        self.assertEqual(first.sequence, 3)
        # Original release replay returns the original receipt.
        self.assertEqual(release(self.db, 'day', 'L', 'H1', 'T', 'L2'), first)
        # Same transfer id with different parameters is a binding conflict.
        with self.assertRaises(TransferRejected):
            release(self.db, 'day', 'L', 'H1', 'T', 'L1')
        # A second transfer id for the same specimen is refused.
        with self.assertRaises(TransferRejected):
            release(self.db, 'day', 'L', 'H2', 'T', 'L2')
        self.assertEqual(local_head(self.db, 'day', 'L'), 3)

        received = receive(self.db, 'day', 'R', 'H1')
        self.assertEqual(received.sequence, 2)
        self.assertEqual(receive(self.db, 'day', 'R', 'H1'), received)
        # Replaying the original release after receive still returns the left receipt.
        self.assertEqual(release(self.db, 'day', 'L', 'H1', 'T', 'L2').sequence, 3)
        # Unknown transfers and same-id cross-shift reuse cannot advance the right head.
        with self.assertRaises(TransferRejected):
            receive(self.db, 'day', 'R', 'H2')
        self.assertEqual(local_head(self.db, 'day', 'R'), 2)
        self.assertEqual(l2.sequence, 2)

    def test_ingest_gates_around_transfer(self):
        self.open_transfer_shift()
        self.seed_measurements()
        release(self.db, 'day', 'L', 'H1', 'T', 'L2')

        for station, reading_id in (('L', 'L9'), ('R', 'R9')):
            with self.subTest(station=station, phase='in_transit'):
                with self.assertRaises(IngestRejected):
                    ingest(self.db, 'day', station, 'T', reading_id, 2, 9)
        # Replays of accepted readings still reclaim the original receipt while in transit.
        self.assertEqual(ingest(self.db, 'day', 'L', 'T', 'L2', '4/1', 4).sequence, 2)
        # Other specimens keep collecting normally.
        self.assertEqual(ingest(self.db, 'day', 'R', 'U', 'R2', 2, 6).sequence, 2)

        receive(self.db, 'day', 'R', 'H1')
        with self.assertRaises(IngestRejected):
            ingest(self.db, 'day', 'L', 'T', 'L9', 2, 9)
        # R head is already at 2 from U's second reading, so receive takes 3 and this takes 4.
        continued = ingest(self.db, 'day', 'R', 'T', 'T-R', 6, 5)
        self.assertEqual((continued.sequence, continued.thickness_um), (4, Fraction(11)))
        # A reading id already accepted elsewhere cannot be re-created with new parameters.
        with self.assertRaises(IngestRejected):
            ingest(self.db, 'day', 'R', 'T', 'L2', 6, 5)
        self.assertEqual(local_head(self.db, 'day', 'L'), 3)
        self.assertEqual(local_head(self.db, 'day', 'R'), 4)

    def test_recording_batch_keeps_earlier_legal_receipts(self):
        self.open_transfer_shift()
        self.seed_measurements()
        release(self.db, 'day', 'L', 'H1', 'T', 'L2')
        batch = StringIO(
            'specimen_id,reading_id,raw_code\n'
            'U,R3,2\n'
            'T,T-R,6\n')
        with self.assertRaises(IngestRejected):
            ingest_recording(self.db, 'day', 'R', batch)
        # U's legal row was committed first; the rejected T row did not take a sequence.
        self.assertEqual(ingest(self.db, 'day', 'R', 'U', 'R3', 2, 2).sequence, 2)
        self.assertEqual(local_head(self.db, 'day', 'R'), 2)

    def test_rejected_actions_do_not_advance_sequences(self):
        self.open_transfer_shift()
        self.seed_measurements()
        for kwargs in (
            dict(shift_id='day', station='R', transfer_id='H1', specimen_id='T', reading_id='L2'),
            dict(shift_id='day', station='L', transfer_id='H1', specimen_id='U', reading_id='R1'),
            dict(shift_id='day', station='L', transfer_id='H1', specimen_id='T', reading_id='R1'),
            dict(shift_id='ghost', station='L', transfer_id='H1', specimen_id='T', reading_id='L2'),
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(TransferRejected):
                    release(self.db, **kwargs)
        self.assertEqual(local_head(self.db, 'day', 'L'), 2)
        with self.assertRaises(TransferRejected):
            receive(self.db, 'day', 'L', 'H1')
        with self.assertRaises(TransferRejected):
            receive(self.db, 'day', 'R', 'H1')
        self.assertEqual(local_head(self.db, 'day', 'R'), 1)
        # Invalid identifiers are transfer rejections rather than silent successes.
        with self.assertRaises(TransferRejected):
            release(self.db, 'day', 'L', ' bad ', 'T', 'L2')
        with self.assertRaises(TransferRejected):
            receive(self.db, 'day', 'R', 'H\n1')

    def test_transfer_actions_share_the_local_sequence(self):
        self.open_transfer_shift()
        self.seed_measurements()
        self.assertEqual(release(self.db, 'day', 'L', 'H1', 'T', 'L2').sequence, 3)
        # A later left reading continues after the transfer position.
        self.assertEqual(ingest(self.db, 'day', 'R', 'U', 'R2', 2, 6).sequence, 2)
        self.assertEqual(receive(self.db, 'day', 'R', 'H1').sequence, 3)
        self.assertEqual(ingest(self.db, 'day', 'R', 'T', 'T-R', 6, 5).sequence, 4)
        self.assertEqual(ingest(self.db, 'day', 'R', 'U', 'R3', 0, 7).sequence, 5)
        self.assertEqual((local_head(self.db, 'day', 'L'), local_head(self.db, 'day', 'R')), (3, 5))


class TransferClosingTests(TransferCase):
    def test_cutoff_boundaries_around_release_and_receive(self):
        self.open_transfer_shift()
        self.seed_measurements()
        # Cutoff 0 stays legal on a station whose head only contains readings.
        empty_left = seal(self.db, 'day', 'L', 0)
        self.assertEqual([detail.count for detail in empty_left.details], [0])
        release(self.db, 'day', 'L', 'H1', 'T', 'L2')
        # The release position itself is a valid left cutoff.
        self.assertEqual(seal(self.db, 'day', 'L', 3).cutoff, 3)
        # Right cutoff 1 remains legal while the receive is still beyond it.
        early_right = seal(self.db, 'day', 'R', 1)
        receive(self.db, 'day', 'R', 'H1')
        # Cutoff exactly at the receive position admits the specimen with no measurements.
        boundary = seal(self.db, 'day', 'R', 2)
        t_detail = next(detail for detail in boundary.details if detail.specimen_id == 'T')
        self.assertEqual((t_detail.count, t_detail.status), (0, 'missing'))
        # A fresh empty-prefix seal still works after transfer activity; roster
        # specimens remain listed as missing and the transferred specimen is excluded.
        empty_right = seal(self.db, 'day', 'R', 0)
        self.assertEqual([detail.specimen_id for detail in empty_right.details], ['U'])
        self.assertTrue(all(detail.status == 'missing' for detail in empty_right.details))
        self.assertEqual(early_right.cutoff, 1)

    def test_transfer_positions_are_valid_cutoffs(self):
        self.open_transfer_shift()
        self.seed_measurements()
        release(self.db, 'day', 'L', 'H1', 'T', 'L2')
        left = seal(self.db, 'day', 'L', 3)
        self.assertEqual([d.count for d in left.details], [2])
        with self.assertRaises(SealRejected):
            seal(self.db, 'day', 'L', 4)
        receive(self.db, 'day', 'R', 'H1')
        right_two = seal(self.db, 'day', 'R', 2)
        specimens = {detail.specimen_id: detail for detail in right_two.details}
        self.assertEqual(set(specimens), {'U', 'T'})
        self.assertEqual((specimens['T'].count, specimens['T'].status, specimens['T'].readings),
                         (0, 'missing', ()))
        self.assertEqual(specimens['U'].count, 1)
        # The receive action itself is not counted as a measurement.
        ingest(self.db, 'day', 'R', 'T', 'T-R', 6, 5)
        right_three = seal(self.db, 'day', 'R', 3)
        t_detail = next(detail for detail in right_three.details if detail.specimen_id == 'T')
        self.assertEqual((t_detail.count, t_detail.maximum_um, t_detail.status),
                         (1, Fraction(11), 'over_limit'))
        with self.assertRaises(SealRejected):
            seal(self.db, 'day', 'R', 6)
        # Historical seals remain readable and unchanged after later activity.
        self.assertEqual(SealStore(self.db).get(left.seal_id), left)
        self.assertEqual(SealStore(self.db).get(right_two.seal_id), right_two)

    def test_publish_rejection_keeps_cutoffs_and_accounts(self):
        self.open_transfer_shift()
        self.seed_measurements()
        left_two = seal(self.db, 'day', 'L', 2)
        release(self.db, 'day', 'L', 'H1', 'T', 'L2')
        left_three = seal(self.db, 'day', 'L', 3)
        receive(self.db, 'day', 'R', 'H1')
        right_two = seal(self.db, 'day', 'R', 2)
        before = report_counts(self.db)
        with closing(connect(self.path)) as observer:
            with self.assertRaises(PublishRejected):
                publish(observer, left_two.seal_id, right_two.seal_id)
            self.assertEqual(report_counts(observer), before)
            self.assertFalse(observer.in_transaction)
        # The same seal pair is still usable once a matching left cutoff is supplied.
        report_id = publish(self.db, left_three.seal_id, right_two.seal_id)
        view = read(self.db, report_id)
        self.assertEqual(next(i for i in view.items if i.specimen_id == 'T').location, 'right')

    def test_commit_failure_leaves_no_new_account(self):
        self.open_transfer_shift()
        self.seed_measurements()
        left_two = seal(self.db, 'day', 'L', 2)
        right_one = seal(self.db, 'day', 'R', 1)
        old_id = publish(self.db, left_two.seal_id, right_one.seal_id)
        old_view = read(self.db, old_id)
        release(self.db, 'day', 'L', 'H1', 'T', 'L2')
        left_three = seal(self.db, 'day', 'L', 3)
        in_transit_seal_pair = (left_three.seal_id, right_one.seal_id)

        self.db.executescript((SAMPLES / 'reject_commit.sql').read_text(encoding='utf-8'))
        with closing(connect(self.path)) as observer:
            before = report_counts(observer)
            with self.assertRaises(PublishRejected) as failure:
                publish(self.db, *in_transit_seal_pair)
            self.assertIsInstance(failure.exception.__cause__, sqlite3.IntegrityError)
            self.assertEqual(failure.exception.__cause__.sqlite_errorname, 'SQLITE_CONSTRAINT_FOREIGNKEY')
            self.assertFalse(self.db.in_transaction)
            self.assertEqual(report_counts(observer), before)
            self.assertEqual(read(observer, old_id), old_view)
            self.assertEqual(SealStore(observer).get(left_three.seal_id), left_three)
            self.db.execute('DROP TRIGGER reject_report_commit')
        retry_id = publish(self.db, *in_transit_seal_pair)
        view = read(self.db, retry_id)
        self.assertEqual(next(i for i in view.items if i.specimen_id == 'T').location, 'in_transit')


class TransferReadbackTests(TransferCase):
    def _signed_reports(self):
        self.open_transfer_shift()
        self.seed_measurements()
        left_two = seal(self.db, 'day', 'L', 2)
        right_one = seal(self.db, 'day', 'R', 1)
        home_id = publish(self.db, left_two.seal_id, right_one.seal_id)
        release(self.db, 'day', 'L', 'H1', 'T', 'L2')
        left_three = seal(self.db, 'day', 'L', 3)
        transit_id = publish(self.db, left_three.seal_id, right_one.seal_id)
        receive(self.db, 'day', 'R', 'H1')
        right_two = seal(self.db, 'day', 'R', 2)
        right_id = publish(self.db, left_three.seal_id, right_two.seal_id)
        return home_id, transit_id, right_id

    def test_locations_and_handover_provenance(self):
        home_id, transit_id, right_id = self._signed_reports()
        home = read(self.db, home_id)
        transit = read(self.db, transit_id)
        right = read(self.db, right_id)

        home_t = next(i for i in home.items if i.specimen_id == 'T')
        self.assertEqual((home_t.location, home_t.handover), ('left', None))
        transit_t = next(i for i in transit.items if i.specimen_id == 'T')
        self.assertEqual(transit_t.location, 'in_transit')
        self.assertEqual(transit_t.station, 'L')
        self.assertEqual((transit_t.count, transit_t.maximum_um), (2, Fraction(16)))
        right_t = next(i for i in right.items if i.specimen_id == 'T')
        self.assertEqual((right_t.location, right_t.station, right_t.count), ('right', 'R', 0))

        for view in (transit_t, right_t):
            origin = view.handover.origin
            self.assertEqual((origin.reading_id, origin.sequence, origin.raw_code, origin.thickness_um,
                              origin.source_line, origin.profile_version, origin.source_station),
                             ('L2', 2, Fraction(4), Fraction(7), 4, 1, 'L'))
            self.assertEqual(view.handover.transfer_id, 'H1')
            self.assertEqual(view.handover.release_sequence, 3)
        self.assertEqual(transit_t.handover.receive_sequence, None)
        self.assertEqual(right_t.handover.receive_sequence, 2)
        # Every specimen appears exactly once, in frozen roster order.
        self.assertEqual([item.specimen_id for item in transit.items], ['U', 'T'])
        self.assertEqual([item.position for item in transit.items], [0, 1])

    def test_history_is_immutable_after_later_activity(self):
        home_id, transit_id, right_id = self._signed_reports()
        snapshots = {rid: read(self.db, rid) for rid in (home_id, transit_id, right_id)}
        # More captures and another seal cannot alter already issued accounts.
        ingest(self.db, 'day', 'R', 'T', 'T-R', 6, 5)
        later_right = seal(self.db, 'day', 'R', 3)
        latest_id = publish(self.db,
                            snapshots[transit_id].left_seal_id, later_right.seal_id)
        for rid, snapshot in snapshots.items():
            self.assertEqual(read(self.db, rid), snapshot)
        latest = next(i for i in read(self.db, latest_id).items if i.specimen_id == 'T')
        self.assertEqual((latest.location, latest.count, latest.maximum_um, latest.status),
                         ('right', 1, Fraction(11), 'over_limit'))

    def test_reopen_claims_fixed_locations_and_origin_evidence(self):
        home_id, transit_id, right_id = self._signed_reports()
        expected = {
            rid: tuple((item.specimen_id, item.location,
                        None if item.handover is None else (
                            item.handover.transfer_id, item.handover.release_sequence,
                            item.handover.receive_sequence, item.handover.origin.reading_id,
                            str(item.handover.origin.raw_code), str(item.handover.origin.thickness_um),
                            item.handover.origin.source_line, item.handover.origin.source_station))
                       for item in read(self.db, rid).items)
            for rid in (home_id, transit_id, right_id)
        }
        self.db.close()
        with closing(connect(self.path)) as reopened:
            for rid, rows in expected.items():
                view = read(reopened, rid)
                actual = tuple((item.specimen_id, item.location,
                                None if item.handover is None else (
                                    item.handover.transfer_id, item.handover.release_sequence,
                                    item.handover.receive_sequence, item.handover.origin.reading_id,
                                    str(item.handover.origin.raw_code),
                                    str(item.handover.origin.thickness_um),
                                    item.handover.origin.source_line,
                                    item.handover.origin.source_station))
                               for item in view.items)
                self.assertEqual(actual, rows)
            with self.assertRaises(ReportNotFound):
                read(reopened, 99999)

    def test_initialize_is_repeatable_on_transfer_schema(self):
        self._signed_reports()
        initialize(self.db)
        initialize(self.db)
        record = TransferJournal(self.db).list_for_shift('day')[0]
        self.assertEqual((record.transfer_id, record.release_sequence, record.receive_sequence),
                         ('H1', 3, 2))


class LegacySchemaTests(LineCase):
    def test_pre_feature_database_migrates_and_keeps_old_accounts(self):
        # Rebuild the pre-transfer report_items shape, then reinitialize against it.
        self.db.execute('ALTER TABLE report_items RENAME TO report_items_legacy')
        self.db.execute('''
            CREATE TABLE report_items (
                report_id INTEGER NOT NULL, position INTEGER NOT NULL, seal_id INTEGER NOT NULL,
                PRIMARY KEY (report_id, position)
            )
        ''')
        self.db.execute('INSERT INTO report_items SELECT report_id, position, seal_id FROM report_items_legacy')
        self.db.execute('DROP TABLE report_items_legacy')
        self.db.execute('DROP TABLE IF EXISTS report_handovers')
        self.db.execute('DROP TABLE IF EXISTS transfers')
        for index in ('idx_transfers_receive_seq', 'idx_transfers_release_seq'):
            self.db.execute(f'DROP INDEX IF EXISTS {index}')

        # Reinitialize first: a real pre-feature database has neither new table.
        initialize(self.db)
        self.open_day()
        self.capture('L-A', 'L1', 0)
        left = seal(self.db, 'day', 'L', 1)
        right = seal(self.db, 'day', 'R', 0)
        old_id = publish(self.db, left.seal_id, right.seal_id)

        initialize(self.db)
        initialize(self.db)
        view = read(self.db, old_id)
        self.assertEqual([(item.specimen_id, item.location, item.handover) for item in view.items],
                         [('R-A', 'right', None), ('L-A', 'left', None),
                          ('R-B', 'right', None), ('L-B', 'left', None)])
        # New transfer workflow remains available after the migration.
        from line.capture.admission import receive, release

        released = release(self.db, 'day', 'L', 'H1', 'L-A', 'L1')
        self.assertEqual(released.sequence, 2)
        self.assertEqual(receive(self.db, 'day', 'R', 'H1').sequence, 1)
        left_two = seal(self.db, 'day', 'L', 2)
        right_one = seal(self.db, 'day', 'R', 1)
        new_id = publish(self.db, left_two.seal_id, right_one.seal_id)
        moved = next(item for item in read(self.db, new_id).items if item.specimen_id == 'L-A')
        self.assertEqual((moved.location, moved.handover.transfer_id), ('right', 'H1'))
        self.assertEqual(read(self.db, old_id), view)
