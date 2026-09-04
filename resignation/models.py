import json
from datetime import datetime

from extensions import db


class ResignationRequest(db.Model):
    __tablename__ = 'resignation_request'
    __table_args__ = (
        db.Index(
            'ix_resignation_user_status_created',
            'user_id', 'status', 'created_at'
        ),
        db.Index(
            'ix_resignation_supervisor_status',
            'supervisor_id', 'status'
        ),
        db.Index(
            'ix_resignation_status_submitted',
            'status', 'submitted_at'
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    reference_no = db.Column(db.String(40), unique=True, index=True)
    user_id = db.Column(db.Integer, nullable=False, index=True)

    # Bagian A - seluruh data diisi sendiri oleh karyawan dan disimpan
    # sebagai snapshot. Perubahan akun tidak mengubah surat yang sudah dibuat.
    employee_name = db.Column(db.String(150))
    employee_nik = db.Column(db.String(80))
    position = db.Column(db.String(120))
    employee_division = db.Column(db.String(100))
    supervisor_id = db.Column(db.Integer, index=True)
    supervisor_name = db.Column(db.String(150))
    employment_status = db.Column(db.String(20))  # pkwtt / pkwt
    start_date = db.Column(db.Date)

    # Bagian B
    submission_date = db.Column(db.Date)
    effective_date = db.Column(db.Date)
    notice_days = db.Column(db.Integer)
    short_notice_reason = db.Column(db.Text)

    # Bagian C. Disimpan sebagai JSON text agar tetap kompatibel dengan
    # PostgreSQL dan database SQLite yang dipakai saat pengujian lokal.
    reason_codes_json = db.Column(db.Text, default='[]')
    reason_other = db.Column(db.Text)

    # Bagian D
    commitment_accepted = db.Column(db.Boolean, default=False, nullable=False)
    no_service_bond_confirmed = db.Column(
        db.Boolean, default=False, nullable=False
    )

    # Bagian E
    correspondence_address = db.Column(db.Text)
    phone_number = db.Column(db.String(40))
    personal_email = db.Column(db.String(160))
    bank_name = db.Column(db.String(100))
    bank_account_number = db.Column(db.String(100))
    bank_account_holder = db.Column(db.String(150))

    # Bagian F dan alur approval
    declaration_accepted = db.Column(db.Boolean, default=False, nullable=False)
    status = db.Column(db.String(40), default='draft', nullable=False)
    submitted_at = db.Column(db.DateTime)

    supervisor_decision = db.Column(db.String(30))
    supervisor_by = db.Column(db.Integer)
    supervisor_at = db.Column(db.DateTime)
    supervisor_note = db.Column(db.Text)

    hrd_decision = db.Column(db.String(30))
    hrd_by = db.Column(db.Integer)
    hrd_at = db.Column(db.DateTime)
    hrd_note = db.Column(db.Text)

    final_document_path = db.Column(db.String(500))
    final_document_name = db.Column(db.String(255))

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(
        db.DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )

    @property
    def reason_codes(self):
        try:
            data = json.loads(self.reason_codes_json or '[]')
            return data if isinstance(data, list) else []
        except (TypeError, ValueError, json.JSONDecodeError):
            return []

    @reason_codes.setter
    def reason_codes(self, values):
        clean_values = []
        for value in values or []:
            value = str(value).strip()
            if value and value not in clean_values:
                clean_values.append(value)
        self.reason_codes_json = json.dumps(clean_values)


class ResignationAuditLog(db.Model):
    __tablename__ = 'resignation_audit_log'
    __table_args__ = (
        db.Index(
            'ix_resignation_audit_request_created',
            'request_id', 'created_at'
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    request_id = db.Column(db.Integer, nullable=False, index=True)
    actor_id = db.Column(db.Integer)
    actor_name = db.Column(db.String(150), nullable=False)
    action = db.Column(db.String(60), nullable=False)
    from_status = db.Column(db.String(40))
    to_status = db.Column(db.String(40))
    note = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
