from datetime import datetime
from extensions import db


class OvertimeRequest(db.Model):
    __tablename__ = 'overtime_request'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, nullable=False)

    # Snapshot data karyawan saat pengajuan dibuat.
    # Ini sengaja disimpan supaya data historis Excel tidak berubah
    # ketika nama/divisi user diubah di Manajemen User.
    employee_name = db.Column(db.String(150), nullable=False)
    employee_divisi = db.Column(db.String(80), nullable=False)

    overtime_date = db.Column(db.Date, nullable=False)
    start_time = db.Column(db.Time, nullable=False)
    end_time = db.Column(db.Time, nullable=False)
    duration_minutes = db.Column(db.Integer, default=0, nullable=False)

    supervisor_name = db.Column(db.String(150))
    work_description = db.Column(db.Text, nullable=False)

    # Bukti wajib
    chat_proof = db.Column(db.String(500), nullable=False)
    overtime_photo = db.Column(db.String(500), nullable=False)
    attendance_photo = db.Column(db.String(500), nullable=False)

    status = db.Column(db.String(50), default='pending', nullable=False)
    # pending / approved / rejected

    approved_by = db.Column(db.Integer)
    approved_at = db.Column(db.DateTime)
    rejected_by = db.Column(db.Integer)
    rejected_at = db.Column(db.DateTime)
    reject_reason = db.Column(db.Text)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
