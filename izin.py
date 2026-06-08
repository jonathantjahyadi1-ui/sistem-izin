from dotenv import load_dotenv
import os
import pandas as pd
import io
from datetime import datetime
from flask import Flask, request, session, render_template, redirect, url_for, flash, send_file, send_from_directory
from werkzeug.utils import secure_filename
from supabase import create_client
import mimetypes
from extensions import db
from models import User, LeaveRequest
from reimburse.models import ReimburseRequest, ReimburseItem
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

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
SUPABASE_STORAGE_BUCKET = os.getenv("SUPABASE_STORAGE_BUCKET", "izin-files")

if not SUPABASE_URL:
    raise Exception("SUPABASE_URL belum diset di environment!")

if not SUPABASE_SERVICE_ROLE_KEY:
    raise Exception("SUPABASE_SERVICE_ROLE_KEY belum diset di environment!")

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

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



# ==================
# FUNGSI BANTU CUTI
# ==================
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

# ================
# FUNGSI KEHADIRAN
# ================

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

    # Tambah kolom receipt_photo di reimburse_item untuk nota per item
    if 'reimburse_item' in inspector.get_table_names():
        reimburse_item_columns = [c['name'] for c in inspector.get_columns('reimburse_item')]

        if 'receipt_photo' not in reimburse_item_columns:
            with db.engine.connect() as conn:
                conn.execute(db.text('ALTER TABLE reimburse_item ADD COLUMN receipt_photo VARCHAR(255)'))
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

    selected_system = request.args.get('system')

    if selected_system == 'izin':
        session['active_system'] = 'izin'
        return redirect('/dashboard')

    elif selected_system == 'reimburse':
        session['active_system'] = 'reimburse'
        return redirect('/reimburse/list')

    elif selected_system == 'purchase_order':
        session['active_system'] = 'purchase_order'
        return redirect('/purchase-order/list')

    if session.get('active_system') == 'izin':
        return redirect('/dashboard')

    elif session.get('active_system') == 'reimburse':
        return redirect('/reimburse/list')

    elif session.get('active_system') == 'purchase_order':
        return redirect('/purchase-order/list')
    
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

    if user.role in ['karyawan', 'accounting']:
        data = LeaveRequest.query.filter_by(user_id=user.id).all()
        sisa_cuti = get_sisa_cuti(user)
        return render_template('dashboard_user.html', data=data, user=user, sisa_cuti=sisa_cuti)

    # ADMIN / HRD / DIREKTUR
    divisi_filter = request.args.get('divisi', '')
    tahun_filter = int(request.args.get('tahun', datetime.now().year))
    bulan_filter = int(request.args.get('bulan', datetime.now().month))
    
    data_kehadiran = get_data_kehadiran_per_bulan(tahun_filter, bulan_filter, divisi_filter if divisi_filter else None)
    stat_divisi = get_statistik_divisi(tahun_filter, bulan_filter)
    
    data_izin = LeaveRequest.query.order_by(
    LeaveRequest.created_at.desc()
    ).limit(10).all()
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

ALLOWED_IMAGE_EXTENSIONS = {'jpg', 'jpeg', 'png'}
ALLOWED_DOCUMENT_EXTENSIONS = {'jpg', 'jpeg', 'png', 'pdf'}

app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024  # Maksimal 5MB


def allowed_file(filename, allowed_extensions):
    return (
        '.' in filename and
        filename.rsplit('.', 1)[1].lower() in allowed_extensions
    )


def upload_file_to_supabase(file, folder, allowed_extensions):
    if not file or file.filename == '':
        return None

    if not allowed_file(file.filename, allowed_extensions):
        raise ValueError("Format file tidak didukung.")

    original_filename = secure_filename(file.filename)
    timestamp = datetime.now().strftime('%Y%m%d%H%M%S%f')
    filename = f"{folder}_{timestamp}_{original_filename}"

    storage_path = f"izin/{folder}/{filename}"

    content_type = (
        file.mimetype or
        mimetypes.guess_type(original_filename)[0] or
        "application/octet-stream"
    )

    file.stream.seek(0)
    file_bytes = file.read()

    supabase.storage.from_(SUPABASE_STORAGE_BUCKET).upload(
        path=storage_path,
        file=file_bytes,
        file_options={
            "content-type": content_type,
            "cache-control": "3600",
            "upsert": "false"
        }
    )

    return storage_path


def create_supabase_signed_url(storage_path, expires_in=300):
    response = supabase.storage.from_(SUPABASE_STORAGE_BUCKET).create_signed_url(
        storage_path,
        expires_in
    )

    signed_url = None

    if isinstance(response, dict):
        signed_url = (
            response.get("signedURL") or
            response.get("signedUrl") or
            response.get("signed_url")
        )
    else:
        signed_url = (
            getattr(response, "signed_url", None) or
            getattr(response, "signedURL", None)
        )

    if signed_url and signed_url.startswith("/"):
        signed_url = SUPABASE_URL.rstrip("/") + signed_url

    return signed_url


@app.route('/izin', methods=['POST'])
def ajukan_izin():
    if 'user_id' not in session:
        return redirect('/login')

    mulai = datetime.strptime(request.form['mulai'], '%Y-%m-%d')
    selesai = datetime.strptime(request.form['selesai'], '%Y-%m-%d')

    if selesai < mulai:
        flash("Tanggal selesai tidak boleh lebih awal dari tanggal mulai.", "danger")
        return redirect('/form_izin')

    jenis_izin = request.form.get('jenis')
    alasan = request.form.get('alasan')

    if not jenis_izin or not alasan:
        flash("Jenis izin dan alasan wajib diisi.", "danger")
        return redirect('/form_izin')

    # =========================
    # Upload surat dokter
    # =========================
    file_surat = request.files.get('file')
    filename_surat = None

    try:
        if file_surat and file_surat.filename != '':
            filename_surat = upload_file_to_supabase(
                file_surat,
                "surat",
                ALLOWED_DOCUMENT_EXTENSIONS
            )
    except ValueError:
        flash("Format surat dokter harus PDF, JPG, JPEG, atau PNG.", "danger")
        return redirect('/form_izin')
    except Exception as e:
        print("ERROR UPLOAD SURAT:", repr(e))
        flash(f"Gagal upload surat dokter: {str(e)}", "danger")
        return redirect('/form_izin')

    # =========================
    # Upload bukti chat
    # =========================
    file_chat = request.files.get('file_chat')
    filename_chat = None

    if not file_chat or file_chat.filename == '':
        flash("Bukti chat wajib diupload.", "danger")
        return redirect('/form_izin')

    try:
        filename_chat = upload_file_to_supabase(
            file_chat,
            "chat",
            ALLOWED_IMAGE_EXTENSIONS
        )
    except ValueError:
        flash("Format bukti chat harus JPG, JPEG, atau PNG.", "danger")
        return redirect('/form_izin')
    except Exception as e:
        print("ERROR UPLOAD CHAT:", repr(e))
        flash(f"Gagal upload bukti chat: {str(e)}", "danger")
        return redirect('/form_izin')

    izin = LeaveRequest(
        user_id=session['user_id'],
        jenis_izin=jenis_izin,
        tanggal_mulai=mulai,
        tanggal_selesai=selesai,
        durasi=(selesai - mulai).days + 1,
        alasan=alasan,
        file_surat=filename_surat,
        file_chat=filename_chat
    )

    db.session.add(izin)
    db.session.commit()

    flash("Izin berhasil diajukan!", "success")
    return redirect('/dashboard')


@app.route('/download/<path:filename>')
def download_file(filename):
    if 'user_id' not in session:
        return redirect('/login')

    # File baru dari Supabase Storage:
    # izin/chat/namafile.png
    # izin/surat/namafile.pdf
    if filename.startswith("izin/"):
        try:
            signed_url = create_supabase_signed_url(filename, expires_in=300)

            if not signed_url:
                return """
                <!DOCTYPE html>
                <html>
                <head>
                    <title>File Tidak Bisa Dibuka</title>
                    <style>
                        body {
                            font-family: Arial, sans-serif;
                            background: #f8fafc;
                            display: flex;
                            align-items: center;
                            justify-content: center;
                            min-height: 100vh;
                            margin: 0;
                        }
                        .box {
                            background: white;
                            padding: 30px;
                            border-radius: 12px;
                            box-shadow: 0 8px 24px rgba(0,0,0,0.08);
                            max-width: 480px;
                            text-align: center;
                        }
                        h3 { color: #ef4444; }
                        p {
                            color: #374151;
                            line-height: 1.5;
                        }
                    </style>
                </head>
                <body>
                    <div class="box">
                        <h3>File tidak bisa dibuka</h3>
                        <p>Gagal membuat link preview file dari Supabase Storage.</p>
                    </div>
                </body>
                </html>
                """, 500

            return redirect(signed_url)

        except Exception as e:
            print("ERROR SIGNED URL:", e)
            return """
            <!DOCTYPE html>
            <html>
            <head>
                <title>File Tidak Bisa Dibuka</title>
                <style>
                    body {
                        font-family: Arial, sans-serif;
                        background: #f8fafc;
                        display: flex;
                        align-items: center;
                        justify-content: center;
                        min-height: 100vh;
                        margin: 0;
                    }
                    .box {
                        background: white;
                        padding: 30px;
                        border-radius: 12px;
                        box-shadow: 0 8px 24px rgba(0,0,0,0.08);
                        max-width: 480px;
                        text-align: center;
                    }
                    h3 { color: #ef4444; }
                    p {
                        color: #374151;
                        line-height: 1.5;
                    }
                </style>
            </head>
            <body>
                <div class="box">
                    <h3>File tidak bisa dibuka</h3>
                    <p>Terjadi masalah saat mengambil file dari Supabase Storage.</p>
                </div>
            </body>
            </html>
            """, 500

    # Fallback untuk file lama yang dulu disimpan lokal di folder uploads
    safe_filename = secure_filename(filename)
    file_path = os.path.join(app.config['UPLOAD_FOLDER'], safe_filename)

    if not os.path.isfile(file_path):
        return """
        <!DOCTYPE html>
        <html>
        <head>
            <title>File Tidak Ditemukan</title>
            <style>
                body {
                    font-family: Arial, sans-serif;
                    background: #f8fafc;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                    min-height: 100vh;
                    margin: 0;
                }
                .box {
                    background: white;
                    padding: 30px;
                    border-radius: 12px;
                    box-shadow: 0 8px 24px rgba(0,0,0,0.08);
                    max-width: 480px;
                    text-align: center;
                }
                h3 { color: #ef4444; }
                p {
                    color: #374151;
                    line-height: 1.5;
                }
            </style>
        </head>
        <body>
            <div class="box">
                <h3>File tidak ditemukan</h3>
                <p>
                    File lama tidak ada di folder uploads server.
                    Silakan upload ulang, atau gunakan data izin baru setelah sistem memakai Supabase Storage.
                </p>
            </div>
        </body>
        </html>
        """, 404

    return send_from_directory(
        app.config['UPLOAD_FOLDER'],
        safe_filename,
        as_attachment=False
    )

@app.route('/semua_izin')
def semua_izin():
    if 'user_id' not in session:
        return redirect('/login')

    user = User.query.get(session['user_id'])
    if user.role not in ['admin', 'hrd', 'direktur']:
        return redirect('/dashboard')

    status_filter = request.args.get('status', '').strip()
    jenis_filter = request.args.get('jenis', '').strip()
    search = request.args.get('search', '').strip()
    divisi_filter = request.args.get('divisi', '').strip()
    nama_filter = request.args.get('nama', '').strip()

    query = db.session.query(LeaveRequest, User).join(
        User, LeaveRequest.user_id == User.id
    )

    if status_filter:
        query = query.filter(LeaveRequest.status == status_filter)

    if jenis_filter:
        query = query.filter(LeaveRequest.jenis_izin == jenis_filter)

    if search:
        query = query.filter(LeaveRequest.alasan.ilike(f'%{search}%'))

    if divisi_filter:
        query = query.filter(User.divisi == divisi_filter)

    if nama_filter:
        query = query.filter(User.username.ilike(f'%{nama_filter}%'))

    data = query.order_by(LeaveRequest.created_at.desc()).limit(100).all()

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

# =========================
# EXPORT EXCEL IZIN
# =========================

def format_tanggal_excel(value, pakai_jam=False):
    if not value:
        return '-'

    try:
        if pakai_jam:
            return value.strftime('%d/%m/%Y %H:%M')
        return value.strftime('%d/%m/%Y')
    except:
        return str(value)


def buat_file_excel(rows, sheet_name, filename_prefix):
    df = pd.DataFrame(rows)

    if df.empty:
        df = pd.DataFrame([{
            'Info': 'Tidak ada data untuk diexport'
        }])

    output = io.BytesIO()

    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name=sheet_name)

        worksheet = writer.sheets[sheet_name]

        for column_cells in worksheet.columns:
            max_length = 0
            column_letter = column_cells[0].column_letter

            for cell in column_cells:
                try:
                    if cell.value:
                        max_length = max(max_length, len(str(cell.value)))
                except:
                    pass

            worksheet.column_dimensions[column_letter].width = max_length + 3

    output.seek(0)

    filename = f"{filename_prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"

    return send_file(
        output,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        as_attachment=True,
        download_name=filename
    )


@app.route('/export_excel')
def export_excel():
    if 'user_id' not in session:
        return redirect('/login')

    current_user = User.query.get(session['user_id'])

    if not current_user:
        session.clear()
        return redirect('/login')

    if current_user.role not in ['admin', 'hrd', 'direktur']:
        flash('Kamu tidak memiliki akses untuk export data izin.', 'danger')
        return redirect('/dashboard')

    status_filter = request.args.get('status', '').strip()
    jenis_filter = request.args.get('jenis', '').strip()
    search = request.args.get('search', '').strip()
    divisi_filter = request.args.get('divisi', '').strip()
    nama_filter = request.args.get('nama', '').strip()

    query = db.session.query(LeaveRequest, User).join(
        User, LeaveRequest.user_id == User.id
    )

    if status_filter:
        query = query.filter(LeaveRequest.status == status_filter)

    if jenis_filter:
        query = query.filter(LeaveRequest.jenis_izin == jenis_filter)

    if search:
        query = query.filter(LeaveRequest.alasan.ilike(f'%{search}%'))

    if divisi_filter:
        query = query.filter(User.divisi == divisi_filter)

    if nama_filter:
        query = query.filter(User.username.ilike(f'%{nama_filter}%'))

    data = query.order_by(LeaveRequest.created_at.desc()).all()

    rows = []

    for izin, pengaju in data:
        rows.append({
            'ID Izin': izin.id,
            'Nama Pengaju': pengaju.username if pengaju else '-',
            'Divisi': pengaju.divisi if pengaju else '-',
            'Jenis Izin': izin.jenis_izin,
            'Tanggal Mulai': format_tanggal_excel(izin.tanggal_mulai),
            'Tanggal Selesai': format_tanggal_excel(izin.tanggal_selesai),
            'Durasi': izin.durasi,
            'Alasan': izin.alasan,
            'Status': izin.status.upper() if izin.status else '-',
            'Tanggal Pengajuan': format_tanggal_excel(izin.created_at, pakai_jam=True),
            'File Surat Dokter': izin.file_surat if izin.file_surat else '-',
            'File Bukti Chat': izin.file_chat if izin.file_chat else '-'
        })

    return buat_file_excel(
        rows=rows,
        sheet_name='Data Izin',
        filename_prefix='Rekap_Izin'
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

from purchase_order.routes import po_bp
app.register_blueprint(po_bp, url_prefix='/purchase-order')

if __name__ == "__main__":
    app.run(debug=True)