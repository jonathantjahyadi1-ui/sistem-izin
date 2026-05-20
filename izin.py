from dotenv import load_dotenv
import os
import pandas as pd
import io
from datetime import datetime
from flask import Flask, request, session, render_template, redirect, url_for, flash, send_file
from extensions import db
from models import User, LeaveRequest
from werkzeug.security import generate_password_hash, check_password_hash
from calendar import monthrange
from datetime import date, timedelta
from sqlalchemy import func, extract

# =========================
# LOAD ENV
# =========================
if os.getenv("RENDER") is None:
    load_dotenv()

app = Flask(__name__)

UPLOAD_FOLDER = 'uploads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

# ================
# DATABASE CONFIG
# ================
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

db.init_app(app)



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
# FUNGSI KEHADIRAN
# =========================

def get_hari_kerja_dalam_bulan(tahun, bulan, divisi):
    """Menghitung jumlah hari kerja dalam bulan tertentu berdasarkan divisi.
       divisi: 'Operational' atau 'Hostlive' -> kerja Senin-Sabtu (6 hari/minggu)
               lainnya -> kerja Senin-Jumat (5 hari/minggu)
    """
    jumlah_hari = monthrange(tahun, bulan)[1]
    hari_kerja = 0
    for hari in range(1, jumlah_hari+1):
        tgl = date(tahun, bulan, hari)
        if tgl.weekday() == 6:  # Minggu
            continue
        if divisi not in ['Operational', 'Hostlive'] and tgl.weekday() == 5:  # Sabtu
            continue
        hari_kerja += 1
    return hari_kerja

def get_total_hari_izin_approved_per_bulan(user_id, tahun, bulan):
    """Menghitung total hari izin (cuti, sakit, izin_lain) yang approved dan jatuh pada hari kerja di bulan tertentu.
       Tidak double count jika ada izin yang overlapping (missal cuti 3 hari tapi bertabrakan dengan sakit? Tidak mungkin karena user hanya punya satu izin per hari? Asumsi tidak double count).
       Mengembalikan jumlah hari kerja yang terpengaruh izin.
    """
    mulai = date(tahun, bulan, 1)
    selesai = date(tahun, bulan, monthrange(tahun, bulan)[1])
    izin = LeaveRequest.query.filter(
        LeaveRequest.user_id == user_id,
        LeaveRequest.status == 'approved',
        LeaveRequest.tanggal_mulai <= selesai,
        LeaveRequest.tanggal_selesai >= mulai
    ).all()
    # Kumpulkan semua tanggal yang terkena izin (dalam rentang bulan)
    tanggal_izin = set()
    for i in izin:
        tgl_mulai = max(i.tanggal_mulai, mulai)
        tgl_selesai = min(i.tanggal_selesai, selesai)
        delta = (tgl_selesai - tgl_mulai).days + 1
        for d in range(delta):
            tgl = tgl_mulai + timedelta(days=d)
            tanggal_izin.add(tgl)

    # Filter hanya hari kerja sesuai divisi user
    user = User.query.get(user_id)
    if not user:
        return 0
    divisi = user.divisi
    total_hari_izin = 0
    for tgl in tanggal_izin:
        if tgl.weekday() == 6:  # Minggu
            continue
        if divisi not in ['Operational', 'Hostlive'] and tgl.weekday() == 5:  # Sabtu
            continue
        total_hari_izin += 1
    return total_hari_izin

def _hitung_hari_kerja_dari_set(tanggal_set, divisi):
    """Hitung jumlah hari kerja dari set tanggal"""
    hari_kerja = 0
    for tgl in tanggal_set:
        if tgl.weekday() == 6:  # Minggu
            continue
        if divisi not in ['Operational', 'Hostlive'] and tgl.weekday() == 5:  # Sabtu
            continue
        hari_kerja += 1
    return hari_kerja

def get_data_kehadiran_per_bulan(tahun=None, bulan=None, divisi_filter=None):
    """Mengembalikan list data kehadiran untuk semua karyawan.
    Setiap item: {user_id, username, divisi, target, hari_sakit, hari_izin, hari_cuti, hadir}
    """
    if tahun is None:
        tahun = datetime.now().year
    if bulan is None:
        bulan = datetime.now().month
    
    query = User.query.filter(User.role == 'karyawan')
    if divisi_filter:
        query = query.filter(User.divisi == divisi_filter)
    users = query.all()
    data = []
    
    for u in users:
        target = get_hari_kerja_dalam_bulan(tahun, bulan, u.divisi)
        
        mulai = date(tahun, bulan, 1)
        selesai = date(tahun, bulan, monthrange(tahun, bulan)[1])
        
        # ✅ HITUNG SAKIT
        izin_sakit = LeaveRequest.query.filter(
            LeaveRequest.user_id == u.id,
            LeaveRequest.jenis_izin == 'sakit',
            LeaveRequest.status == 'approved',
            LeaveRequest.tanggal_mulai <= selesai,
            LeaveRequest.tanggal_selesai >= mulai
        ).all()
        
        tanggal_sakit = set()
        for i in izin_sakit:
            tgl_mulai = max(i.tanggal_mulai, mulai)
            tgl_selesai = min(i.tanggal_selesai, selesai)
            delta = (tgl_selesai - tgl_mulai).days + 1
            for d in range(delta):
                tgl = tgl_mulai + timedelta(days=d)
                tanggal_sakit.add(tgl)
        
        hari_sakit = _hitung_hari_kerja_dari_set(tanggal_sakit, u.divisi)
        
        # ✅ HITUNG IZIN LAIN
        izin_lain = LeaveRequest.query.filter(
            LeaveRequest.user_id == u.id,
            LeaveRequest.jenis_izin == 'izin_lain',
            LeaveRequest.status == 'approved',
            LeaveRequest.tanggal_mulai <= selesai,
            LeaveRequest.tanggal_selesai >= mulai
        ).all()
        
        tanggal_izin = set()
        for i in izin_lain:
            tgl_mulai = max(i.tanggal_mulai, mulai)
            tgl_selesai = min(i.tanggal_selesai, selesai)
            delta = (tgl_selesai - tgl_mulai).days + 1
            for d in range(delta):
                tgl = tgl_mulai + timedelta(days=d)
                tanggal_izin.add(tgl)
        
        hari_izin = _hitung_hari_kerja_dari_set(tanggal_izin, u.divisi)
        
        #HITUNG CUTI (gak motong hari kerja, cuma informasi)
        izin_cuti = LeaveRequest.query.filter(
            LeaveRequest.user_id == u.id,
            LeaveRequest.jenis_izin == 'cuti',
            LeaveRequest.status == 'approved',
            LeaveRequest.tanggal_mulai <= selesai,
            LeaveRequest.tanggal_selesai >= mulai
        ).all()
        
        tanggal_cuti = set()
        for i in izin_cuti:
            tgl_mulai = max(i.tanggal_mulai, mulai)
            tgl_selesai = min(i.tanggal_selesai, selesai)
            delta = (tgl_selesai - tgl_mulai).days + 1
            for d in range(delta):
                tgl = tgl_mulai + timedelta(days=d)
                tanggal_cuti.add(tgl)
        
        hari_cuti = _hitung_hari_kerja_dari_set(tanggal_cuti, u.divisi)
        
        #KEHADIRAN 
        total_motong = hari_sakit + hari_izin
        hadir = target - total_motong
        
        data.append({
            'user_id': u.id,
            'username': u.username,
            'divisi': u.divisi,
            'target': target,
            'hari_sakit': hari_sakit,      
            'hari_izin': hari_izin,        
            'hadir': hadir
        })
    return data


def get_statistik_divisi(tahun=None, bulan=None):
    """Menghitung rata-rata kehadiran per divisi (hanya untuk karyawan)."""
    if tahun is None:
        tahun = datetime.now().year
    if bulan is None:
        bulan = datetime.now().month
    divisi_list = ['Marketing', 'Operational', 'Hostlive', 'Creative', 'Accounting', 'IT Support', 'HRD']
    stat = []
    for divisi in divisi_list:
        users = User.query.filter(User.role == 'karyawan', User.divisi == divisi).all()
        if not users:
            continue
        total_target = 0
        total_hadir = 0
        for u in users:
            target = get_hari_kerja_dalam_bulan(tahun, bulan, divisi)
            izin_hari = get_total_hari_izin_approved_per_bulan(u.id, tahun, bulan)
            hadir = target - izin_hari
            total_target += target
            total_hadir += hadir
        stat.append({
            'divisi': divisi,
            'rata_target': round(total_target / len(users), 1),
            'rata_hadir': round(total_hadir / len(users), 1),
            'persen': round((total_hadir / total_target) * 100 if total_target > 0 else 0, 1)
        })
    return stat
# ==========================
# AUTO CREATE TABLE & SEEDER
# ==========================
with app.app_context():
    db.create_all()

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

    if not User.query.filter_by(username='aul').first():
        db.session.add(User(
        username='aul',
        password=generate_password_hash('aul@accounting'),
        role='accounting',
        divisi='Accounting'
    ))

    db.session.commit()

# ========
# ROUTES
# ========
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
        return redirect('/main_dashboard')  
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

@app.route('/main_dashboard')
def main_dashboard():
    if 'user_id' not in session:
        return redirect('/login')
    
    user = User.query.get(session['user_id'])
    if not user:
        session.clear()
        return redirect('/login')
    
    if request.args.get('system') == 'izin':
        session['active_system'] = 'izin'
        return redirect('/dashboard')
    elif request.args.get('system') == 'reimburse':
        session['active_system'] = 'reimburse'
        return redirect('/reimburse/list')
    if session.get('active_system') == 'izin':
        return redirect('/dashboard')
    elif session.get('active_system') == 'reimburse':
        return redirect('/reimburse/list')
    
    return render_template('main_dashboard.html', user=user)

@app.route('/change_system')
def change_system():
    session.pop('active_system', None)
    return redirect('/main_dashboard')

@app.route('/dashboard')
def dashboard():
    if 'user_id' not in session:
        return redirect('/login')
    user = User.query.get(session['user_id'])
    if not user:
        session.clear()
        return redirect('/login')

    if user.role == ['karyawan', 'accounting']:
        data = LeaveRequest.query.filter_by(user_id=user.id).all()
        sisa_cuti = get_sisa_cuti(user)
        return render_template('dashboard_user.html', data=data, user=user, sisa_cuti=sisa_cuti)

    # ADMIN / HRD / DIREKTUR
    # Filter kehadiran dari request args
    divisi_filter = request.args.get('divisi', '')
    tahun_filter = int(request.args.get('tahun', datetime.now().year))
    bulan_filter = int(request.args.get('bulan', datetime.now().month))
    
    data_kehadiran = get_data_kehadiran_per_bulan(tahun_filter, bulan_filter, divisi_filter if divisi_filter else None)
    stat_divisi = get_statistik_divisi(tahun_filter, bulan_filter)
    
    data_izin = LeaveRequest.query.all()
    sisa_cuti_pribadi = get_sisa_cuti(user)
    jenis_data = db.session.query(
        LeaveRequest.jenis_izin,
        func.count(LeaveRequest.id)
    ).group_by(LeaveRequest.jenis_izin).all()
    jenis_labels = [j[0] for j in jenis_data]
    jenis_values = [j[1] for j in jenis_data]

    return render_template(
        'dashboard_admin.html',
        data=data_izin,
        user=user,
        sisa_cuti_pribadi=sisa_cuti_pribadi,
        total=LeaveRequest.query.count(),
        pending=LeaveRequest.query.filter_by(status='pending').count(),
        approved=LeaveRequest.query.filter_by(status='approved').count(),
        rejected=LeaveRequest.query.filter_by(status='rejected').count(),
        jenis_labels=jenis_labels,
        jenis_values=jenis_values,
        data_kehadiran=data_kehadiran,
        stat_divisi=stat_divisi,
        divisi_selected=divisi_filter,
        bulan_selected=bulan_filter,
        tahun_selected=tahun_filter
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
    
    # Ambil parameter filter
    status_filter = request.args.get('status', '')
    jenis_filter = request.args.get('jenis', '')
    search = request.args.get('search', '')          # untuk alasan
    divisi_filter = request.args.get('divisi', '')   # baru
    nama_filter = request.args.get('nama', '')       # baru
    
    query = LeaveRequest.query
    
    if status_filter:
        query = query.filter_by(status=status_filter)
    if jenis_filter:
        query = query.filter_by(jenis_izin=jenis_filter)
    if search:
        query = query.filter(LeaveRequest.alasan.ilike(f'%{search}%'))
    
    # Filter berdasarkan divisi (join dengan User)
    if divisi_filter:
        query = query.join(User, LeaveRequest.user_id == User.id).filter(User.divisi == divisi_filter)
    
    # Filter berdasarkan nama pengaju (username)
    if nama_filter:
        query = query.join(User, LeaveRequest.user_id == User.id).filter(User.username.ilike(f'%{nama_filter}%'))
    
    data = query.order_by(LeaveRequest.created_at.desc()).all()
    
    # Kirim juga daftar divisi untuk dropdown
    divisi_list = db.session.query(User.divisi).distinct().all()
    divisi_list = [d[0] for d in divisi_list if d[0]]
    
    return render_template(
        'semua_izin.html',
        data=data,
        user=user,
        status_filter=status_filter,
        jenis_filter=jenis_filter,
        search=search,
        divisi_filter=divisi_filter,
        nama_filter=nama_filter,
        divisi_list=divisi_list
    )

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

# Di izin.py, setelah app initialization
@app.route('/uploads/<filename>')
def uploaded_file(filename):
    from flask import send_from_directory
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

from reimburse.routes import reimburse_bp
app.register_blueprint(reimburse_bp, url_prefix='/reimburse')

if __name__ == "__main__":
    app.run(debug=True)