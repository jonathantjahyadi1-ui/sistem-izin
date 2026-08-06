from datetime import datetime
from extensions import db


class MeetingRoom(db.Model):
    __tablename__ = 'meeting_rooms'

    id = db.Column(db.Integer, primary_key=True)
    room_name = db.Column(db.String(100), nullable=False)
    capacity = db.Column(db.Integer, nullable=False)
    location = db.Column(db.String(100), nullable=False)
    facilities = db.Column(db.Text, default='')
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    bookings = db.relationship('RoomBooking', backref='room', lazy=True)

    def get_facilities_list(self):
        if not self.facilities:
            return []
        return [item.strip() for item in self.facilities.split(',') if item.strip()]

    @classmethod
    def get_active_rooms(cls):
        return cls.query.filter_by(is_active=True).order_by(cls.room_name.asc()).all()

    @classmethod
    def seed_defaults(cls):
        defaults = [
            {
                'room_name': 'Ruang Meeting 1',
                'capacity': 10,
                'location': 'Lantai 1',
                'facilities': 'TV, Whiteboard, AC',
            },
            {
                'room_name': 'Ruang Meeting 2',
                'capacity': 15,
                'location': 'Lantai 2',
                'facilities': 'Projector, Whiteboard, AC',
            },
            {
                'room_name': 'Ruang Meeting Besar',
                'capacity': 30,
                'location': 'Lantai 3',
                'facilities': 'Projector, Sound System, Whiteboard, AC',
            },
        ]

        for data in defaults:
            exists = cls.query.filter_by(room_name=data['room_name']).first()
            if not exists:
                db.session.add(cls(**data))

        db.session.commit()


class RoomBooking(db.Model):
    __tablename__ = 'room_bookings'

    STATUS_PENDING = 'Pending'
    STATUS_APPROVED = 'Approved'
    STATUS_REJECTED = 'Rejected'
    STATUS_CANCELLED = 'Cancelled'
    VALID_STATUSES = [STATUS_PENDING, STATUS_APPROVED, STATUS_REJECTED, STATUS_CANCELLED]

    id = db.Column(db.Integer, primary_key=True)

    # Disengaja tanpa ForeignKey ke User agar aman digabung ke sistem izin lama
    # yang memakai tabel user dan kolom berbeda.
    user_id = db.Column(db.Integer, nullable=False, index=True)

    room_id = db.Column(db.Integer, db.ForeignKey('meeting_rooms.id'), nullable=False, index=True)
    title = db.Column(db.String(200), nullable=False)
    division = db.Column(db.String(50), nullable=False)
    meeting_date = db.Column(db.Date, nullable=False, index=True)
    start_time = db.Column(db.Time, nullable=False)
    end_time = db.Column(db.Time, nullable=False)
    participant_count = db.Column(db.Integer, nullable=False)
    purpose = db.Column(db.Text, nullable=False)
    notes = db.Column(db.Text, default='')

    status = db.Column(db.String(20), default=STATUS_PENDING, index=True)
    reject_reason = db.Column(db.Text, default=None)

    approved_by = db.Column(db.Integer, default=None)
    approved_at = db.Column(db.DateTime, default=None)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def get_status_badge_class(self):
        return {
            self.STATUS_PENDING: 'bg-warning text-dark',
            self.STATUS_APPROVED: 'bg-success',
            self.STATUS_REJECTED: 'bg-danger',
            self.STATUS_CANCELLED: 'bg-secondary',
        }.get(self.status, 'bg-secondary')
