from dotenv import load_dotenv
import os
import pandas as pd
import io
from datetime import datetime, timezone
from flask import Flask, request, session, render_template, redirect, url_for, flash, send_file, send_from_directory
from werkzeug.utils import secure_filename
from supabase import create_client
import mimetypes
from extensions import db
from models import User, LeaveRequest
from overtime.models import OvertimeRequest
from meeting_room.models import MeetingRoom, RoomBooking
from werkzeug.security import generate_password_hash, check_password_hash
from calendar import monthrange
from datetime import date, timedelta
from sqlalchemy import case, func, extract
from zoneinfo import ZoneInfo

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

if uri.startswith("postgresql://") and "sslmode" not in uri:
    if "?" in uri:
        uri += "&sslmode=require"
    else:
        uri += "?sslmode=require"

app.config['SQLALCHEMY_DATABASE_URI'] = uri
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
secret_key = os.getenv("SECRET_KEY")
if not secret_key and os.getenv("RENDER"):
    raise Exception("SECRET_KEY belum diset di environment Render!")
app.config['SECRET_KEY'] = secret_key or "development-only-change-me"

engine_options = {
    "pool_pre_ping": True,
    "pool_recycle": 300,
}
if uri.startswith("postgresql://"):
    engine_options.update({
        "pool_size": 5,
        "max_overflow": 5,
        "pool_timeout": 30,
    })
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = engine_options

db.init_app(app)

# ==================
# FUNGSI BANTU CUTI
# ==================
JAKARTA_TZ = ZoneInfo("Asia/Jakarta")
DIVISI_LIST = [
    'Marketing', 'Operational', 'Hostlive', 'Creative',
    'Accounting', 'IT Support', 'HRD'
]


def tanggal_hari_ini_jakarta():
    """Tanggal bisnis aplikasi. Render memakai UTC, kantor memakai WIB."""
    return datetime.now(JAKARTA_TZ).date()


NAMA_BULAN_INDONESIA = (
    'Jan', 'Feb', 'Mar', 'Apr', 'Mei', 'Jun',
    'Jul', 'Agu', 'Sep', 'Okt', 'Nov', 'Des'
)


def ubah_ke_waktu_jakarta(value):
    """Ubah datetime UTC dari database menjadi waktu Asia/Jakarta."""
    if not isinstance(value, datetime):
        return value

    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)

    return value.astimezone(JAKARTA_TZ)


def format_tanggal_indonesia(value):
    """Format tanggal untuk tampilan, tanpa menyertakan jam."""
    if not value:
        return '-'

    value = ubah_ke_waktu_jakarta(value)
    return f'{value.day:02d} {NAMA_BULAN_INDONESIA[value.month - 1]} {value.year}'


app.jinja_env.filters['tanggal_id'] = format_tanggal_indonesia


def tambah_bulan(tanggal_awal, jumlah_bulan):
    """
    Menambahkan bulan dengan aman.
    Contoh: 31 Januari + 1 bulan = 28/29 Februari.
    """
    bulan = tanggal_awal.month - 1 + jumlah_bulan
    tahun = tanggal_awal.year + bulan // 12
    bulan = bulan % 12 + 1
    hari = min(tanggal_awal.day, monthrange(tahun, bulan)[1])

    return date(tahun, bulan, hari)


def awal_bulan(tanggal):
    """Mengubah tanggal apa pun menjadi tanggal 1 pada bulan yang sama."""
    if not tanggal:
        return None
    return date(tanggal.year, tanggal.month, 1)


def tanggal_accrual_pertama(join_date):
    """Tanggal 1 pertama saat masa kerja sudah genap enam bulan."""
    eligible_date = tambah_bulan(join_date, 6)
    if eligible_date.day == 1:
        return eligible_date
    return tambah_bulan(awal_bulan(eligible_date), 1)


def tanggal_accrual_berikutnya(user):
    """Jadwal +1 berikutnya berdasarkan join date dan marker terakhir."""
    if not user or user.role != 'karyawan' or not user.join_date:
        return None

    tanggal_pertama = tanggal_accrual_pertama(user.join_date)
    if not user.last_cuti_accrual_date:
        return tanggal_pertama

    bulan_terakhir = awal_bulan(user.last_cuti_accrual_date)
    return max(tanggal_pertama, tambah_bulan(bulan_terakhir, 1))


def jumlah_siklus_accrual(join_date, today=None):
    """Jumlah jadwal +1 sejak karyawan eligible sampai bulan berjalan."""
    if not join_date:
        return 0

    today = today or tanggal_hari_ini_jakarta()
    tanggal_pertama = tanggal_accrual_pertama(join_date)
    if today < tanggal_pertama:
        return 0

    return (
        (today.year - tanggal_pertama.year) * 12
        + today.month - tanggal_pertama.month
        + 1
    )


def informasi_accrual_cuti(user, today=None):
    """Informasi jadwal accrual untuk ditampilkan kepada HRD/admin."""
    today = today or tanggal_hari_ini_jakarta()
    info = {
        'status': 'non_karyawan',
        'tanggal_pertama': None,
        'tanggal_berikutnya': None,
        'tanggal_diproses_sampai': None,
        'jumlah_siklus': 0,
    }

    if not user:
        return info

    if user.last_cuti_accrual_date:
        info['tanggal_diproses_sampai'] = awal_bulan(
            user.last_cuti_accrual_date
        )

    if user.role != 'karyawan':
        return info

    if not user.join_date:
        info['status'] = 'join_date_kosong'
        return info

    tanggal_pertama = tanggal_accrual_pertama(user.join_date)
    info['tanggal_pertama'] = tanggal_pertama
    info['tanggal_berikutnya'] = tanggal_accrual_berikutnya(user)
    info['jumlah_siklus'] = jumlah_siklus_accrual(user.join_date, today=today)
    info['status'] = 'menunggu_6_bulan' if today < tanggal_pertama else 'aktif'
    return info


def get_total_cuti_approved_this_year(user_id, tahun=None):
    """Total durasi cuti approved pada tahun berjalan."""
    if tahun is None:
        tahun = tanggal_hari_ini_jakarta().year

    total = db.session.query(func.sum(LeaveRequest.durasi)) \
        .filter(LeaveRequest.user_id == user_id) \
        .filter(LeaveRequest.jenis_izin == 'cuti') \
        .filter(LeaveRequest.status == 'approved') \
        .filter(extract('year', LeaveRequest.tanggal_mulai) == tahun) \
        .scalar() or 0

    return total


def proses_jatah_cuti_otomatis(user, today=None):
    """
    Menambahkan +1 ke kuota_cuti jika karyawan sudah eligible.

    Aturan:
    - join_date kosong: tidak tambah
    - belum 6 bulan kerja: tidak tambah
    - setelah 6 bulan: tambah 1 pada tanggal 1 berikutnya
    - setiap tanggal 1 berikutnya: tambah 1
    - kuota_cuti tetap bisa diedit manual oleh HRD/admin

    Fungsi ini hanya mengubah object SQLAlchemy. Commit dilakukan satu kali oleh
    proses_semua_jatah_cuti(), bukan berulang kali untuk setiap karyawan.
    """
    if not user or user.role != 'karyawan' or not user.join_date:
        return 0

    today = today or tanggal_hari_ini_jakarta()
    tanggal_mulai_dapat_cuti = tanggal_accrual_pertama(user.join_date)

    # Marker dari versi lama mungkin tersimpan pada tanggal 8, 15, dan
    # tanggal lainnya. Karena kebijakan baru selalu tanggal 1, marker tersebut
    # dinormalisasi tanpa mengubah kuota. Perhitungan lama juga sebenarnya
    # sudah membaca marker per bulan, jadi langkah ini tidak membuat dobel.
    if user.last_cuti_accrual_date:
        user.last_cuti_accrual_date = awal_bulan(
            user.last_cuti_accrual_date
        )

    if today < tanggal_mulai_dapat_cuti:
        return 0

    if user.kuota_cuti is None:
        user.kuota_cuti = 0

    if user.last_cuti_accrual_date is None:
        # Data lama yang sudah mempunyai kuota dianggap sudah pernah
        # diinisialisasi. Ini mencegah kuota lama ditambah ulang sekaligus.
        if user.kuota_cuti > 0:
            user.last_cuti_accrual_date = awal_bulan(today)
            return 0

        tanggal_jatah_berikutnya = tanggal_mulai_dapat_cuti
    else:
        bulan_accrual_terakhir = awal_bulan(
            user.last_cuti_accrual_date
        )
        tanggal_jatah_berikutnya = max(
            tanggal_mulai_dapat_cuti,
            tambah_bulan(bulan_accrual_terakhir, 1)
        )

    if tanggal_jatah_berikutnya > today:
        return 0

    total_ditambahkan = (
        (today.year - tanggal_jatah_berikutnya.year) * 12
        + today.month - tanggal_jatah_berikutnya.month
        + 1
    )
    user.kuota_cuti += total_ditambahkan
    user.last_cuti_accrual_date = tambah_bulan(
        tanggal_jatah_berikutnya,
        total_ditambahkan - 1
    )
    return total_ditambahkan


def proses_semua_jatah_cuti(today=None):
    """Proses seluruh accrual dengan satu query dan satu commit."""
    today = today or tanggal_hari_ini_jakarta()
    users = User.query.filter(
        User.role == 'karyawan',
        User.join_date.isnot(None)
    ).with_for_update().all()

    total_ditambahkan = 0
    for user in users:
        total_ditambahkan += proses_jatah_cuti_otomatis(user, today=today)

    db.session.commit()
    return total_ditambahkan


_tanggal_pengecekan_accrual = None


def jalankan_accrual_harian_jika_perlu():
    """Fallback aman bila cron bulanan belum dipasang di Render."""
    global _tanggal_pengecekan_accrual
    today = tanggal_hari_ini_jakarta()
    if _tanggal_pengecekan_accrual == today:
        return 0

    try:
        total = proses_semua_jatah_cuti(today=today)
        _tanggal_pengecekan_accrual = today
        return total
    except Exception:
        db.session.rollback()
        app.logger.exception("Proses accrual cuti gagal")
        return 0


def get_sisa_cuti(user):
    """
    Sisa cuti = kuota_cuti manual/hasil auto - total cuti approved tahun ini.
    """
    if not user:
        return 0

    total_dipakai = get_total_cuti_approved_this_year(user.id)
    return (user.kuota_cuti or 0) - total_dipakai


def get_total_cuti_approved_semua_user(tahun=None):
    """Total cuti approved per karyawan dalam satu query."""
    tahun = tahun or tanggal_hari_ini_jakarta().year
    rows = db.session.query(
        LeaveRequest.user_id,
        func.coalesce(func.sum(LeaveRequest.durasi), 0)
    ).filter(
        LeaveRequest.jenis_izin == 'cuti',
        LeaveRequest.status == 'approved',
        extract('year', LeaveRequest.tanggal_mulai) == tahun
    ).group_by(LeaveRequest.user_id).all()
    return {user_id: int(total or 0) for user_id, total in rows}

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

def _tanggal_dalam_rentang(tanggal_mulai, tanggal_selesai, mulai, selesai):
    """Menghasilkan tanggal yang sudah dipotong ke periode laporan."""
    awal = max(tanggal_mulai, mulai)
    akhir = min(tanggal_selesai, selesai)
    for offset in range((akhir - awal).days + 1):
        yield awal + timedelta(days=offset)


def get_rekap_kehadiran(tahun=None, bulan=None, divisi_filter=None):
    """Ambil rekap tabel dan grafik dengan dua query database saja."""
    today = tanggal_hari_ini_jakarta()
    tahun = tahun or today.year
    bulan = bulan or today.month
    mulai = date(tahun, bulan, 1)
    selesai = date(tahun, bulan, monthrange(tahun, bulan)[1])

    users = db.session.query(
        User.id, User.username, User.divisi
    ).filter(User.role == 'karyawan').order_by(User.username.asc()).all()

    user_ids = [u.id for u in users]
    izin_rows = []
    if user_ids:
        izin_rows = db.session.query(
            LeaveRequest.user_id,
            LeaveRequest.jenis_izin,
            LeaveRequest.tanggal_mulai,
            LeaveRequest.tanggal_selesai
        ).filter(
            LeaveRequest.user_id.in_(user_ids),
            LeaveRequest.status == 'approved',
            LeaveRequest.tanggal_mulai <= selesai,
            LeaveRequest.tanggal_selesai >= mulai
        ).all()

    tanggal_per_user = {
        user_id: {'sakit': set(), 'izin_lain': set(), 'cuti': set(), 'semua': set()}
        for user_id in user_ids
    }
    for izin in izin_rows:
        if not izin.tanggal_mulai or not izin.tanggal_selesai:
            continue
        tanggal = set(_tanggal_dalam_rentang(
            izin.tanggal_mulai, izin.tanggal_selesai, mulai, selesai
        ))
        tanggal_per_user[izin.user_id]['semua'].update(tanggal)
        if izin.jenis_izin in ('sakit', 'izin_lain', 'cuti'):
            tanggal_per_user[izin.user_id][izin.jenis_izin].update(tanggal)

    target_cache = {}
    data_semua = []
    for user in users:
        divisi = user.divisi or '-'
        if divisi not in target_cache:
            target_cache[divisi] = get_hari_kerja_dalam_bulan(tahun, bulan, divisi)
        target = target_cache[divisi]
        kumpulan = tanggal_per_user[user.id]
        hari_sakit = _hitung_hari_kerja_dari_set(kumpulan['sakit'], divisi)
        hari_izin = _hitung_hari_kerja_dari_set(kumpulan['izin_lain'], divisi)
        hari_cuti = _hitung_hari_kerja_dari_set(kumpulan['cuti'], divisi)
        semua_izin = _hitung_hari_kerja_dari_set(kumpulan['semua'], divisi)

        data_semua.append({
            'user_id': user.id,
            'username': user.username,
            'divisi': divisi,
            'target': target,
            'hari_sakit': hari_sakit,
            'hari_izin': hari_izin,
            'hari_cuti': hari_cuti,
            # Tabel lama memang hanya mengurangi sakit + izin_lain.
            'hadir': target - hari_sakit - hari_izin,
            # Grafik lama mengurangi semua jenis izin, termasuk cuti.
            '_hadir_statistik': target - semua_izin,
        })

    data_tabel = [
        item for item in data_semua
        if not divisi_filter or item['divisi'] == divisi_filter
    ]

    statistik = []
    for divisi in DIVISI_LIST:
        anggota = [item for item in data_semua if item['divisi'] == divisi]
        if not anggota:
            continue
        total_target = sum(item['target'] for item in anggota)
        total_hadir = sum(item['_hadir_statistik'] for item in anggota)
        statistik.append({
            'divisi': divisi,
            'rata_target': round(total_target / len(anggota), 1),
            'rata_hadir': round(total_hadir / len(anggota), 1),
            'persen': round((total_hadir / total_target) * 100 if total_target else 0, 1),
        })

    return data_tabel, statistik


def get_data_kehadiran_per_bulan(tahun=None, bulan=None, divisi_filter=None):
    return get_rekap_kehadiran(tahun, bulan, divisi_filter)[0]


def get_statistik_divisi(tahun=None, bulan=None):
    return get_rekap_kehadiran(tahun, bulan)[1]
# ==========================
# AUTO CREATE TABLE & SEEDER
# ==========================
def ensure_database_indexes():
    """Tambahkan index yang dibutuhkan query dashboard pada database lama."""
    statements = [
        'CREATE INDEX IF NOT EXISTS ix_user_role_divisi ON "user" (role, divisi)',
        ('CREATE INDEX IF NOT EXISTS ix_leave_user_kind_status_date '
         'ON leave_request (user_id, jenis_izin, status, tanggal_mulai)'),
        ('CREATE INDEX IF NOT EXISTS ix_leave_status_period '
         'ON leave_request (status, tanggal_mulai, tanggal_selesai)'),
        ('CREATE INDEX IF NOT EXISTS ix_leave_status_created '
         'ON leave_request (status, created_at)'),
        'CREATE INDEX IF NOT EXISTS ix_leave_created ON leave_request (created_at)',
    ]
    with db.engine.begin() as conn:
        for statement in statements:
            conn.execute(db.text(statement))


def seed_users_from_environment():
    """Buat akun awal hanya bila username dan password diberikan lewat env."""
    seed_definitions = [
        ('SEED_ADMIN_USERNAME', 'SEED_ADMIN_PASSWORD', 'admin', 'IT'),
        ('SEED_HRD_USERNAME', 'SEED_HRD_PASSWORD', 'hrd', 'HRD'),
        ('SEED_DIRECTOR_USERNAME', 'SEED_DIRECTOR_PASSWORD', 'direktur', 'Direksi'),
        ('SEED_ACCOUNTING_USERNAME', 'SEED_ACCOUNTING_PASSWORD', 'accounting', 'Accounting'),
    ]
    changed = False
    for username_key, password_key, role, divisi in seed_definitions:
        username = (os.getenv(username_key) or '').strip()
        password = os.getenv(password_key) or ''
        if not username or not password:
            continue
        if not User.query.filter(func.lower(User.username) == username.lower()).first():
            db.session.add(User(
                username=username,
                nama_lengkap=username,
                password=generate_password_hash(password),
                role=role,
                divisi=divisi,
            ))
            changed = True
    if changed:
        db.session.commit()


with app.app_context():
    db.create_all()

    inspector = db.inspect(db.engine)
    user_columns = [c['name'] for c in inspector.get_columns('user')]

    if 'nama_lengkap' not in user_columns:
        with db.engine.connect() as conn:
            conn.execute(db.text('ALTER TABLE "user" ADD COLUMN nama_lengkap VARCHAR(150)'))
            conn.execute(db.text("""
                UPDATE "user"
                SET nama_lengkap = username
                WHERE nama_lengkap IS NULL OR nama_lengkap = ''
            """))
            conn.commit()

    if 'tempat_lahir' not in user_columns:
        with db.engine.connect() as conn:
            conn.execute(db.text('ALTER TABLE "user" ADD COLUMN tempat_lahir VARCHAR(100)'))
            conn.commit()

    if 'tanggal_lahir' not in user_columns:
        with db.engine.connect() as conn:
            conn.execute(db.text('ALTER TABLE "user" ADD COLUMN tanggal_lahir DATE'))
            conn.commit()

    if 'join_date' not in user_columns:
        with db.engine.connect() as conn:
            conn.execute(db.text('ALTER TABLE "user" ADD COLUMN join_date DATE'))
            conn.commit()

    if 'kuota_cuti' not in user_columns:
        with db.engine.begin() as conn:
            conn.execute(db.text(
            'ALTER TABLE "user" ADD COLUMN kuota_cuti INTEGER DEFAULT 0'
        ))

    if 'last_cuti_accrual_date' not in user_columns:
        with db.engine.begin() as conn:
            conn.execute(db.text(
            'ALTER TABLE "user" ADD COLUMN last_cuti_accrual_date DATE'
        ))


    ensure_database_indexes()
    seed_users_from_environment()

def normalize_full_name(name):
    return " ".join((name or "").strip().split())


def is_valid_full_name(name):
    return len(name.split()) >= 2

def parse_date_or_none(value):
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%d").date()

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
        username = normalize_full_name(request.form.get('username'))
        password = request.form.get('password')
        divisi = request.form.get('divisi')

        if not is_valid_full_name(username):
            flash("Nama wajib menggunakan nama lengkap minimal 2 kata.", "danger")
            return redirect('/register')

        if User.query.filter(func.lower(User.username) == username.lower()).first():
            flash("Nama lengkap tersebut sudah terdaftar.", "danger")
            return redirect('/register')

        user = User(
            username=username,
            password=generate_password_hash(password),
            divisi=divisi
        )
        db.session.add(user)
        db.session.commit()

        flash("Registrasi berhasil. Silakan login.", "success")
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

    elif selected_system == 'overtime':
        session['active_system'] = 'overtime'
        return redirect('/overtime/list')

    elif selected_system == 'meeting_room':
        session['active_system'] = 'meeting_room'
        return redirect('/meeting-room/list')

    if session.get('active_system') == 'izin':
        return redirect('/dashboard')

    elif session.get('active_system') == 'overtime':
        return redirect('/overtime/list')

    elif session.get('active_system') == 'meeting_room':
        return redirect('/meeting-room/list')

    return render_template('main_dashboard.html', user=user)

@app.route('/change_system')
def change_system():
    session.pop('active_system', None)
    return redirect('/main_dashboard')

@app.route('/dashboard')
def dashboard():
    if 'user_id' not in session:
        return redirect('/login')

    # Maksimal satu kali per hari per worker. Penambahan tetap idempotent karena
    # setiap user dilindungi last_cuti_accrual_date dan row lock database.
    jalankan_accrual_harian_jika_perlu()

    user = User.query.get(session['user_id'])
    if not user:
        session.clear()
        return redirect('/login')

    if user.role in ['karyawan', 'accounting']:
        page = max(request.args.get('page', 1, type=int) or 1, 1)
        pagination = LeaveRequest.query.filter_by(user_id=user.id).order_by(
            LeaveRequest.created_at.desc()
        ).paginate(page=page, per_page=25, error_out=False)
        data = pagination.items
        ringkasan = db.session.query(
            func.count(LeaveRequest.id),
            func.coalesce(func.sum(case((LeaveRequest.status == 'pending', 1), else_=0)), 0),
            func.coalesce(func.sum(case((LeaveRequest.status == 'approved', 1), else_=0)), 0),
        ).filter(LeaveRequest.user_id == user.id).one()
        sisa_cuti = get_sisa_cuti(user)
        return render_template(
            'dashboard_user.html',
            data=data,
            user=user,
            sisa_cuti=sisa_cuti,
            pagination=pagination,
            total_izin=int(ringkasan[0] or 0),
            total_pending=int(ringkasan[1] or 0),
            total_approved=int(ringkasan[2] or 0),
        )

    # ADMIN / HRD / DIREKTUR
    divisi_filter = request.args.get('divisi', '')
    today = tanggal_hari_ini_jakarta()
    tahun_filter = request.args.get('tahun', today.year, type=int) or today.year
    bulan_filter = request.args.get('bulan', today.month, type=int) or today.month
    if not 1 <= bulan_filter <= 12:
        bulan_filter = today.month

    data_kehadiran, stat_divisi = get_rekap_kehadiran(
        tahun_filter,
        bulan_filter,
        divisi_filter or None,
    )

    data_izin = db.session.query(
        LeaveRequest,
        User.username.label('pengaju_username')
    ).outerjoin(User, LeaveRequest.user_id == User.id).order_by(
        LeaveRequest.created_at.desc()
    ).limit(10).all()
    sisa_cuti_pribadi = get_sisa_cuti(user)
    ringkasan = db.session.query(
        func.count(LeaveRequest.id),
        func.coalesce(func.sum(case((LeaveRequest.status == 'pending', 1), else_=0)), 0),
        func.coalesce(func.sum(case((LeaveRequest.status == 'approved', 1), else_=0)), 0),
        func.coalesce(func.sum(case((LeaveRequest.status == 'rejected', 1), else_=0)), 0),
    ).one()
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
        total=int(ringkasan[0] or 0),
        pending=int(ringkasan[1] or 0),
        approved=int(ringkasan[2] or 0),
        rejected=int(ringkasan[3] or 0),
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

ALLOWED_IMAGE_EXTENSIONS = {'jpg', 'jpeg', 'png', 'pdf'}
ALLOWED_DOCUMENT_EXTENSIONS = {'jpg', 'jpeg', 'png', 'pdf'}

app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024


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

    # File baru dari Supabase Storage
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

    page = max(request.args.get('page', 1, type=int) or 1, 1)
    pagination = query.order_by(LeaveRequest.created_at.desc()).paginate(
        page=page,
        per_page=20,
        error_out=False
    )
    data = pagination.items

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
        divisi_list=divisi_list,
        pagination=pagination
    )

@app.route('/manage_users')
def manage_users():
    if 'user_id' not in session:
        return redirect('/login')

    user = User.query.get(session['user_id'])
    if not user or user.role not in ['admin', 'hrd']:
        return redirect('/dashboard')

    users = User.query.order_by(User.created_at.desc()).all()
    return render_template('manage_users.html', users=users, current_user=user)

@app.route('/add_user', methods=['POST'])
def add_user():
    if 'user_id' not in session:
        return redirect('/login')

    current_user = User.query.get(session['user_id'])
    if not current_user or current_user.role not in ['admin', 'hrd']:
        return redirect('/dashboard')

    username = request.form['username'].strip()
    nama_lengkap = request.form.get('nama_lengkap', '').strip()
    tempat_lahir = request.form.get('tempat_lahir', '').strip()
    tanggal_lahir = parse_date_or_none(request.form.get('tanggal_lahir'))
    join_date = parse_date_or_none(request.form.get('join_date'))
    password = request.form['password']
    role = request.form['role']
    divisi = request.form['divisi']

    if not is_valid_full_name(username):
        flash('Nama wajib menggunakan nama lengkap minimal 2 kata.', 'danger')
        return redirect('/manage_users')

    if User.query.filter(func.lower(User.username) == username.lower()).first():
        flash('Nama lengkap tersebut sudah ada!', 'danger')
        return redirect('/manage_users')

    new_user = User(
        username=username,
        nama_lengkap=nama_lengkap if nama_lengkap else username,
        tempat_lahir=tempat_lahir,
        tanggal_lahir=tanggal_lahir,
        join_date=join_date,
        password=generate_password_hash(password),
        role=role,
        divisi=divisi
)

    db.session.add(new_user)
    db.session.commit()

    flash('User berhasil ditambahkan!', 'success')
    return redirect('/manage_users')

@app.route('/update_user/<int:id>', methods=['POST'])
def update_user(id):
    if 'user_id' not in session:
        return redirect('/login')

    current_user = User.query.get(session['user_id'])
    if not current_user or current_user.role not in ['admin', 'hrd']:
        return redirect('/dashboard')

    user = User.query.get(id)
    if not user:
        flash('User tidak ditemukan.', 'danger')
        return redirect('/manage_users')

    join_date_lama = user.join_date
    role_lama = user.role

    username_input = request.form.get('username') or request.form.get('nama_lengkap')
    username = normalize_full_name(username_input)

    if not is_valid_full_name(username):
        flash('Nama wajib menggunakan nama lengkap minimal 2 kata.', 'danger')
        return redirect('/manage_users')

    duplicate = User.query.filter(
        func.lower(User.username) == username.lower(),
        User.id != id
    ).first()

    if duplicate:
        flash('Nama lengkap tersebut sudah digunakan user lain.', 'danger')
        return redirect('/manage_users')

    user.username = username
    user.nama_lengkap = request.form.get('nama_lengkap', '').strip() or username
    user.tempat_lahir = request.form.get('tempat_lahir', '').strip()
    user.tanggal_lahir = parse_date_or_none(request.form.get('tanggal_lahir'))
    user.join_date = parse_date_or_none(request.form.get('join_date'))
    user.divisi = request.form.get('divisi')

    if current_user.role == 'admin':
        role = request.form.get('role')
        if role:
            user.role = role

    # Saat join date/role diubah, rapikan marker lama dan langsung sinkronkan
    # jadwal. Kuota manual tidak direset supaya koreksi HRD tetap aman.
    if join_date_lama != user.join_date or role_lama != user.role:
        if not user.join_date:
            user.last_cuti_accrual_date = None
        elif user.last_cuti_accrual_date:
            user.last_cuti_accrual_date = awal_bulan(
                user.last_cuti_accrual_date
            )

            # Marker sebelum tanggal pertama bukan marker accrual yang valid.
            if (
                user.role == 'karyawan'
                and user.last_cuti_accrual_date
                < tanggal_accrual_pertama(user.join_date)
            ):
                user.last_cuti_accrual_date = None

        proses_jatah_cuti_otomatis(
            user,
            today=tanggal_hari_ini_jakarta()
        )

    db.session.commit()

    if user.id == current_user.id:
        session['role'] = user.role

    flash('Data user berhasil diperbarui!', 'success')
    return redirect('/manage_users')

@app.route('/reset_password/<int:id>', methods=['POST'])
def reset_password(id):
    if 'user_id' not in session:
        return redirect('/login')

    current_user = User.query.get(session['user_id'])

    # Hanya admin yang boleh reset password
    if not current_user or current_user.role != 'admin':
        flash('Hanya admin yang bisa reset password.', 'danger')
        return redirect('/dashboard')

    user = User.query.get(id)

    if not user:
        flash('User tidak ditemukan.', 'danger')
        return redirect('/manage_users')

    new_password = request.form.get('new_password', '').strip()

    if not new_password:
        flash('Password baru wajib diisi.', 'danger')
        return redirect('/manage_users')

    if len(new_password) < 6:
        flash('Password baru minimal 6 karakter.', 'danger')
        return redirect('/manage_users')

    user.password = generate_password_hash(new_password)
    db.session.commit()

    flash(f'Password untuk {user.username} berhasil direset.', 'success')
    return redirect('/manage_users')
# =========================
# EXPORT EXCEL IZIN
# =========================

def format_tanggal_excel(value, pakai_jam=False):
    if not value:
        return '-'

    try:
        value = ubah_ke_waktu_jakarta(value)
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
            'Tanggal Pengajuan': format_tanggal_excel(izin.created_at),
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
    data = db.session.query(LeaveRequest, User).outerjoin(
        User, LeaveRequest.user_id == User.id
    ).filter(LeaveRequest.status == 'pending').order_by(
        LeaveRequest.created_at.asc()
    ).all()
    return render_template('approval.html', data=data, user=user)

@app.route('/logout')
def logout_view():
    session.clear()
    return redirect('/login')

@app.route('/approve/<int:id>', methods=['POST'])
def approve(id):
    if 'user_id' not in session:
        return redirect('/login')

    jalankan_accrual_harian_jika_perlu()
    current_user = User.query.get(session['user_id'])
    if not current_user or current_user.role not in ['admin', 'hrd', 'direktur']:
        flash('Kamu tidak memiliki akses untuk approve izin.', 'danger')
        return redirect('/dashboard')

    izin = LeaveRequest.query.get(id)
    if not izin:
        flash('Izin tidak ditemukan.', 'danger')
        return redirect(request.referrer or '/dashboard')

    if izin.status != 'pending':
        flash('Izin ini sudah diproses sebelumnya.', 'warning')
        return redirect(request.referrer or '/dashboard')

    pengaju = User.query.get(izin.user_id)

    sisa_sebelum = None
    sisa_setelah = None

    if izin.jenis_izin == 'cuti' and pengaju:
        sisa_sebelum = get_sisa_cuti(pengaju)
        sisa_setelah = sisa_sebelum - izin.durasi

    izin.status = 'approved'
    db.session.commit()

    if izin.jenis_izin == 'cuti' and pengaju:
        if sisa_setelah < 0:
            flash(
                f'Cuti berhasil di-approve. Saldo cuti {pengaju.username} sekarang {sisa_setelah} hari.',
                'warning'
            )
        else:
            flash(
                f'Cuti berhasil di-approve. Sisa cuti {pengaju.username} sekarang {sisa_setelah} hari.',
                'success'
            )
    else:
        flash('Izin berhasil di-approve.', 'success')

    return redirect(request.referrer or '/dashboard')

@app.route('/reject/<int:id>', methods=['POST'])
def reject(id):
    if 'user_id' not in session:
        return redirect('/login')

    current_user = User.query.get(session['user_id'])
    if not current_user or current_user.role not in ['admin', 'hrd', 'direktur']:
        flash('Kamu tidak memiliki akses untuk menolak izin.', 'danger')
        return redirect('/dashboard')

    izin = LeaveRequest.query.get(id)
    if not izin:
        flash('Izin tidak ditemukan.', 'danger')
        return redirect(request.referrer or '/dashboard')
    if izin.status != 'pending':
        flash('Izin ini sudah diproses sebelumnya.', 'warning')
        return redirect(request.referrer or '/dashboard')

    izin.status = 'rejected'
    db.session.commit()
    flash('Izin ditolak.', 'warning')
    return redirect(request.referrer or '/dashboard')

# =========================
# ROUTE MANAJEMEN KUOTA CUTI
# =========================
@app.route('/manage_quota')
def manage_quota():
    if 'user_id' not in session:
        return redirect('/login')

    jalankan_accrual_harian_jika_perlu()
    user = User.query.get(session['user_id'])
    if not user or user.role not in ['admin', 'hrd']:
        return redirect('/dashboard')

    users = User.query.filter(User.role != 'direktur').order_by(User.username.asc()).all()
    cuti_dipakai = get_total_cuti_approved_semua_user()

    user_data = []
    today = tanggal_hari_ini_jakarta()
    for u in users:
        kuota = u.kuota_cuti or 0
        info_accrual = informasi_accrual_cuti(u, today=today)
        user_data.append({
            'id': u.id,
            'username': u.username,
            'divisi': u.divisi,
            'role': u.role,
            'join_date': u.join_date,
            'kuota': kuota,
            'sisa': kuota - cuti_dipakai.get(u.id, 0),
            'status_accrual': info_accrual['status'],
            'tanggal_accrual_pertama': info_accrual['tanggal_pertama'],
            'tanggal_accrual_berikutnya': info_accrual['tanggal_berikutnya'],
            'jumlah_siklus_accrual': info_accrual['jumlah_siklus'],
            'last_cuti_accrual_date': info_accrual['tanggal_diproses_sampai']
        })

    return render_template('manage_quota.html', users=user_data, current_user=user)

@app.route('/update_quota/<int:user_id>', methods=['POST'])
def update_quota(user_id):
    if 'user_id' not in session:
        return redirect('/login')
    current = User.query.get(session['user_id'])
    if not current or current.role not in ['admin', 'hrd']:
        return redirect('/dashboard')

    try:
        new_kuota = int(request.form['kuota'])
    except (KeyError, TypeError, ValueError):
        flash('Kuota cuti harus berupa angka.', 'danger')
        return redirect('/manage_quota')

    user = User.query.get(user_id)
    if user:
        user.kuota_cuti = new_kuota
        db.session.commit()
        flash(f'Kuota cuti {user.username} diperbarui menjadi {new_kuota} hari.', 'success')
    return redirect('/manage_quota')


@app.cli.command('accrue-cuti')
def accrue_cuti_command():
    """Perintah opsional untuk Render Cron Job bulanan."""
    total = proses_semua_jatah_cuti()
    print(f'Accrual selesai. Total tambahan: {total} hari.')


@app.cli.command('normalize-cuti-dates')
def normalize_cuti_dates_command():
    """Rapikan marker tanggal lama menjadi tanggal 1 tanpa mengubah kuota."""
    users = User.query.filter(
        User.last_cuti_accrual_date.isnot(None)
    ).with_for_update().all()

    total_diperbaiki = 0
    for user in users:
        tanggal_normal = awal_bulan(user.last_cuti_accrual_date)
        if tanggal_normal != user.last_cuti_accrual_date:
            user.last_cuti_accrual_date = tanggal_normal
            total_diperbaiki += 1

    db.session.commit()
    print(
        f'Normalisasi selesai. {total_diperbaiki} tanggal diperbaiki; '
        'kuota tidak diubah.'
    )


@app.cli.command('audit-cuti')
def audit_cuti_command():
    """Laporan baca-saja untuk memeriksa jadwal accrual seluruh user."""
    today = tanggal_hari_ini_jakarta()
    users = User.query.order_by(User.username.asc()).all()

    print(
        'ID | Nama | Role | Join date | Mulai auto | Diproses s.d. | '
        'Auto berikutnya | Siklus sejak join | Kuota'
    )
    for user in users:
        info = informasi_accrual_cuti(user, today=today)

        def fmt(value):
            return value.strftime('%d/%m/%Y') if value else '-'

        print(
            f'{user.id} | {user.username} | {user.role} | '
            f'{fmt(user.join_date)} | {fmt(info["tanggal_pertama"])} | '
            f'{fmt(info["tanggal_diproses_sampai"])} | '
            f'{fmt(info["tanggal_berikutnya"])} | {info["jumlah_siklus"]} | '
            f'{user.kuota_cuti or 0}'
        )

@app.route('/uploads/<filename>')
def uploaded_file(filename):
    from flask import send_from_directory
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)


from overtime.routes import overtime_bp
app.register_blueprint(overtime_bp, url_prefix='/overtime')

from meeting_room.routes import meeting_room_bp
app.register_blueprint(meeting_room_bp, url_prefix='/meeting-room')


if __name__ == "__main__":
    app.run(debug=True)
