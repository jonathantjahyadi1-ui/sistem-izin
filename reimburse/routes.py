from flask import Blueprint, request, render_template, redirect, url_for, flash, session, send_file
from extensions import db
from models import User
from .models import ReimburseRequest, ReimburseItem
from datetime import datetime, timedelta
from werkzeug.utils import secure_filename
from supabase import create_client
import os
import io
import pandas as pd
import mimetypes

reimburse_bp = Blueprint('reimburse', __name__, template_folder='../templates/reimburse')

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
SUPABASE_STORAGE_BUCKET = os.getenv("SUPABASE_STORAGE_BUCKET", "izin-files")

if not SUPABASE_URL:
    raise Exception("SUPABASE_URL belum diset di environment!")

if not SUPABASE_SERVICE_ROLE_KEY:
    raise Exception("SUPABASE_SERVICE_ROLE_KEY belum diset di environment!")

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

ALLOWED_REIMBURSE_EXTENSIONS = {'jpg', 'jpeg', 'png'}


def allowed_reimburse_file(filename):
    return (
        '.' in filename and
        filename.rsplit('.', 1)[1].lower() in ALLOWED_REIMBURSE_EXTENSIONS
    )


def upload_reimburse_to_supabase(file, folder):
    if not file or file.filename == '':
        return None

    if not allowed_reimburse_file(file.filename):
        raise ValueError("Format file reimburse harus JPG, JPEG, atau PNG.")

    original_filename = secure_filename(file.filename)
    timestamp = datetime.now().strftime('%Y%m%d%H%M%S%f')
    filename = f"{folder}_{timestamp}_{original_filename}"

    storage_path = f"reimburse/{folder}/{filename}"

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


def create_reimburse_signed_url(storage_path, expires_in=300):
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


def reimburse_file_url(path):
    if not path:
        return ""

    # File baru dari Supabase Storage
    if path.startswith("reimburse/"):
        return url_for("reimburse.reimburse_file", storage_path=path)

    # Fallback untuk file lama yang masih tersimpan di folder uploads
    return f"/uploads/{path}"


@reimburse_bp.app_context_processor
def reimburse_template_helpers():
    return dict(reimburse_file_url=reimburse_file_url)

def get_user(user_id):
    return db.session.get(User, user_id)

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

@reimburse_bp.route('/file/<path:storage_path>')
def reimburse_file(storage_path):
    if 'user_id' not in session:
        return redirect('/login')

    user = db.session.get(User, session['user_id'])
    if not user:
        return redirect('/login')

    if not storage_path.startswith("reimburse/"):
        flash("File tidak valid.", "danger")
        return redirect(url_for('reimburse.list_reimburse'))

    item = ReimburseItem.query.filter_by(receipt_photo=storage_path).first()

    if item:
        reimb = item.request
    else:
        reimb = ReimburseRequest.query.filter(
            (ReimburseRequest.payment_proof == storage_path) |
            (ReimburseRequest.receipt_photo == storage_path)
        ).first()

    if not reimb:
        flash("File tidak ditemukan di data reimburse.", "danger")
        return redirect(url_for('reimburse.list_reimburse'))

    if user.role not in ['admin', 'direktur', 'accounting'] and reimb.user_id != user.id:
        flash("Kamu tidak punya akses ke file ini.", "danger")
        return redirect(url_for('reimburse.list_reimburse'))

    signed_url = create_reimburse_signed_url(storage_path, expires_in=300)

    if not signed_url:
        flash("File tidak bisa dibuka.", "danger")
        return redirect(url_for('reimburse.list_reimburse'))

    return redirect(signed_url)

@reimburse_bp.route('/list')
def list_reimburse():
    if 'user_id' not in session:
        return redirect('/login')
    user = db.session.get(User, session['user_id'])
    if not user:
        return redirect('/login')

    now = datetime.utcnow()
    cutoff = now - timedelta(days=7)

    # Query dasar: hanya data yang belum diarsipkan (paid_at null atau <7 hari)
    query = ReimburseRequest.query.filter(
        (ReimburseRequest.paid_at == None) |
        (ReimburseRequest.paid_at >= cutoff)
    )

    # 🔒 PEMBATASAN AKSES: karyawan hanya melihat milik sendiri
    if user.role not in ['admin', 'direktur', 'accounting']:
        query = query.filter(ReimburseRequest.user_id == user.id)
    else:
        # 🔍 FILTER BERDASARKAN NAMA (hanya untuk role yang punya akses penuh)
        nama_filter = request.args.get('nama', '')
        if nama_filter:
            query = query.join(User, ReimburseRequest.user_id == User.id)
            query = query.filter(User.username.ilike(f'%{nama_filter}%'))

    query = query.order_by(ReimburseRequest.created_at.desc())
    data = query.all()

    # Kirim nama_filter ke template agar input filter tetap terisi
    nama_filter = request.args.get('nama', '')
    return render_template('reimburse/list.html', data=data, user=user, get_user=get_user, nama_filter=nama_filter)


@reimburse_bp.route('/archive')
def archive():
    if 'user_id' not in session:
        return redirect('/login')

    user = db.session.get(User, session['user_id'])
    if not user:
        return redirect('/login')

    now = datetime.utcnow()
    cutoff = now - timedelta(days=7)

    # Basis query: semua yang sudah dibayar & lewat 7 hari
    query = ReimburseRequest.query.filter(
        ReimburseRequest.paid_at != None,
        ReimburseRequest.paid_at < cutoff
    )

    # Batasi akses: karyawan hanya lihat punya sendiri
    if user.role not in ['admin', 'direktur', 'accounting']:
        query = query.filter(ReimburseRequest.user_id == user.id)

    data = query.order_by(ReimburseRequest.paid_at.desc()).all()

    return render_template(
        'reimburse/archive.html',
        data=data,
        user=user,
        get_user=get_user
    )

@reimburse_bp.route('/archive/export_excel')
def export_archive_excel():
    if 'user_id' not in session:
        return redirect('/login')

    user = db.session.get(User, session['user_id'])

    if not user:
        return redirect('/login')

    if user.role not in ['admin', 'direktur', 'accounting']:
        flash('Kamu tidak memiliki akses untuk mengunduh data arsip.', 'danger')
        return redirect(url_for('reimburse.archive'))

    now = datetime.utcnow()
    cutoff = now - timedelta(days=7)

    query = db.session.query(ReimburseRequest, User).join(
        User, ReimburseRequest.user_id == User.id
    ).filter(
        ReimburseRequest.paid_at != None,
        ReimburseRequest.paid_at < cutoff
    )

    reimbursements = query.order_by(ReimburseRequest.paid_at.desc()).all()

    rows = []

    for reimb, pengaju in reimbursements:
        if reimb.items:
            for item in reimb.items:
                rows.append({
                    'ID Reimburse': reimb.id,
                    'Nama Pengaju': pengaju.username if pengaju else '-',
                    'Divisi': pengaju.divisi if pengaju else '-',
                    'Tanggal Pengajuan': format_tanggal_excel(reimb.created_at, pakai_jam=True),
                    'Tanggal Dibayar': format_tanggal_excel(reimb.paid_at, pakai_jam=True),
                    'Status': 'Dibayar' if reimb.status == 'paid' else reimb.status,
                    'Nama Item': item.item_name,
                    'Harga': item.price,
                    'Qty': item.qty,
                    'Subtotal': item.price * item.qty,
                    'Total Reimburse': reimb.total_amount,
                    'File Nota': item.receipt_photo if item.receipt_photo else (reimb.receipt_photo if reimb.receipt_photo else '-'),
                    'Bukti Pembayaran': reimb.payment_proof if reimb.payment_proof else '-'
                })
        else:
            rows.append({
                'ID Reimburse': reimb.id,
                'Nama Pengaju': pengaju.username if pengaju else '-',
                'Divisi': pengaju.divisi if pengaju else '-',
                'Tanggal Pengajuan': format_tanggal_excel(reimb.created_at, pakai_jam=True),
                'Tanggal Dibayar': format_tanggal_excel(reimb.paid_at, pakai_jam=True),
                'Status': 'Dibayar' if reimb.status == 'paid' else reimb.status,
                'Nama Item': '-',
                'Harga': 0,
                'Qty': 0,
                'Subtotal': 0,
                'Total Reimburse': reimb.total_amount,
                'File Nota': reimb.receipt_photo if reimb.receipt_photo else '-',
                'Bukti Pembayaran': reimb.payment_proof if reimb.payment_proof else '-'
            })

    return buat_file_excel(
        rows=rows,
        sheet_name='Arsip Reimburse',
        filename_prefix='Arsip_Reimburse'
    )

@reimburse_bp.route('/detail/<int:id>', methods=['GET', 'POST'])
def detail(id):
    if 'user_id' not in session:
        return redirect('/login')

    user = db.session.get(User, session['user_id'])
    if not user:
        return redirect('/login')

    reimb = ReimburseRequest.query.get_or_404(id)

    if user.role not in ['admin', 'direktur', 'accounting'] and reimb.user_id != user.id:
        flash('Kamu tidak punya akses ke pengajuan ini.', 'danger')
        return redirect(url_for('reimburse.list_reimburse'))

    if request.method == 'POST':
        if user.role != 'direktur':
            flash('Hanya direktur yang bisa upload bukti pembayaran.', 'danger')
            return redirect(url_for('reimburse.detail', id=id))

        file = request.files.get('payment_proof')

        if file and file.filename != '':
            try:
                filename = upload_reimburse_to_supabase(file, "payment")
            except ValueError as e:
                flash(str(e), 'danger')
                return redirect(url_for('reimburse.detail', id=id))
            except Exception as e:
                print("ERROR UPLOAD BUKTI PEMBAYARAN:", repr(e))
                flash("Gagal upload bukti pembayaran.", "danger")
                return redirect(url_for('reimburse.detail', id=id))

            reimb.payment_proof = filename
            reimb.paid_at = datetime.utcnow()
            reimb.status = 'paid'
            db.session.commit()

            flash('Bukti pembayaran berhasil diunggah.', 'success')
            return redirect(url_for('reimburse.detail', id=id))

        flash('Bukti pembayaran wajib diupload.', 'danger')
        return redirect(url_for('reimburse.detail', id=id))

    return render_template(
        'reimburse/detail.html',
        reimb=reimb,
        user=user,
        get_user=get_user
    )

@reimburse_bp.route('/export_excel')
def export_excel():
    if 'user_id' not in session:
        return redirect('/login')

    user = db.session.get(User, session['user_id'])

    if not user:
        return redirect('/login')

    if user.role not in ['admin', 'direktur', 'accounting']:
        flash('Kamu tidak memiliki akses untuk mengunduh data reimburse.', 'danger')
        return redirect('/reimburse/list')

    nama_filter = request.args.get('nama', '').strip()

    now = datetime.utcnow()
    cutoff = now - timedelta(days=7)

    query = db.session.query(ReimburseRequest, User).join(
        User, ReimburseRequest.user_id == User.id
    ).filter(
        (ReimburseRequest.paid_at == None) |
        (ReimburseRequest.paid_at >= cutoff)
    )

    if nama_filter:
        query = query.filter(User.username.ilike(f'%{nama_filter}%'))

    reimbursements = query.order_by(ReimburseRequest.created_at.desc()).all()

    rows = []

    for reimb, pengaju in reimbursements:
        if reimb.items:
            for item in reimb.items:
                rows.append({
                    'ID Reimburse': reimb.id,
                    'Nama Pengaju': pengaju.username if pengaju else '-',
                    'Divisi': pengaju.divisi if pengaju else '-',
                    'Tanggal Pengajuan': format_tanggal_excel(reimb.created_at, pakai_jam=True),
                    'Status': 'Dibayar' if reimb.status == 'paid' else 'Menunggu Pembayaran',
                    'Nama Item': item.item_name,
                    'Harga': item.price,
                    'Qty': item.qty,
                    'Subtotal': item.price * item.qty,
                    'Total Reimburse': reimb.total_amount,
                    'Tanggal Dibayar': format_tanggal_excel(reimb.paid_at, pakai_jam=True),
                    'File Nota': item.receipt_photo if item.receipt_photo else (reimb.receipt_photo if reimb.receipt_photo else '-'),
                    'Bukti Pembayaran': reimb.payment_proof if reimb.payment_proof else '-'
                })
        else:
            rows.append({
                'ID Reimburse': reimb.id,
                'Nama Pengaju': pengaju.username if pengaju else '-',
                'Divisi': pengaju.divisi if pengaju else '-',
                'Tanggal Pengajuan': format_tanggal_excel(reimb.created_at, pakai_jam=True),
                'Status': 'Dibayar' if reimb.status == 'paid' else 'Menunggu Pembayaran',
                'Nama Item': '-',
                'Harga': 0,
                'Qty': 0,
                'Subtotal': 0,
                'Total Reimburse': reimb.total_amount,
                'Tanggal Dibayar': format_tanggal_excel(reimb.paid_at, pakai_jam=True),
                'File Nota': reimb.receipt_photo if reimb.receipt_photo else '-',
                'Bukti Pembayaran': reimb.payment_proof if reimb.payment_proof else '-'
            })

    return buat_file_excel(
        rows=rows,
        sheet_name='Data Reimburse',
        filename_prefix='Rekap_Reimburse_Aktif'
    )


@reimburse_bp.route('/submit', methods=['GET', 'POST'])
def submit():
    if 'user_id' not in session:
        return redirect('/login')

    user = db.session.get(User, session['user_id'])

    if not user:
        return redirect('/login')

    if request.method == 'POST':
        item_names = request.form.getlist('item_name[]')
        prices = request.form.getlist('price[]')
        qtys = request.form.getlist('qty[]')
        receipts = request.files.getlist('receipt[]')

        if not item_names:
            flash('Minimal satu item harus diisi.', 'danger')
            return redirect(url_for('reimburse.submit'))

        total = 0
        items = []

        for index, name in enumerate(item_names):
            name = name.strip()

            if not name:
                continue

            try:
                price = int(prices[index]) if index < len(prices) and prices[index] else 0
                qty = int(qtys[index]) if index < len(qtys) and qtys[index] else 1
            except ValueError:
                flash('Harga dan qty harus berupa angka.', 'danger')
                return redirect(url_for('reimburse.submit'))

            if price < 0 or qty <= 0:
                flash('Harga tidak boleh minus dan qty minimal 1.', 'danger')
                return redirect(url_for('reimburse.submit'))

            receipt_file = receipts[index] if index < len(receipts) else None
            receipt_filename = None

            if receipt_file and receipt_file.filename != '':
                try:
                    receipt_filename = upload_reimburse_to_supabase(receipt_file, "receipt")
                except ValueError as e:
                    flash(str(e), 'danger')
                    return redirect(url_for('reimburse.submit'))
                except Exception as e:
                    print("ERROR UPLOAD NOTA REIMBURSE:", repr(e))
                    flash(f'Gagal upload nota untuk item "{name}".', 'danger')
                    return redirect(url_for('reimburse.submit'))
            else:
                flash(f'Foto nota untuk item "{name}" wajib diupload.', 'danger')
                return redirect(url_for('reimburse.submit'))

            total += price * qty

            items.append({
                'item_name': name,
                'price': price,
                'qty': qty,
                'receipt_photo': receipt_filename
            })

        if not items:
            flash('Minimal satu item valid harus diisi.', 'danger')
            return redirect(url_for('reimburse.submit'))

        reimb = ReimburseRequest(
            user_id=user.id,
            total_amount=total,
            receipt_photo=None
        )

        db.session.add(reimb)
        db.session.flush()

        for item in items:
            db.session.add(ReimburseItem(
                reimburse_id=reimb.id,
                item_name=item['item_name'],
                price=item['price'],
                qty=item['qty'],
                receipt_photo=item['receipt_photo']
            ))

        db.session.commit()

        flash('Pengajuan reimburse berhasil!', 'success')
        return redirect(url_for('reimburse.list_reimburse'))

    return render_template('reimburse/form.html', user=user)
