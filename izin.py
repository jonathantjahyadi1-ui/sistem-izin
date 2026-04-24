from dotenv import load_dotenv
import os
import pandas as pd
import io

# =========================
# LOAD ENV
# =========================
env_path = os.path.join(os.path.dirname(__file__), ".env")
load_dotenv(env_path)

from flask import Flask, request, jsonify, session, render_template, redirect, url_for, flash, send_file
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime
from datetime import datetime
from sqlalchemy import func

app = Flask(__name__)

app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv("DATABASE_URL")
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
# WEB ROUTES
# =========================
@app.route('/')
def index():
    return redirect('/login')

# LOGIN WEB
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

# REGISTER WEB
@app.route('/register', methods=['GET', 'POST'])
def register_view():
    if request.method == 'POST':

        # ✅ ambil dulu
        username = request.form['username']
        password = request.form['password']
        divisi = request.form['divisi']

        # ✅ baru cek
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

# DASHBOARD
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

        # 🔥 HITUNG PER JENIS IZIN (cuti / sakit)
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
    

@app.route('/export_excel')
def export_excel():
    if 'user_id' not in session:
        return redirect('/login')

    data = LeaveRequest.query.all()

    hasil = []
    for i in data:
        hasil.append({
            'ID': i.id,
            'User ID': i.user_id,
            'Jenis Izin': i.jenis_izin,
            'Tanggal Mulai': i.tanggal_mulai,
            'Tanggal Selesai': i.tanggal_selesai,
            'Durasi': i.durasi,
            'Status': i.status,
            'Alasan': i.alasan
        })

    df = pd.DataFrame(hasil)

    output = io.BytesIO()
    df.to_excel(output, index=False, engine='openpyxl')
    output.seek(0)

    return send_file(
        output,
        download_name="data_izin.xlsx",
        as_attachment=True
    )
# FORM IZIN
@app.route('/form_izin')
def form_izin():
    if 'user_id' not in session:
        return redirect('/login')

    return render_template('izin.html')

# LOGOUT WEB
@app.route('/logout')
def logout_view():
    session.clear()
    return redirect('/login')

# =========================
# IZIN LOGIC
# =========================
@app.route('/izin', methods=['POST'])
def ajukan_izin():
    if 'user_id' not in session:
        return redirect('/login')

    user_id = session['user_id']

    jenis = request.form.get('jenis')
    mulai = request.form.get('mulai')
    selesai = request.form.get('selesai')
    alasan = request.form.get('alasan')
    file = request.files.get('file')

    tgl_mulai = datetime.strptime(mulai, '%Y-%m-%d')
    tgl_selesai = datetime.strptime(selesai, '%Y-%m-%d')

    # 🔥 VALIDASI TANGGAL
    if tgl_selesai < tgl_mulai:
        flash("Tanggal tidak valid!", "danger")
        return redirect('/form_izin')

    durasi = (tgl_selesai - tgl_mulai).days + 1

    # 🔥 VALIDASI SURAT
    if jenis == 'sakit' and durasi > 1 and not file:
        flash("Wajib upload surat dokter!", "danger")
        return redirect('/form_izin')

    filename = None
    if file:
        allowed = ['pdf', 'jpg', 'png', 'jpeg']
        ext = file.filename.split('.')[-1].lower()

        if ext not in allowed:
            flash("Format file tidak didukung!", "danger")
            return redirect('/form_izin')

        import uuid
        filename = str(uuid.uuid4()) + "_" + file.filename

        os.makedirs('uploads', exist_ok=True)
        file.save(os.path.join('uploads', filename))

    izin = LeaveRequest(
        user_id=user_id,
        jenis_izin=jenis,
        tanggal_mulai=tgl_mulai,
        tanggal_selesai=tgl_selesai,
        durasi=durasi,
        alasan=alasan,
        file_surat=filename
    )

    db.session.add(izin)
    db.session.commit()

    flash("Izin berhasil diajukan!", "success")
    return redirect('/dashboard')


@app.route('/admin')
def admin():
    if session.get('role') not in ['admin', 'hrd']:
        return "Akses ditolak"

    data = LeaveRequest.query.all()
    return render_template('admin.html', data=data)

# =========================
# APPROVE / REJECT
# =========================
@app.route('/approve/<int:id>', methods=['POST'])
def approve(id):
    if session.get('role') not in ['hrd', 'admin']:
        return "Akses ditolak"

    izin = LeaveRequest.query.get(id)
    izin.status = 'approved'
    db.session.commit()

    flash("Izin disetujui", "success")
    return redirect('/dashboard')

@app.route('/reject/<int:id>', methods=['POST'])
def reject(id):
    if session.get('role') not in ['hrd', 'admin']:
        return "Akses ditolak"

    izin = LeaveRequest.query.get(id)
    izin.status = 'rejected'
    db.session.commit()

    flash("Izin ditolak", "danger")
    return redirect('/dashboard')

# =========================
# API (OPTIONAL)
# =========================
@app.route('/api/login', methods=['POST'])
def api_login():
    data = request.get_json()

    user = User.query.filter_by(username=data.get('username')).first()

    if not user or not check_password_hash(user.password, data.get('password')):
        return jsonify({'message': 'Login gagal'}), 401

    return jsonify({'message': 'Login berhasil'})

if __name__ == "__main__":
    with app.app_context():
        db.create_all()

        # ADMIN
        if not User.query.filter_by(username='Jonathan').first():
            admin = User(
                username='Jonathan',
                password=generate_password_hash('Jonathan@itsupport'),
                role='admin',
                divisi='IT'
            )
            db.session.add(admin)

        # HRD
        if not User.query.filter_by(username='Devina').first():
            hrd = User(
                username='Devina',
                password=generate_password_hash('Devina@hrd'),
                role='hrd',
                divisi='HRD'
            )
            db.session.add(hrd)

        db.session.commit()
        print("✅ DATABASE READY")

    app.run(debug=True)
