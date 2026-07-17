from extensions import db
from datetime import datetime


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)

    username = db.Column(db.String(100), unique=True, nullable=False)
    nama_lengkap = db.Column(db.String(150))

    tempat_lahir = db.Column(db.String(100))
    tanggal_lahir = db.Column(db.Date)
    join_date = db.Column(db.Date)

    password = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(50), default='karyawan')
    divisi = db.Column(db.String(50))

    # Tetap dipakai untuk saldo/jatah cuti yang bisa diedit HRD/admin
    kuota_cuti = db.Column(db.Integer, default=0)

    # Dipakai agar sistem tahu kapan terakhir kali menambah +1 otomatis
    last_cuti_accrual_date = db.Column(db.Date)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class LeaveRequest(db.Model):
    id = db.Column(db.Integer, primary_key=True)

    user_id = db.Column(db.Integer)

    jenis_izin = db.Column(db.String(50))
    tanggal_mulai = db.Column(db.Date)
    tanggal_selesai = db.Column(db.Date)
    durasi = db.Column(db.Integer)
    alasan = db.Column(db.Text)

    file_surat = db.Column(db.String(255))
    file_chat = db.Column(db.String(255))

    status = db.Column(db.String(50), default='pending')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)