from datetime import datetime
from extensions import db

class ReimburseRequest(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, nullable=False)
    total_amount = db.Column(db.Integer, default=0)
    receipt_photo = db.Column(db.String(255))          # foto nota (camscanner)
    status = db.Column(db.String(50), default='submitted')  # submitted / paid / archived
    payment_proof = db.Column(db.String(255))          # bukti bayar direktur
    paid_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    items = db.relationship('ReimburseItem', backref='request', lazy=True, cascade='all, delete-orphan')


class ReimburseItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    reimburse_id = db.Column(db.Integer, db.ForeignKey('reimburse_request.id'), nullable=False)
    item_name = db.Column(db.String(200))
    price = db.Column(db.Integer)        # harga satuan
    qty = db.Column(db.Integer, default=1)