from dotenv import load_dotenv
import os
import pandas as pd
import io
from datetime import datetime
from flask import Flask, request, session, render_template, redirect, url_for, flash, send_file
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from sqlalchemy import func, extract   # <--- tambahkan extract

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
# DATABASE CONFIG
# =========================
uri = os.getenv("DATABASE_URL")
if not uri: 
    raise Exception("DATABASE_URL belum diset di environment!")

if uri.startswith("postgres://"):
    uri = uri.replace("postgres://", "postgresql://", 1)

if "sslmode" not in uri:
    if "?" in uri:
        uri += "&sslmode=require"
    else:
        uri += "?sslmode=require"

app.config['SQLALCHEMY_DATABASE_URI'] = uri
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = os.getenv("SECRET_KEY", "supersecret")

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
    kuota_cuti = db.Column(db.Integer, default=12)   # tambahan
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
# FUNGSI BANTU CUTI (diletakkan setelah model)
# =========================
def get_total_cuti_approved_this_year(user_id, tahun=None):
    """Total durasi cuti (jenis='cuti') yang sudah approved pada tahun tertentu"""
    if tahun is None:
        tahun = datetime.now().year
    total = db.session.query(func.sum(LeaveRequest.durasi)) \
        .filter(LeaveRequest.user_id == user_id) \
        .filter(LeaveRequest.jenis_izin == 'cuti') \
        .filter(LeaveRequest.status == 'approved') \
        .filter(extract('year', LeaveRequest.tanggal_mulai) == tahun) \
        .scalar() or 0
    return total

def get_sisa_cuti(user):
    """Menghitung sisa cuti user berdasarkan kuota dan total cuti approved tahun ini"""
    total_dipakai = get_total_cuti_approved_this_year(user.id)
    return user.kuota_cuti - total_dipakai

# =========================
# AUTO CREATE TABLE & SEEDER
# =========================
with app.app_context():
    db.create_all()

    # Tambah kolom kuota_cuti jika belum ada (migrasi)
    inspector = db.inspect(db.engine)
    if 'kuota_cuti' not in [c['name'] for c in inspector.get_columns('user')]:
        with db.engine.connect() as conn:
            conn.execute(db.text('ALTER TABLE "user" ADD COLUMN kuota_cuti INTEGER DEFAULT 12'))
            conn.commit()

    # ADMIN
    if not User.query.filter_by(username='Jonathan').first():
        db.session.add(User(
            username='Jonathan',
            password=generate_password_hash('Jonathan@itsupport'),
            role='admin',
            divisi='IT'
        ))

    # HRD
    if not User.query.filter_by(username='Devina').first():
        db.session.add(User(
            username='Devina',
            password=generate_password_hash('Devina@hrd'),
            role='hrd',
            divisi='HRD'
        ))

    # DIREKTUR
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
        sisa_cuti = get_sisa_cuti(user)
        return render_template('dashboard_user.html', data=data, user=user, sisa_cuti=sisa_cuti)

    # ADMIN / HRD / DIREKTUR
    data = LeaveRequest.query.all()
    sisa_cuti_pribadi = get_sisa_cuti(user)   # <--- DITAMBAHKAN
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
        sisa_cuti_pribadi=sisa_cuti_pribadi,
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

    file_surat = request.files.get('file')
    filename_surat = None
    if file_surat and file_surat.filename != '':
        filename_surat = f"surat_{datetime.now().timestamp()}_{file_surat.filename}"
        file_surat.save(os.path.join(app.config['UPLOAD_FOLDER'], filename_surat))

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
        file_chat=filename_chat
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

@app.route('/semua_izin')
def semua_izin():
    if 'user_id' not in session:
        return redirect('/login')
    user = User.query.get(session['user_id'])
    if user.role not in ['admin', 'hrd', 'direktur']:
        return redirect('/dashboard')
    
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
                           status_filter=status_filter, jenis_filter=jenis_filter, search=search)

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
    new_user = User(username=username, password=generate_password_hash(password), role=role, divisi=divisi)
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

@app.route('/export_excel')
def export_excel():
    if 'user_id' not in session:
        return redirect('/login')
    user = User.query.get(session['user_id'])
    if user.role not in ['admin', 'hrd', 'direktur']:        
        return redirect('/dashboard')
    data = LeaveRequest.query.all()
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
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Data Izin')
    output.seek(0)
    filename = f"Rekap_Izin_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    return send_file(output, mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                     as_attachment=True, download_name=filename)

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
    if not izin:
        flash('Izin tidak ditemukan', 'danger')
        return redirect(request.referrer or '/dashboard')
    
    # Validasi kuota cuti
    if izin.jenis_izin == 'cuti':
        user = User.query.get(izin.user_id)
        sisa = get_sisa_cuti(user)
        if izin.durasi > sisa:
            flash(f'Gagal approve! Sisa cuti {user.username} hanya {sisa} hari, '
                  f'sedangkan cuti yang diajukan {izin.durasi} hari.', 'danger')
            return redirect(request.referrer or '/dashboard')
    
    izin.status = 'approved'
    db.session.commit()
    flash('Izin berhasil di-approve.', 'success')
    return redirect(request.referrer or '/dashboard')

@app.route('/reject/<int:id>', methods=['POST'])
def reject(id):
    izin = LeaveRequest.query.get(id)
    if izin:
        izin.status = 'rejected'
        db.session.commit()
        flash('Izin ditolak.', 'warning')
    return redirect(request.referrer or '/dashboard')

# =========================
# ROUTE MANAJEMEN KUOTA CUTI (HRD/ADMIN)
# =========================
@app.route('/manage_quota')
def manage_quota():
    if 'user_id' not in session:
        return redirect('/login')
    user = User.query.get(session['user_id'])
    if user.role not in ['admin', 'hrd']:
        return redirect('/dashboard')
    # Tampilkan semua user dengan role karyawan
    users = User.query.filter(User.role != 'direktur').all()
    user_data = []
    for u in users:
        user_data.append({
            'id': u.id,
            'username': u.username,
            'divisi': u.divisi,
            'kuota': u.kuota_cuti,
            'sisa': get_sisa_cuti(u)
        })
    return render_template('manage_quota.html', users=user_data, current_user=user)

@app.route('/update_quota/<int:user_id>', methods=['POST'])
def update_quota(user_id):
    if 'user_id' not in session:
        return redirect('/login')
    current = User.query.get(session['user_id'])
    if current.role not in ['admin', 'hrd']:
        return redirect('/dashboard')
    new_kuota = int(request.form['kuota'])
    user = User.query.get(user_id)
    if user:
        user.kuota_cuti = new_kuota
        db.session.commit()
        flash(f'Kuota cuti {user.username} diperbarui menjadi {new_kuota} hari.', 'success')
    return redirect('/manage_quota')

if __name__ == "__main__":
    app.run(debug=True)