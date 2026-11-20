from line.calibration.conversion import RawCodeRejected, convert_raw
from line.calibration.revisions import ProfileRevisions
from line.capture.identity import ReadingKey, make_reading_claim
from line.capture.journal import ReadingConflict, ReadingJournal
from line.capture.receipts import TransferReceipt, receipt_from_reading
from line.capture.recording import iter_recorded_readings
from line.capture.transfers import (
    TransferJournal,
    TransferRecord,
    TransferRejected,
    local_head,
    validate_transfer_identifier,
)
from line.intake.registry import ShiftRegistry
from line.intake.stations import valid_identifier
from line.workday import write_transaction


class IngestRejected(ValueError):
    pass


def _opened_shift_or_raise(db, shift_id, error_type):
    shift = ShiftRegistry(db).get_opened_shift(shift_id)
    if shift is None:
        raise error_type(f'shift {shift_id!r} is not open')
    return shift


def _side_or_raise(shift, station, error_type):
    try:
        return shift.stations.side_of(station)
    except ValueError as exc:
        raise error_type(str(exc)) from exc


def ingest(db, shift_id, station, specimen_id, reading_id, raw_code, source_line):
    try:
        claim = make_reading_claim(shift_id, station, specimen_id, reading_id, raw_code, source_line)
    except ValueError as exc:
        raise IngestRejected('invalid measurement input') from exc

    with write_transaction(db):
        shift = _opened_shift_or_raise(db, shift_id, IngestRejected)
        journal = ReadingJournal(db)
        original = journal.get_reading(claim.key)
        if original is not None:
            # An accepted reading is reclaimed with its original receipt; replays do not
            # advance the sequence and remain valid while a transfer is in progress.
            if not original.claim.matches(claim):
                raise IngestRejected(
                    f'reading {claim.key.reading_id!r} has different original parameters')
            return receipt_from_reading(original)

        transfer = TransferJournal(db).released_specimen(shift_id, specimen_id)
        if shift.roster.station_for(specimen_id) != station:
            if transfer is None or transfer.receive_sequence is None or transfer.receive_station != station:
                raise IngestRejected(f'specimen {specimen_id!r} is not assigned to {station!r}')
        elif transfer is not None:
            # Between release and receive neither station may add readings; after
            # receive the left station can no longer collect the specimen.
            raise IngestRejected(
                f'specimen {specimen_id!r} has been released to {transfer.receive_station!r}')
        version = shift.version_for(station)
        profile = ProfileRevisions(db).get_version(version)
        try:
            thickness = convert_raw(profile, claim.raw_code)
            accepted = journal.accept(claim, thickness, version)
        except (RawCodeRejected, ReadingConflict) as exc:
            raise IngestRejected(str(exc)) from exc
        return receipt_from_reading(accepted)


def ingest_recording(db, shift_id, station, source):
    receipts = []
    for row in iter_recorded_readings(source):
        receipts.append(ingest(db, shift_id, station, row.specimen_id, row.reading_id,
                               row.raw_code, row.source_line))
    return tuple(receipts)


def release(db, shift_id, station, transfer_id, specimen_id, reading_id):
    if not all(valid_identifier(value) for value in (shift_id, station, specimen_id, reading_id)):
        raise TransferRejected('shift, station, specimen and reading identifiers are required')
    validate_transfer_identifier(transfer_id)

    with write_transaction(db):
        shift = _opened_shift_or_raise(db, shift_id, TransferRejected)
        side = _side_or_raise(shift, station, TransferRejected)
        if side != 'left':
            raise TransferRejected('only the left station can release specimens')
        if shift.roster.station_for(specimen_id) != station:
            raise TransferRejected(f'specimen {specimen_id!r} is not registered to {station!r}')

        journal = TransferJournal(db)
        existing = journal.get(shift_id, transfer_id)
        if existing is not None:
            # Same-action replay returns the original receipt without advancing the sequence.
            if (existing.release_station == station and existing.specimen_id == specimen_id
                    and existing.reading_id == reading_id):
                return TransferReceipt(transfer_id, existing.release_sequence)
            raise TransferRejected(f'transfer {transfer_id!r} is already bound to different parameters')

        if journal.released_specimen(shift_id, specimen_id) is not None:
            raise TransferRejected(f'specimen {specimen_id!r} can only be transferred once per shift')

        reading = ReadingJournal(db).get_reading(ReadingKey(shift_id, reading_id))
        if reading is None or reading.claim.station != station or reading.claim.specimen_id != specimen_id:
            raise TransferRejected(
                f'reading {reading_id!r} is not an accepted left-station reading of {specimen_id!r}')

        sequence = local_head(db, shift_id, station) + 1
        journal.save_release(TransferRecord(
            shift_id, transfer_id, specimen_id, reading_id, station, shift.stations.right,
            sequence, None))
        return TransferReceipt(transfer_id, sequence)


def receive(db, shift_id, station, transfer_id):
    if not all(valid_identifier(value) for value in (shift_id, station, transfer_id)):
        raise TransferRejected('shift, station and transfer identifiers are required')

    with write_transaction(db):
        shift = _opened_shift_or_raise(db, shift_id, TransferRejected)
        side = _side_or_raise(shift, station, TransferRejected)
        if side != 'right':
            raise TransferRejected('only the right station can receive specimens')

        journal = TransferJournal(db)
        record = journal.get(shift_id, transfer_id)
        if record is None:
            raise TransferRejected(f'unknown transfer {transfer_id!r}')
        if record.release_station != shift.stations.left or record.receive_station != station:
            raise TransferRejected(f'transfer {transfer_id!r} is not bound to station {station!r}')
        if record.receive_sequence is not None:
            # Replaying the original receive action reclaims its original receipt.
            return TransferReceipt(transfer_id, record.receive_sequence)

        sequence = local_head(db, shift_id, station) + 1
        journal.save_receive(shift_id, transfer_id, sequence)
        return TransferReceipt(transfer_id, sequence)
