from dotenv import load_dotenv
import os
import pandas as pd
import io
from datetime import datetime
from flask import Flask, request, jsonify, session, render_template, redirect, url_for, flash, send_file
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from sqlalchemy import func

# =========================
# LOAD ENV
# =========================
load_dotenv()

app = Flask(__name__)

# =========================
# FIX DATABASE URL (RENDER)
# =========================
uri = os.getenv("DATABASE_URL")

if uri and uri.startswith("postgres://"):
    uri = uri.replace("postgres://", "postgresql://", 1)

app.config['SQLALCHEMY_DATABASE_URI'] = uri
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = os.getenv("SECRET_KEY")

db = SQLAlchemy(app)

# =========================
# MODEL USER
# =========================
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(100), unique=True, nullable=False)
    password = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(50), default='karyawan')
    divisi = db.Column(db.String(50))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

# =========================
# MODEL IZIN
# =========================
class LeaveRequest(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer)
    jenis_izin = db.Column(db.String(50))
    tanggal_mulai = db.Column(db.Date)
    tanggal_selesai = db.Column(db.Date)
    durasi = db.Column(db.Integer)
    alasan = db.Column(db.Text)
    file_surat = db.Column(db.String(255))
    status = db.Column(db.String(50), default='pending')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

# =========================
# AUTO CREATE TABLE
# =========================
with app.app_context():
    db.create_all()

    # ADMIN DEFAULT
    if not User.query.filter_by(username='Jonathan').first():
        admin = User(
            username='Jonathan',
            password=generate_password_hash('Jonathan@itsupport'),
            role='admin',
            divisi='IT'
        )
        db.session.add(admin)

    if not User.query.filter_by(username='Devina').first():
        hrd = User(
            username='Devina',
            password=generate_password_hash('Devina@hrd'),
            role='hrd',
            divisi='HRD'
        )
        db.session.add(hrd)

    db.session.commit()

# =========================
# ROUTES
# =========================
@app.route('/')
def index():
    return redirect('/login')

@app.route('/login', methods=['GET', 'POST'])
def login_view():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']

        user = User.query.filter_by(username=username).first()

        if not user or not check_password_hash(user.password, password):
            flash("Username / password salah!", "danger")
            return redirect('/login')

        session['user_id'] = user.id
        session['role'] = user.role

        return redirect('/dashboard')

    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register_view():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        divisi = request.form['divisi']

        if User.query.filter_by(username=username).first():
            return "Username sudah dipakai"

        hashed = generate_password_hash(password)

        user = User(
            username=username,
            password=hashed,
            divisi=divisi
        )

        db.session.add(user)
        db.session.commit()

        return redirect('/login')

    return render_template('register.html')

@app.route('/dashboard')
def dashboard():
    if 'user_id' not in session:
        return redirect('/login')

    user = User.query.get(session['user_id'])

    if user.role == 'karyawan':
        data = LeaveRequest.query.filter_by(user_id=user.id).all()
        return render_template('dashboard_user.html', data=data, user=user)
    else:
        data = LeaveRequest.query.all()

        total = LeaveRequest.query.count()
        pending = LeaveRequest.query.filter_by(status='pending').count()
        approved = LeaveRequest.query.filter_by(status='approved').count()
        rejected = LeaveRequest.query.filter_by(status='rejected').count()

        jenis_data = db.session.query(
            LeaveRequest.jenis_izin,
            func.count(LeaveRequest.id)
        ).group_by(LeaveRequest.jenis_izin).all()

        jenis_labels = [j[0] for j in jenis_data]
        jenis_values = [j[1] for j in jenis_data]

        return render_template(
            'dashboard_admin.html',
            data=data,
            user=user,
            total=total,
            pending=pending,
            approved=approved,
            rejected=rejected,
            jenis_labels=jenis_labels,
            jenis_values=jenis_values
        )

@app.route('/logout')
def logout_view():
    session.clear()
    return redirect('/login')

@app.route('/izin', methods=['POST'])
def ajukan_izin():
    if 'user_id' not in session:
        return redirect('/login')

    user_id = session['user_id']

    jenis = request.form.get('jenis')
    mulai = request.form.get('mulai')
    selesai = request.form.get('selesai')
    alasan = request.form.get('alasan')

    tgl_mulai = datetime.strptime(mulai, '%Y-%m-%d')
    tgl_selesai = datetime.strptime(selesai, '%Y-%m-%d')

    durasi = (tgl_selesai - tgl_mulai).days + 1

    izin = LeaveRequest(
        user_id=user_id,
        jenis_izin=jenis,
        tanggal_mulai=tgl_mulai,
        tanggal_selesai=tgl_selesai,
        durasi=durasi,
        alasan=alasan
    )

    db.session.add(izin)
    db.session.commit()

    flash("Izin berhasil diajukan!", "success")
    return redirect('/dashboard')

@app.route('/approve/<int:id>', methods=['POST'])
def approve(id):
    izin = LeaveRequest.query.get(id)
    izin.status = 'approved'
    db.session.commit()
    return redirect('/dashboard')

@app.route('/reject/<int:id>', methods=['POST'])
def reject(id):
    izin = LeaveRequest.query.get(id)
    izin.status = 'rejected'
    db.session.commit()
    return redirect('/dashboard')