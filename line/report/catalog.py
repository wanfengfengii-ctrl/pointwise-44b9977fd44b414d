from line.report.assembly import HandoverDraft, ReportDraft, ReportItemRef
from line.workday import valid_database_id


class ReportCatalog:
    def __init__(self, db):
        self.db = db

    def save(self, draft: ReportDraft):
        cursor = self.db.execute('''
            INSERT INTO reports(shift_id, left_seal_id, right_seal_id) VALUES (?, ?, ?)
        ''', (draft.shift_id, draft.left_seal_id, draft.right_seal_id))
        report_id = cursor.lastrowid
        self.db.executemany('''
            INSERT INTO report_items(report_id, position, seal_id, location, handover_id)
            VALUES (?, ?, ?, ?, ?)
        ''', ((report_id, item.position, item.seal_id, item.location,
               item.handover.transfer_id if item.handover is not None else None)
              for item in draft.items))
        for item in draft.items:
            if item.handover is None:
                continue
            self.db.execute('''
                INSERT INTO report_handovers(report_id, position, transfer_id, release_sequence,
                                             receive_sequence, reading_id)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (report_id, item.position, item.handover.transfer_id, item.handover.release_sequence,
                  item.handover.receive_sequence, item.handover.reading_id))
        return report_id

    def get(self, report_id):
        if not valid_database_id(report_id):
            return None
        row = self.db.execute('''
            SELECT shift_id, left_seal_id, right_seal_id FROM reports WHERE report_id = ?
        ''', (report_id,)).fetchone()
        if row is None:
            return None
        handovers = {
            position: HandoverDraft(transfer_id, release_sequence, receive_sequence, reading_id)
            for position, transfer_id, release_sequence, receive_sequence, reading_id in self.db.execute('''
                SELECT position, transfer_id, release_sequence, receive_sequence, reading_id
                FROM report_handovers WHERE report_id = ?
            ''', (report_id,))
        }
        refs = self.db.execute('''
            SELECT position, seal_id, location, handover_id FROM report_items
            WHERE report_id = ? ORDER BY position
        ''', (report_id,))
        items = tuple(
            ReportItemRef(position, seal_id, location,
                          handovers.get(position) if handover_id is not None else None)
            for position, seal_id, location, handover_id in refs
        )
        return ReportDraft(*row, items)


def _has_column(db, table, column):
    return any(info[1] == column for info in db.execute(f'PRAGMA table_info({table})').fetchall())


def initialize_reports(db):
    db.execute('''
        CREATE TABLE IF NOT EXISTS reports (
            report_id INTEGER PRIMARY KEY,
            shift_id TEXT NOT NULL REFERENCES shifts(shift_id),
            left_seal_id INTEGER NOT NULL REFERENCES station_seals(seal_id),
            right_seal_id INTEGER NOT NULL REFERENCES station_seals(seal_id),
            CHECK (left_seal_id <> right_seal_id)
        )
    ''')
    db.execute('''
        CREATE TABLE IF NOT EXISTS report_items (
            report_id INTEGER NOT NULL REFERENCES reports(report_id),
            position INTEGER NOT NULL,
            seal_id INTEGER NOT NULL,
            PRIMARY KEY (report_id, position),
            FOREIGN KEY (seal_id, position) REFERENCES seal_details(seal_id, position)
        )
    ''')
    db.execute('''
        CREATE TABLE IF NOT EXISTS report_handovers (
            report_id INTEGER NOT NULL REFERENCES reports(report_id),
            position INTEGER NOT NULL,
            transfer_id TEXT NOT NULL,
            release_sequence INTEGER NOT NULL CHECK (release_sequence > 0),
            receive_sequence INTEGER CHECK (receive_sequence IS NULL OR receive_sequence > 0),
            reading_id TEXT NOT NULL,
            PRIMARY KEY (report_id, position)
        )
    ''')
    if not _has_column(db, 'report_items', 'location'):
        db.execute('ALTER TABLE report_items ADD COLUMN location TEXT')
        db.execute('''
            UPDATE report_items
            SET location = (
                SELECT side FROM station_seals WHERE station_seals.seal_id = report_items.seal_id
            )
        ''')
    if not _has_column(db, 'report_items', 'handover_id'):
        db.execute('ALTER TABLE report_items ADD COLUMN handover_id TEXT')
