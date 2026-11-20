from dataclasses import dataclass

from line.intake.stations import valid_identifier


@dataclass(frozen=True)
class TransferRecord:
    shift_id: str
    transfer_id: str
    specimen_id: str
    reading_id: str
    release_station: str
    receive_station: str
    release_sequence: int
    receive_sequence: int | None


class TransferRejected(ValueError):
    pass


def local_head(db, shift_id, station):
    """Highest local sequence occupied by readings, releases and receives."""
    row = db.execute(
        '''
        SELECT COALESCE(MAX(seq), 0) FROM (
            SELECT seq FROM readings WHERE shift_id = ? AND station = ?
            UNION ALL
            SELECT release_sequence AS seq FROM transfers
            WHERE shift_id = ? AND release_station = ?
            UNION ALL
            SELECT receive_sequence AS seq FROM transfers
            WHERE shift_id = ? AND receive_station = ?
        )
        ''',
        (shift_id, station, shift_id, station, shift_id, station),
    ).fetchone()
    return row[0]


class TransferJournal:
    def __init__(self, db):
        self.db = db

    def get(self, shift_id, transfer_id):
        row = self.db.execute(
            '''
            SELECT shift_id, transfer_id, specimen_id, reading_id, release_station,
                   receive_station, release_sequence, receive_sequence
            FROM transfers WHERE shift_id = ? AND transfer_id = ?
            ''',
            (shift_id, transfer_id),
        ).fetchone()
        return None if row is None else TransferRecord(*row)

    def list_for_shift(self, shift_id):
        rows = self.db.execute(
            '''
            SELECT shift_id, transfer_id, specimen_id, reading_id, release_station,
                   receive_station, release_sequence, receive_sequence
            FROM transfers WHERE shift_id = ? ORDER BY transfer_id
            ''',
            (shift_id,),
        )
        return tuple(TransferRecord(*row) for row in rows)

    def released_specimen(self, shift_id, specimen_id):
        """The transfer record on whose behalf the specimen left the left station, if any."""
        row = self.db.execute(
            'SELECT shift_id, transfer_id, specimen_id, reading_id, release_station, '
            'receive_station, release_sequence, receive_sequence '
            'FROM transfers WHERE shift_id = ? AND specimen_id = ?',
            (shift_id, specimen_id),
        ).fetchone()
        return None if row is None else TransferRecord(*row)

    def save_release(self, record: TransferRecord):
        self.db.execute(
            '''
            INSERT INTO transfers(shift_id, transfer_id, specimen_id, reading_id,
                                  release_station, receive_station, release_sequence, receive_sequence)
            VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
            ''',
            (record.shift_id, record.transfer_id, record.specimen_id, record.reading_id,
             record.release_station, record.receive_station, record.release_sequence),
        )

    def save_receive(self, shift_id, transfer_id, sequence):
        self.db.execute(
            'UPDATE transfers SET receive_sequence = ? WHERE shift_id = ? AND transfer_id = ?',
            (sequence, shift_id, transfer_id),
        )

    def list_received_through(self, shift_id, station, cutoff):
        rows = self.db.execute(
            '''
            SELECT shift_id, transfer_id, specimen_id, reading_id, release_station,
                   receive_station, release_sequence, receive_sequence
            FROM transfers
            WHERE shift_id = ? AND receive_station = ? AND receive_sequence <= ?
            ORDER BY receive_sequence
            ''',
            (shift_id, station, cutoff),
        )
        return tuple(TransferRecord(*row) for row in rows)


def validate_transfer_identifier(value):
    if not valid_identifier(value):
        raise TransferRejected('a valid transfer identifier is required')


def initialize_transfers(db):
    db.execute('''
        CREATE TABLE IF NOT EXISTS transfers (
            shift_id TEXT NOT NULL,
            transfer_id TEXT NOT NULL,
            specimen_id TEXT NOT NULL,
            reading_id TEXT NOT NULL,
            release_station TEXT NOT NULL,
            receive_station TEXT NOT NULL,
            release_sequence INTEGER NOT NULL CHECK (release_sequence > 0),
            receive_sequence INTEGER CHECK (receive_sequence IS NULL OR receive_sequence > 0),
            PRIMARY KEY (shift_id, transfer_id),
            UNIQUE (shift_id, specimen_id),
            UNIQUE (shift_id, release_station, release_sequence),
            UNIQUE (shift_id, receive_station, receive_sequence),
            FOREIGN KEY (shift_id, specimen_id) REFERENCES roster_items(shift_id, specimen_id),
            FOREIGN KEY (shift_id, reading_id) REFERENCES readings(shift_id, reading_id)
        )
    ''')
    # Older SQLite builds treat NULL receive sequences as equal inside a UNIQUE clause;
    # a partial index enforces the live-sequence constraint without that ambiguity.
    db.execute('''
        CREATE UNIQUE INDEX IF NOT EXISTS idx_transfers_receive_seq
        ON transfers(shift_id, receive_station, receive_sequence)
        WHERE receive_sequence IS NOT NULL
    ''')
    db.execute('''
        CREATE UNIQUE INDEX IF NOT EXISTS idx_transfers_release_seq
        ON transfers(shift_id, release_station, release_sequence)
    ''')
