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
if os.getenv("RENDER") is None:
    load_dotenv()
    
app = Flask(__name__)

# =========================
# DATABASE CONFIG (FIX SUPABASE + RENDER)
# =========================
uri = os.getenv("DATABASE_URL")

if not uri:
    raise Exception("DATABASE_URL belum diset di environment!")

if uri.startswith("postgres://"):
    uri = uri.replace("postgres://", "postgresql://", 1)

# 🔥 WAJIB untuk Supabase
if "sslmode" not in uri:
    if "?" in uri:
        uri += "&sslmode=require"
    else:
        uri += "?sslmode=require"

app.config['SQLALCHEMY_DATABASE_URI'] = uri
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = os.getenv("SECRET_KEY", "supersecret")

# 🔥 biar koneksi stabil
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    "pool_pre_ping": True,
    "pool_recycle": 300,
}

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

    if not User.query.filter_by(username='Jonathan').first():
        db.session.add(User(
            username='Jonathan',
            password=generate_password_hash('Jonathan@itsupport'),
            role='admin',
            divisi='IT'
        ))

    if not User.query.filter_by(username='Devina').first():
        db.session.add(User(
            username='Devina',
            password=generate_password_hash('Devina@hrd'),
            role='hrd',
            divisi='HRD'
        ))

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
        user = User.query.filter_by(username=request.form['username']).first()

        if not user or not check_password_hash(user.password, request.form['password']):
            flash("Username / password salah!", "danger")
            return redirect('/login')

        session['user_id'] = user.id
        session['role'] = user.role

        return redirect('/dashboard')

    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register_view():
    if request.method == 'POST':
        if User.query.filter_by(username=request.form['username']).first():
            return "Username sudah dipakai"

        user = User(
            username=request.form['username'],
            password=generate_password_hash(request.form['password']),
            divisi=request.form['divisi']
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

    if not user:
        session.clear()
        return redirect('/login')

    if user.role == 'karyawan':
        data = LeaveRequest.query.filter_by(user_id=user.id).all()
        return render_template('dashboard_user.html', data=data, user=user)

    data = LeaveRequest.query.all()

    return render_template(
        'dashboard_admin.html',
        data=data,
        user=user,
        total=LeaveRequest.query.count(),
        pending=LeaveRequest.query.filter_by(status='pending').count(),
        approved=LeaveRequest.query.filter_by(status='approved').count(),
        rejected=LeaveRequest.query.filter_by(status='rejected').count()
    )

@app.route('/form_izin')
def form_izin():
    if 'user_id' not in session:
        return redirect('/login')

    return render_template('izin.html')

@app.route('/izin', methods=['POST'])
def ajukan_izin():
    if 'user_id' not in session:
        return redirect('/login')

    mulai = datetime.strptime(request.form['mulai'], '%Y-%m-%d')
    selesai = datetime.strptime(request.form['selesai'], '%Y-%m-%d')

    izin = LeaveRequest(
        user_id=session['user_id'],
        jenis_izin=request.form['jenis'],
        tanggal_mulai=mulai,
        tanggal_selesai=selesai,
        durasi=(selesai - mulai).days + 1,
        alasan=request.form['alasan']
    )

    db.session.add(izin)
    db.session.commit()

    flash("Izin berhasil diajukan!", "success")
    return redirect('/dashboard')

@app.route('/logout')
def logout_view():
    session.clear()
    return redirect('/login')

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