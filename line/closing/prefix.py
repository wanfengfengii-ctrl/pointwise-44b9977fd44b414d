from dataclasses import dataclass

from line.calibration.profiles import GaugeProfile
from line.calibration.revisions import ProfileRevisions
from line.capture.journal import AcceptedReading, ReadingJournal
from line.capture.transfers import TransferJournal, TransferRecord, local_head
from line.intake.registry import ShiftRegistry
from line.intake.roster import RosterItem
from line.intake.stations import valid_identifier


class InvalidCut(ValueError):
    pass


@dataclass(frozen=True)
class SealPrefix:
    shift_id: str
    station: str
    side: str
    cutoff: int
    profile_version: int
    profile: GaugeProfile
    roster_items: tuple[RosterItem, ...]
    readings: tuple[AcceptedReading, ...]
    received: tuple[TransferRecord, ...] = ()


def load_prefix(db, shift_id, station, cutoff):
    if not valid_identifier(shift_id) or not valid_identifier(station):
        raise InvalidCut('a valid shift and station are required')
    if type(cutoff) is not int or cutoff < 0:
        raise InvalidCut('cutoff must be a nonnegative integer')

    shift = ShiftRegistry(db).get_opened_shift(shift_id)
    if shift is None:
        raise InvalidCut(f'shift {shift_id!r} is not open')
    try:
        side = shift.stations.side_of(station)
    except ValueError as exc:
        raise InvalidCut(str(exc)) from exc

    head = local_head(db, shift_id, station)
    if cutoff > head:
        raise InvalidCut(f'cutoff {cutoff} exceeds local head {head}')
    version = shift.version_for(station)
    profile = ProfileRevisions(db).get_version(version)
    roster_items = shift.roster.items_for(station)
    journal = ReadingJournal(db)
    readings = journal.list_through(shift_id, station, cutoff)
    received = ()
    if side == 'right':
        received = TransferJournal(db).list_received_through(shift_id, station, cutoff)
        known = {item.specimen_id for item in roster_items}
        received_items = tuple(
            RosterItem(_position_of(shift, record.specimen_id), record.specimen_id, shift.stations.left)
            for record in received if record.specimen_id not in known
        )
        roster_items = tuple(roster_items) + received_items
    return SealPrefix(shift_id, station, side, cutoff, version, profile,
                      roster_items, readings, received)


def _position_of(shift, specimen_id):
    return next(item.position for item in shift.roster.items if item.specimen_id == specimen_id)
