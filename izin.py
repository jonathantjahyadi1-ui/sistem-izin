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

UPLOAD_FOLDER = 'uploads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

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
    file_chat = db.Column(db.String(255))
    status = db.Column(db.String(50), default='pending')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

# =========================
# AUTO CREATE TABLE
# =========================
with app.app_context():
    db.create_all()

    # 🔥 ADMIN
    if not User.query.filter_by(username='Jonathan').first():
        db.session.add(User(
            username='Jonathan',
            password=generate_password_hash('Jonathan@itsupport'),
            role='admin',
            divisi='IT'
        ))

    # 🔥 HRD
    if not User.query.filter_by(username='Devina').first():
        db.session.add(User(
            username='Devina',
            password=generate_password_hash('Devina@hrd'),
            role='hrd',
            divisi='HRD'
        ))

    # 🔥 DIREKTUR (CREATE ATAU UPDATE PASSWORD)
    user = User.query.filter_by(username='Martin').first()
    if user:
        user.password = generate_password_hash('Martin@direktur')
    else:
        db.session.add(User(
            username='Martin',
            password=generate_password_hash('Martin@direktur'),
            role='direktur',
            divisi='Direksi'
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

    # 🔥 INI MASIH DALAM FUNCTION
    data = LeaveRequest.query.all()

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
        total=LeaveRequest.query.count(),
        pending=LeaveRequest.query.filter_by(status='pending').count(),
        approved=LeaveRequest.query.filter_by(status='approved').count(),
        rejected=LeaveRequest.query.filter_by(status='rejected').count(),
        jenis_labels=jenis_labels,
        jenis_values=jenis_values
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

    # Proses File Surat Sakit
    file_surat = request.files.get('file')
    filename_surat = None
    if file_surat and file_surat.filename != '':
        filename_surat = f"surat_{datetime.now().timestamp()}_{file_surat.filename}"
        file_surat.save(os.path.join(app.config['UPLOAD_FOLDER'], filename_surat))

    # Proses File Bukti Chat (BARU)
    file_chat = request.files.get('file_chat')
    filename_chat = None
    if file_chat and file_chat.filename != '':
        filename_chat = f"chat_{datetime.now().timestamp()}_{file_chat.filename}"
        file_chat.save(os.path.join(app.config['UPLOAD_FOLDER'], filename_chat))

    izin = LeaveRequest(
        user_id=session['user_id'],
        jenis_izin=request.form['jenis'],
        tanggal_mulai=mulai,
        tanggal_selesai=selesai,
        durasi=(selesai - mulai).days + 1,
        alasan=request.form['alasan'],
        file_surat=filename_surat,
        file_chat=filename_chat  # <--- Simpan nama file chat ke database
    )

    db.session.add(izin)
    db.session.commit()

    flash("Izin berhasil diajukan!", "success")
    return redirect('/dashboard')

@app.route('/download/<filename>')
def download_file(filename):
    return send_file(
        os.path.join(app.config['UPLOAD_FOLDER'], filename),
        as_attachment=True
    )

# =========================
# HALAMAN SEMUA IZIN (ADMIN/HRD)
# =========================
@app.route('/semua_izin')
def semua_izin():
    if 'user_id' not in session:
        return redirect('/login')
    
    user = User.query.get(session['user_id'])
    if user.role not in ['admin', 'hrd', 'direktur']:
        return redirect('/dashboard')
    
    # Filter
    status_filter = request.args.get('status', '')
    jenis_filter = request.args.get('jenis', '')
    search = request.args.get('search', '')
    
    query = LeaveRequest.query
    
    if status_filter:
        query = query.filter_by(status=status_filter)
    if jenis_filter:
        query = query.filter_by(jenis_izin=jenis_filter)
    if search:
        query = query.filter(LeaveRequest.alasan.ilike(f'%{search}%'))
    
    data = query.order_by(LeaveRequest.created_at.desc()).all()
    
    return render_template('semua_izin.html', data=data, user=user,
                        status_filter=status_filter,
                        jenis_filter=jenis_filter,
                        search=search)


# =========================
# MANAJEMEN USER (ADMIN)
# =========================
@app.route('/manage_users')
def manage_users():
    if 'user_id' not in session:
        return redirect('/login')
    
    user = User.query.get(session['user_id'])
    if user.role != 'admin':
        return redirect('/dashboard')
    
    users = User.query.all()
    return render_template('manage_users.html', users=users, current_user=user)


@app.route('/add_user', methods=['POST'])
def add_user():
    if 'user_id' not in session:
        return redirect('/login')
    
    current_user = User.query.get(session['user_id'])
    if current_user.role != 'admin':
        return redirect('/dashboard')
    
    username = request.form['username']
    password = request.form['password']
    role = request.form['role']
    divisi = request.form['divisi']
    
    if User.query.filter_by(username=username).first():
        flash('Username sudah ada!', 'danger')
        return redirect('/manage_users')
    
    new_user = User(
        username=username,
        password=generate_password_hash(password),
        role=role,
        divisi=divisi
    )
    db.session.add(new_user)
    db.session.commit()
    flash('User berhasil ditambahkan!', 'success')
    return redirect('/manage_users')


@app.route('/reset_password/<int:id>', methods=['POST'])
def reset_password(id):
    if 'user_id' not in session:
        return redirect('/login')
    
    current_user = User.query.get(session['user_id'])
    if current_user.role != 'admin':
        return redirect('/dashboard')
    
    user = User.query.get(id)
    new_password = request.form['new_password']
    user.password = generate_password_hash(new_password)
    db.session.commit()
    flash(f'Password {user.username} berhasil direset!', 'success')
    return redirect('/manage_users')


# =========================
# EXPORT EXCEL
# =========================
@app.route('/export_excel')
def export_excel():
    if 'user_id' not in session:
        return redirect('/login')
    
    user = User.query.get(session['user_id'])
    if user.role not in ['admin', 'hrd', 'direktur']:        
        return redirect('/dashboard')
    
    data = LeaveRequest.query.all()
    
    # Buat DataFrame
    rows = []
    for i in data:
        pengaju = User.query.get(i.user_id)
        rows.append({
            'ID': i.id,
            'Pengaju': pengaju.username if pengaju else '-',
            'Divisi': pengaju.divisi if pengaju else '-',
            'Jenis Izin': i.jenis_izin,
            'Tanggal Mulai': i.tanggal_mulai,
            'Tanggal Selesai': i.tanggal_selesai,
            'Durasi (Hari)': i.durasi,
            'Alasan': i.alasan,
            'Status': i.status.upper(),
            'Tanggal Ajuan': i.created_at
        })
    
    df = pd.DataFrame(rows)
    
    # Export ke Excel
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Data Izin')
    
    output.seek(0)
    
    filename = f"Rekap_Izin_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    
    return send_file(
        output,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        as_attachment=True,
        download_name=filename
    )

@app.context_processor
def utility_processor():
    def get_user(user_id):
        return db.session.get(User, user_id)
    return dict(get_user=get_user)

@app.route('/approval')
def approval():
    if 'user_id' not in session:
        return redirect('/login')

    user = User.query.get(session['user_id'])

    if user.role not in ['admin', 'hrd', 'direktur']:        
        return redirect('/dashboard')

    data = LeaveRequest.query.filter_by(status='pending').all()

    return render_template('approval.html', data=data, user=user)

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

if __name__ == "__main__":
    app.run(debug=True)
