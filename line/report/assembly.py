from dataclasses import dataclass

from line.closing.store import StationSeal
from line.capture.transfers import TransferJournal


@dataclass(frozen=True)
class HandoverDraft:
    transfer_id: str
    release_sequence: int
    receive_sequence: int | None
    reading_id: str


@dataclass(frozen=True)
class ReportItemRef:
    position: int
    seal_id: int
    location: str
    handover: HandoverDraft | None


@dataclass(frozen=True)
class ReportDraft:
    shift_id: str
    left_seal_id: int
    right_seal_id: int
    items: tuple[ReportItemRef, ...]


class InvalidSealPair(ValueError):
    pass


def _details_by_position(seal: StationSeal):
    return {detail.position: detail for detail in seal.details}


def assemble_report(db, first: StationSeal | None, second: StationSeal | None):
    if first is None or second is None:
        raise InvalidSealPair('both parent seals must exist')
    if first.shift_id != second.shift_id:
        raise InvalidSealPair('parent seals belong to different shifts')
    if {first.side, second.side} != {'left', 'right'} or first.station == second.station:
        raise InvalidSealPair('one seal from each station is required')

    left, right = (first, second) if first.side == 'left' else (second, first)
    left_details = _details_by_position(left)
    right_details = _details_by_position(right)
    positions = sorted(set(left_details) | set(right_details))

    transfers = TransferJournal(db).list_for_shift(left.shift_id)
    by_specimen = {record.specimen_id: record for record in transfers}

    refs = []
    for position in positions:
        left_detail = left_details.get(position)
        right_detail = right_details.get(position)
        specimen_id = (left_detail or right_detail).specimen_id
        record = by_specimen.get(specimen_id)

        release_included = (
            record is not None and record.release_sequence is not None
            and record.release_sequence <= left.cutoff
        )
        receive_included = (
            record is not None and record.receive_sequence is not None
            and record.receive_sequence <= right.cutoff
        )
        if receive_included and not release_included:
            raise InvalidSealPair(
                f'transfer {record.transfer_id!r} is received within the right cut but its '
                'release is outside the left cut')

        if receive_included:
            location = 'right'
            seal_id = right.seal_id
        elif release_included:
            location = 'in_transit'
            seal_id = left.seal_id
        elif right_detail is not None and left_detail is None:
            location = 'right'
            seal_id = right.seal_id
        else:
            location = 'left'
            seal_id = left.seal_id

        handover = None
        if release_included:
            handover = HandoverDraft(
                record.transfer_id, record.release_sequence,
                record.receive_sequence if receive_included else None,
                record.reading_id,
            )
        refs.append(ReportItemRef(position, seal_id, location, handover))
    return ReportDraft(left.shift_id, left.seal_id, right.seal_id, tuple(refs))
