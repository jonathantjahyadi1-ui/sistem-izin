from datetime import datetime, timedelta
import io
import mimetypes
import os

import pandas as pd
from flask import Blueprint, flash, redirect, render_template, request, send_file, session, url_for
from supabase import create_client
from werkzeug.utils import secure_filename

from extensions import db
from models import User
from .models import OvertimeRequest


overtime_bp = Blueprint(
    'overtime',
    __name__,
    template_folder='../templates'
)

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
SUPABASE_STORAGE_BUCKET = os.getenv("SUPABASE_STORAGE_BUCKET", "izin-files")

if not SUPABASE_URL:
    raise Exception("SUPABASE_URL belum diset di environment!")

if not SUPABASE_SERVICE_ROLE_KEY:
    raise Exception("SUPABASE_SERVICE_ROLE_KEY belum diset di environment!")

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

# Sesuai permintaan: approval lembur hanya oleh HRD.
# Admin tetap diberi akses melihat semua data dan export untuk kebutuhan kontrol sistem.
FULL_ACCESS_ROLES = ['admin', 'hrd']
APPROVER_ROLES = ['admin', 'hrd']

ALLOWED_OVERTIME_EXTENSIONS = {'jpg', 'jpeg', 'png', 'pdf'}


def get_user(user_id):
    return db.session.get(User, user_id)


def current_logged_user():
    if 'user_id' not in session:
        return None
    return db.session.get(User, session['user_id'])


def allowed_overtime_file(filename):
    return (
        '.' in filename and
        filename.rsplit('.', 1)[1].lower() in ALLOWED_OVERTIME_EXTENSIONS
    )


def upload_overtime_to_supabase(file, folder):
    if not file or file.filename == '':
        return None

    if not allowed_overtime_file(file.filename):
        raise ValueError("Format file harus JPG, JPEG, PNG, atau PDF.")

    original_filename = secure_filename(file.filename)
    timestamp = datetime.now().strftime('%Y%m%d%H%M%S%f')
    filename = f"{folder}_{timestamp}_{original_filename}"
    storage_path = f"overtime/{folder}/{filename}"

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




def delete_overtime_storage_files(paths):
    """Hapus file overtime lama dari Supabase setelah penggantian berhasil."""
    valid_paths = [
        path for path in paths
        if path and path.startswith('overtime/')
    ]

    if not valid_paths:
        return

    try:
        supabase.storage.from_(SUPABASE_STORAGE_BUCKET).remove(valid_paths)
    except Exception as e:
        # Gagal cleanup file lama tidak boleh membatalkan perubahan data.
        print('WARNING CLEANUP FILE OVERTIME:', repr(e))


def create_overtime_signed_url(storage_path, expires_in=300):
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


def overtime_file_url(path):
    if not path:
        return ""

    if path.startswith("overtime/"):
        return url_for("overtime.overtime_file", storage_path=path)

    # Fallback jika suatu saat ada file lama di folder uploads
    return f"/uploads/{path}"


@overtime_bp.app_context_processor
def overtime_template_helpers():
    return dict(
        overtime_file_url=overtime_file_url,
        format_duration_minutes=format_duration_minutes,
        is_pdf_file=is_pdf_file
    )


def is_pdf_file(path):
    return (path or '').lower().endswith('.pdf')


def parse_date(value):
    return datetime.strptime(value, '%Y-%m-%d').date()


def parse_time(value):
    return datetime.strptime(value, '%H:%M').time()


def calculate_duration_minutes(overtime_date, start_time, end_time):
    start_dt = datetime.combine(overtime_date, start_time)
    end_dt = datetime.combine(overtime_date, end_time)

    if end_dt == start_dt:
        raise ValueError('Jam selesai tidak boleh sama dengan jam mulai.')

    # Jika jam selesai lebih kecil dari jam mulai, dianggap lembur lewat tengah malam.
    # Contoh: 22:00 sampai 02:00 = 4 jam.
    if end_dt < start_dt:
        end_dt += timedelta(days=1)

    minutes = int((end_dt - start_dt).total_seconds() // 60)

    if minutes <= 0:
        raise ValueError('Durasi lembur tidak valid.')

    if minutes > 24 * 60:
        raise ValueError('Durasi lembur tidak boleh lebih dari 24 jam.')

    return minutes


def format_duration_minutes(minutes):
    minutes = int(minutes or 0)
    hours = minutes // 60
    mins = minutes % 60

    if hours and mins:
        return f"{hours} jam {mins} menit"

    if hours:
        return f"{hours} jam"

    return f"{mins} menit"


def format_tanggal_excel(value, pakai_jam=False):
    if not value:
        return '-'

    try:
        if pakai_jam:
            return value.strftime('%d/%m/%Y %H:%M')
        return value.strftime('%d/%m/%Y')
    except Exception:
        return str(value)


def format_jam_excel(value):
    if not value:
        return '-'

    try:
        return value.strftime('%H:%M')
    except Exception:
        return str(value)


def label_status_overtime(status):
    labels = {
        'pending': 'Menunggu Approval HRD',
        'approved': 'Disetujui HRD',
        'rejected': 'Ditolak HRD'
    }
    return labels.get(status, status or '-')


def build_overtime_query_for_user(user):
    query = OvertimeRequest.query

    if user.role not in FULL_ACCESS_ROLES:
        query = query.filter(OvertimeRequest.user_id == user.id)

    return query


def apply_overtime_filters(query):
    status_filter = request.args.get('status', '').strip()
    nama_filter = request.args.get('nama', '').strip()
    divisi_filter = request.args.get('divisi', '').strip()
    date_from = request.args.get('date_from', '').strip()
    date_to = request.args.get('date_to', '').strip()

    if status_filter:
        query = query.filter(OvertimeRequest.status == status_filter)

    if nama_filter:
        query = query.filter(OvertimeRequest.employee_name.ilike(f'%{nama_filter}%'))

    if divisi_filter:
        query = query.filter(OvertimeRequest.employee_divisi == divisi_filter)

    if date_from:
        query = query.filter(OvertimeRequest.overtime_date >= parse_date(date_from))

    if date_to:
        query = query.filter(OvertimeRequest.overtime_date <= parse_date(date_to))

    return query


def create_excel_response(rows, sheet_name, filename_prefix):
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
                except Exception:
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


@overtime_bp.route('/file/<path:storage_path>')
def overtime_file(storage_path):
    if 'user_id' not in session:
        return redirect('/login')

    user = current_logged_user()
    if not user:
        session.clear()
        return redirect('/login')

    if not storage_path.startswith('overtime/'):
        flash('File tidak valid.', 'danger')
        return redirect(url_for('overtime.list_overtime'))

    overtime = OvertimeRequest.query.filter(
        (OvertimeRequest.chat_proof == storage_path) |
        (OvertimeRequest.overtime_photo == storage_path) |
        (OvertimeRequest.attendance_photo == storage_path)
    ).first()

    if not overtime:
        flash('File tidak ditemukan di data lembur.', 'danger')
        return redirect(url_for('overtime.list_overtime'))

    if user.role not in FULL_ACCESS_ROLES and overtime.user_id != user.id:
        flash('Kamu tidak punya akses ke file ini.', 'danger')
        return redirect(url_for('overtime.list_overtime'))

    signed_url = create_overtime_signed_url(storage_path, expires_in=300)

    if not signed_url:
        flash('File tidak bisa dibuka.', 'danger')
        return redirect(url_for('overtime.list_overtime'))

    return redirect(signed_url)


@overtime_bp.route('/list')
def list_overtime():
    if 'user_id' not in session:
        return redirect('/login')

    user = current_logged_user()
    if not user:
        session.clear()
        return redirect('/login')

    query = build_overtime_query_for_user(user)
    query = apply_overtime_filters(query)
    data = query.order_by(OvertimeRequest.created_at.desc()).all()

    divisi_list = [
        row[0] for row in db.session.query(OvertimeRequest.employee_divisi)
        .distinct()
        .order_by(OvertimeRequest.employee_divisi.asc())
        .all()
        if row[0]
    ]

    return render_template(
        'overtime/list.html',
        data=data,
        user=user,
        get_user=get_user,
        status_filter=request.args.get('status', '').strip(),
        nama_filter=request.args.get('nama', '').strip(),
        divisi_filter=request.args.get('divisi', '').strip(),
        date_from=request.args.get('date_from', '').strip(),
        date_to=request.args.get('date_to', '').strip(),
        divisi_list=divisi_list,
        approver_roles=APPROVER_ROLES,
        full_access_roles=FULL_ACCESS_ROLES
    )


@overtime_bp.route('/submit', methods=['GET', 'POST'])
def submit_overtime():
    if 'user_id' not in session:
        return redirect('/login')

    user = current_logged_user()
    if not user:
        session.clear()
        return redirect('/login')

    if request.method == 'POST':
        try:
            overtime_date = parse_date(request.form.get('overtime_date', '').strip())
            start_time = parse_time(request.form.get('start_time', '').strip())
            end_time = parse_time(request.form.get('end_time', '').strip())
            duration_minutes = calculate_duration_minutes(overtime_date, start_time, end_time)
        except ValueError as e:
            flash(str(e), 'danger')
            return redirect(url_for('overtime.submit_overtime'))
        except Exception:
            flash('Tanggal dan jam lembur wajib diisi dengan format yang benar.', 'danger')
            return redirect(url_for('overtime.submit_overtime'))

        supervisor_name = request.form.get('supervisor_name', '').strip()
        work_description = request.form.get('work_description', '').strip()

        if not work_description:
            flash('Keterangan pekerjaan selama lembur wajib diisi.', 'danger')
            return redirect(url_for('overtime.submit_overtime'))

        chat_file = request.files.get('chat_proof')
        overtime_photo_file = request.files.get('overtime_photo')
        attendance_photo_file = request.files.get('attendance_photo')

        if not chat_file or chat_file.filename == '':
            flash('Bukti chat dengan SPV/Manager wajib diupload.', 'danger')
            return redirect(url_for('overtime.submit_overtime'))

        if not overtime_photo_file or overtime_photo_file.filename == '':
            flash('Foto bukti lembur wajib diupload.', 'danger')
            return redirect(url_for('overtime.submit_overtime'))

        if not attendance_photo_file or attendance_photo_file.filename == '':
            flash('Foto tap absen wajib diupload.', 'danger')
            return redirect(url_for('overtime.submit_overtime'))

        try:
            chat_proof = upload_overtime_to_supabase(chat_file, 'chat')
            overtime_photo = upload_overtime_to_supabase(overtime_photo_file, 'work_photo')
            attendance_photo = upload_overtime_to_supabase(attendance_photo_file, 'attendance')
        except ValueError as e:
            flash(str(e), 'danger')
            return redirect(url_for('overtime.submit_overtime'))
        except Exception as e:
            print('ERROR UPLOAD OVERTIME:', repr(e))
            flash('Gagal upload bukti lembur ke Supabase Storage.', 'danger')
            return redirect(url_for('overtime.submit_overtime'))

        employee_name = user.nama_lengkap or user.username
        employee_divisi = user.divisi or '-'

        overtime = OvertimeRequest(
            user_id=user.id,
            employee_name=employee_name,
            employee_divisi=employee_divisi,
            overtime_date=overtime_date,
            start_time=start_time,
            end_time=end_time,
            duration_minutes=duration_minutes,
            supervisor_name=supervisor_name,
            work_description=work_description,
            chat_proof=chat_proof,
            overtime_photo=overtime_photo,
            attendance_photo=attendance_photo,
            status='pending'
        )

        db.session.add(overtime)
        db.session.commit()

        flash(f'Pengajuan lembur berhasil dibuat. Total lembur: {format_duration_minutes(duration_minutes)}.', 'success')
        return redirect(url_for('overtime.list_overtime'))

    return render_template('overtime/form.html', user=user, editing=False)

@overtime_bp.route('/edit/<int:id>', methods=['GET', 'POST'])
def edit_overtime(id):
    if 'user_id' not in session:
        return redirect('/login')

    user = current_logged_user()
    if not user:
        session.clear()
        return redirect('/login')

    overtime = OvertimeRequest.query.get_or_404(id)

    # Hanya pengaju asli yang boleh mengedit pengajuan miliknya sendiri.
    if overtime.user_id != user.id:
        flash('Kamu hanya bisa mengedit pengajuan lembur milik sendiri.', 'danger')
        return redirect(url_for('overtime.list_overtime'))

    # Setelah diproses HRD, data dikunci agar histori approval tetap konsisten.
    if overtime.status != 'pending':
        flash('Pengajuan lembur yang sudah diproses HRD tidak dapat diedit.', 'warning')
        return redirect(url_for('overtime.detail_overtime', id=id))

    if request.method == 'POST':
        try:
            overtime_date = parse_date(request.form.get('overtime_date', '').strip())
            start_time = parse_time(request.form.get('start_time', '').strip())
            end_time = parse_time(request.form.get('end_time', '').strip())
            duration_minutes = calculate_duration_minutes(
                overtime_date,
                start_time,
                end_time
            )
        except ValueError as e:
            flash(str(e), 'danger')
            return redirect(url_for('overtime.edit_overtime', id=id))
        except Exception:
            flash('Tanggal dan jam lembur wajib diisi dengan format yang benar.', 'danger')
            return redirect(url_for('overtime.edit_overtime', id=id))

        supervisor_name = request.form.get('supervisor_name', '').strip()
        work_description = request.form.get('work_description', '').strip()

        if not work_description:
            flash('Keterangan pekerjaan selama lembur wajib diisi.', 'danger')
            return redirect(url_for('overtime.edit_overtime', id=id))

        upload_definitions = [
            ('chat_proof', 'chat_proof', 'chat'),
            ('overtime_photo', 'overtime_photo', 'work_photo'),
            ('attendance_photo', 'attendance_photo', 'attendance'),
        ]

        new_files = {}
        uploaded_new_paths = []

        try:
            for form_name, model_field, folder in upload_definitions:
                file = request.files.get(form_name)
                if file and file.filename:
                    new_path = upload_overtime_to_supabase(file, folder)
                    new_files[model_field] = new_path
                    uploaded_new_paths.append(new_path)
        except ValueError as e:
            delete_overtime_storage_files(uploaded_new_paths)
            flash(str(e), 'danger')
            return redirect(url_for('overtime.edit_overtime', id=id))
        except Exception as e:
            delete_overtime_storage_files(uploaded_new_paths)
            print('ERROR UPLOAD EDIT OVERTIME:', repr(e))
            flash('Gagal upload bukti pengganti ke Supabase Storage.', 'danger')
            return redirect(url_for('overtime.edit_overtime', id=id))

        old_replaced_paths = []
        for model_field, new_path in new_files.items():
            old_path = getattr(overtime, model_field, None)
            if old_path and old_path != new_path:
                old_replaced_paths.append(old_path)
            setattr(overtime, model_field, new_path)

        overtime.overtime_date = overtime_date
        overtime.start_time = start_time
        overtime.end_time = end_time
        overtime.duration_minutes = duration_minutes
        overtime.supervisor_name = supervisor_name
        overtime.work_description = work_description

        try:
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            delete_overtime_storage_files(uploaded_new_paths)
            print('ERROR UPDATE OVERTIME:', repr(e))
            flash('Perubahan lembur gagal disimpan.', 'danger')
            return redirect(url_for('overtime.edit_overtime', id=id))

        # File lama dibersihkan hanya setelah perubahan database berhasil.
        delete_overtime_storage_files(old_replaced_paths)

        flash(
            f'Pengajuan lembur berhasil diperbarui. Total lembur: '
            f'{format_duration_minutes(duration_minutes)}.',
            'success'
        )
        return redirect(url_for('overtime.detail_overtime', id=id))

    return render_template(
        'overtime/form.html',
        user=user,
        overtime=overtime,
        editing=True
    )


@overtime_bp.route('/detail/<int:id>', methods=['GET', 'POST'])
def detail_overtime(id):
    if 'user_id' not in session:
        return redirect('/login')

    user = current_logged_user()
    if not user:
        session.clear()
        return redirect('/login')

    overtime = OvertimeRequest.query.get_or_404(id)

    if user.role not in FULL_ACCESS_ROLES and overtime.user_id != user.id:
        flash('Kamu tidak punya akses ke pengajuan lembur ini.', 'danger')
        return redirect(url_for('overtime.list_overtime'))

    if request.method == 'POST':
        action = request.form.get('action')

        if user.role not in APPROVER_ROLES:
            flash('Hanya HRD yang bisa approve/reject lembur.', 'danger')
            return redirect(url_for('overtime.detail_overtime', id=id))

        if overtime.status != 'pending':
            flash('Pengajuan lembur ini sudah diproses sebelumnya.', 'warning')
            return redirect(url_for('overtime.detail_overtime', id=id))

        if action == 'approve':
            overtime.status = 'approved'
            overtime.approved_by = user.id
            overtime.approved_at = datetime.utcnow()
            db.session.commit()
            flash('Pengajuan lembur berhasil di-approve oleh HRD.', 'success')

        elif action == 'reject':
            reject_reason = request.form.get('reject_reason', '').strip()

            if not reject_reason:
                flash('Alasan penolakan wajib diisi.', 'danger')
                return redirect(url_for('overtime.detail_overtime', id=id))

            overtime.status = 'rejected'
            overtime.rejected_by = user.id
            overtime.rejected_at = datetime.utcnow()
            overtime.reject_reason = reject_reason
            db.session.commit()
            flash('Pengajuan lembur ditolak oleh HRD.', 'warning')

        else:
            flash('Aksi tidak valid.', 'danger')

        return redirect(url_for('overtime.detail_overtime', id=id))

    return render_template(
        'overtime/detail.html',
        overtime=overtime,
        user=user,
        get_user=get_user,
        approver_roles=APPROVER_ROLES,
        full_access_roles=FULL_ACCESS_ROLES
    )


@overtime_bp.route('/export_excel')
def export_overtime_excel():
    if 'user_id' not in session:
        return redirect('/login')

    user = current_logged_user()
    if not user:
        session.clear()
        return redirect('/login')

    if user.role not in FULL_ACCESS_ROLES:
        flash('Kamu tidak memiliki akses untuk export data lembur.', 'danger')
        return redirect(url_for('overtime.list_overtime'))

    query = OvertimeRequest.query
    query = apply_overtime_filters(query)
    data = query.order_by(OvertimeRequest.created_at.desc()).all()

    rows = []

    for overtime in data:
        approved_by_user = get_user(overtime.approved_by) if overtime.approved_by else None
        rejected_by_user = get_user(overtime.rejected_by) if overtime.rejected_by else None

        rows.append({
            'ID Lembur': overtime.id,
            'Nama Karyawan': overtime.employee_name,
            'Divisi': overtime.employee_divisi,
            'Tanggal Lembur': format_tanggal_excel(overtime.overtime_date),
            'Jam Mulai': format_jam_excel(overtime.start_time),
            'Jam Selesai': format_jam_excel(overtime.end_time),
            'Total Durasi': format_duration_minutes(overtime.duration_minutes),
            'Total Menit': overtime.duration_minutes,
            'Nama SPV/Manager': overtime.supervisor_name if overtime.supervisor_name else '-',
            'Keterangan Pekerjaan': overtime.work_description,
            'Status': label_status_overtime(overtime.status),
            'Tanggal Pengajuan': format_tanggal_excel(overtime.created_at, pakai_jam=True),
            'Approved By': approved_by_user.username if approved_by_user else '-',
            'Approved At': format_tanggal_excel(overtime.approved_at, pakai_jam=True),
            'Rejected By': rejected_by_user.username if rejected_by_user else '-',
            'Rejected At': format_tanggal_excel(overtime.rejected_at, pakai_jam=True),
            'Alasan Ditolak': overtime.reject_reason if overtime.reject_reason else '-',
            'Bukti Chat': overtime.chat_proof,
            'Foto Lembur': overtime.overtime_photo,
            'Foto Tap Absen': overtime.attendance_photo
        })

    return create_excel_response(
        rows=rows,
        sheet_name='Data Lembur',
        filename_prefix='Rekap_Lembur'
    )
