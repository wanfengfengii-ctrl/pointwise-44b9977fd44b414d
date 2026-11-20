from dataclasses import dataclass
from fractions import Fraction

from line.calibration.profiles import GaugeProfile
from line.capture.journal import ReadingJournal
from line.capture.identity import ReadingKey
from line.closing.store import SealStore
from line.report.assembly import ReportDraft


@dataclass(frozen=True)
class ReadingEvidence:
    reading_id: str
    sequence: int
    raw_code: Fraction
    thickness_um: Fraction
    source_line: int
    profile_version: int


@dataclass(frozen=True)
class HandoverOrigin:
    reading_id: str
    sequence: int
    raw_code: Fraction
    thickness_um: Fraction
    source_line: int
    profile_version: int
    source_station: str


@dataclass(frozen=True)
class HandoverEvidence:
    transfer_id: str
    release_sequence: int
    receive_sequence: int | None
    origin: HandoverOrigin


@dataclass(frozen=True)
class SpecimenEvidence:
    position: int
    specimen_id: str
    station: str
    seal_id: int
    profile_version: int
    profile: GaugeProfile
    count: int
    maximum_um: Fraction | None
    status: str
    readings: tuple[ReadingEvidence, ...]
    location: str
    handover: HandoverEvidence | None


def _reading_evidence(row):
    return ReadingEvidence(
        row.claim.key.reading_id, row.sequence, row.claim.raw_code, row.thickness_um,
        row.claim.source_line, row.profile_version,
    )


def resolve_report_evidence(db, draft: ReportDraft):
    store = SealStore(db)
    journal = ReadingJournal(db)
    detail_refs = {}
    for seal_id in (draft.left_seal_id, draft.right_seal_id):
        parent = store.get(seal_id)
        if parent is None:
            raise LookupError(f'fixed parent seal {seal_id} is unavailable')
        for detail in parent.details:
            detail_refs[seal_id, detail.position] = (parent, detail)

    evidence = []
    for ref in draft.items:
        parent, detail = detail_refs[ref.seal_id, ref.position]
        readings = tuple(_reading_evidence(row) for row in detail.readings)
        handover = None
        if ref.handover is not None:
            origin_reading = journal.get_reading(
                ReadingKey(draft.shift_id, ref.handover.reading_id))
            if origin_reading is None:
                raise LookupError(
                    f'handover {ref.handover.transfer_id!r} references missing reading '
                    f'{ref.handover.reading_id!r}')
            origin = HandoverOrigin(
                origin_reading.claim.key.reading_id, origin_reading.sequence,
                origin_reading.claim.raw_code, origin_reading.thickness_um,
                origin_reading.claim.source_line, origin_reading.profile_version,
                origin_reading.claim.station,
            )
            handover = HandoverEvidence(
                ref.handover.transfer_id, ref.handover.release_sequence,
                ref.handover.receive_sequence, origin,
            )
        evidence.append(SpecimenEvidence(
            detail.position, detail.specimen_id, parent.station, ref.seal_id, parent.profile_version,
            parent.profile, detail.count, detail.maximum_um, detail.status, readings,
            ref.location, handover,
        ))
    return tuple(evidence)
