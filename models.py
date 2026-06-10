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
    kuota_cuti = db.Column(db.Integer, default=12)
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


class PurchaseOrderRequest(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, nullable=False)

    reason = db.Column(db.Text, nullable=False)
    total_amount = db.Column(db.Integer, default=0)

    status = db.Column(db.String(50), default='submitted')
    # submitted / approved / rejected / ordered

    order_proof = db.Column(db.String(255))

    accounting_approved_at = db.Column(db.DateTime)
    accounting_rejected_at = db.Column(db.DateTime)

    director_approved_at = db.Column(db.DateTime)
    director_rejected_at = db.Column(db.DateTime)

    reject_reason = db.Column(db.Text)

    approved_at = db.Column(db.DateTime)
    rejected_at = db.Column(db.DateTime)
    ordered_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    items = db.relationship(
        'PurchaseOrderItem',
        backref='po_request',
        lazy=True,
        cascade='all, delete-orphan'
    )


class PurchaseOrderItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)

    po_id = db.Column(
        db.Integer,
        db.ForeignKey('purchase_order_request.id'),
        nullable=False
    )

    item_name = db.Column(db.String(200), nullable=False)
    estimated_price = db.Column(db.Integer, default=0)
    qty = db.Column(db.Integer, default=1)
    note = db.Column(db.Text)