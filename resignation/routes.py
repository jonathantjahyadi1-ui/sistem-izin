import hmac
import io
import mimetypes
import os
import secrets
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import (
    Blueprint,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from sqlalchemy import func
from supabase import create_client

from extensions import db
from models import User
from .documents import build_resignation_document, make_document_filename
from .models import ResignationAuditLog, ResignationRequest


resignation_bp = Blueprint(
    'resignation',
    __name__,
    template_folder='../templates',
)

JAKARTA_TZ = ZoneInfo('Asia/Jakarta')
HRD_ROLES = {'admin', 'hrd'}
EDITABLE_STATUSES = {'draft', 'revision_hrd', 'revision_supervisor'}
ACTIVE_STATUSES = {
    'draft',
    'pending_hrd',
    'revision_hrd',
    # Status lama tetap dikenali agar data yang telanjur dibuat pada alur
    # approval atasan tidak terkunci setelah pembaruan.
    'pending_supervisor',
    'revision_supervisor',
}

DIVISION_OPTIONS = (
    'Marketing',
    'Operational',
    'Hostlive',
    'Creative',
    'Accounting',
    'IT Support',
    'HRD',
    'Direksi',
)

REASON_OPTIONS = (
    ('pekerjaan_lain', 'Mendapat pekerjaan lain'),
    ('pendidikan', 'Melanjutkan pendidikan'),
    ('kesehatan', 'Kesehatan (pribadi/keluarga)'),
    ('relokasi', 'Relokasi domisili'),
    ('keluarga', 'Alasan pribadi/keluarga lainnya'),
    ('lainnya', 'Lainnya'),
)

STATUS_LABELS = {
    'draft': 'Draft',
    'pending_hrd': 'Menunggu HRD',
    'revision_hrd': 'Perlu Perbaikan dari HRD',
    'rejected_hrd': 'Ditolak HRD',
    'pending_supervisor': 'Menunggu HRD (Data Lama)',
    'revision_supervisor': 'Perlu Perbaikan (Data Lama)',
    'rejected_supervisor': 'Ditolak (Riwayat Lama)',
    'approved': 'Disetujui',
    'cancelled': 'Dibatalkan',
}

SUPABASE_URL = os.getenv('SUPABASE_URL')
SUPABASE_SERVICE_ROLE_KEY = os.getenv('SUPABASE_SERVICE_ROLE_KEY')
SUPABASE_STORAGE_BUCKET = os.getenv('SUPABASE_STORAGE_BUCKET', 'izin-files')
supabase = (
    create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
    if SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY
    else None
)


def current_logged_user():
    user_id = session.get('user_id')
    return db.session.get(User, user_id) if user_id else None


def display_name(user):
    if not user:
        return '-'
    return user.nama_lengkap or user.username


def today_jakarta():
    return datetime.now(JAKARTA_TZ).date()


def utc_now():
    return datetime.utcnow()


def parse_date(value):
    value = (value or '').strip()
    if not value:
        return None
    return datetime.strptime(value, '%Y-%m-%d').date()


def clean_text(value, max_length=None):
    value = ' '.join((value or '').strip().split())
    if max_length:
        value = value[:max_length]
    return value


def clean_multiline(value, max_length=4000):
    value = (value or '').strip()
    return value[:max_length]


def status_label(status):
    return STATUS_LABELS.get(status, status or '-')


def status_class(status):
    if status == 'approved':
        return 'approved'
    if status in {'rejected_hrd', 'rejected_supervisor', 'cancelled'}:
        return 'rejected'
    if status in {'revision_hrd', 'revision_supervisor'}:
        return 'revision'
    if status == 'draft':
        return 'draft'
    return 'pending'


def format_datetime_id(value):
    if not value:
        return '-'
    local_value = value.replace(tzinfo=ZoneInfo('UTC')).astimezone(JAKARTA_TZ)
    return local_value.strftime('%d/%m/%Y %H:%M WIB')


def get_csrf_token():
    token = session.get('resignation_csrf_token')
    if not token:
        token = secrets.token_urlsafe(32)
        session['resignation_csrf_token'] = token
    return token


def validate_csrf():
    expected = session.get('resignation_csrf_token', '')
    supplied = (
        request.form.get('_csrf_token', '')
        or request.headers.get('X-CSRF-Token', '')
    )
    if not expected or not supplied or not hmac.compare_digest(expected, supplied):
        abort(400, description='Token keamanan formulir tidak valid.')


@resignation_bp.app_context_processor
def resignation_template_helpers():
    return dict(
        resignation_csrf_token=get_csrf_token,
        resignation_status_label=status_label,
        resignation_status_class=status_class,
        resignation_datetime_id=format_datetime_id,
        resignation_display_name=display_name,
    )


def get_user(user_id):
    return db.session.get(User, user_id) if user_id else None


def build_visible_query(user):
    query = ResignationRequest.query
    role = (user.role or '').lower()
    if role in HRD_ROLES:
        return query
    return query.filter(ResignationRequest.user_id == user.id)


def can_view(user, resignation):
    role = (user.role or '').lower()
    return (
        resignation.user_id == user.id
        or role in HRD_ROLES
    )


def can_view_sensitive_data(user, resignation):
    return (
        resignation.user_id == user.id
        or (user.role or '').lower() in HRD_ROLES
    )


def add_audit(resignation, actor, action, old_status, new_status, note=''):
    db.session.add(ResignationAuditLog(
        request_id=resignation.id,
        actor_id=actor.id if actor else None,
        actor_name=display_name(actor) if actor else 'Sistem',
        action=action,
        from_status=old_status,
        to_status=new_status,
        note=clean_multiline(note, 2000),
    ))


def active_request_for_user(user_id, exclude_id=None):
    query = ResignationRequest.query.filter(
        ResignationRequest.user_id == user_id,
        ResignationRequest.status.in_(ACTIVE_STATUSES),
    )
    if exclude_id:
        query = query.filter(ResignationRequest.id != exclude_id)
    return query.order_by(ResignationRequest.created_at.desc()).first()


def find_editable_request(user, request_id=None):
    resignation = None
    if request_id:
        resignation = db.session.get(ResignationRequest, request_id)
        if not resignation or resignation.user_id != user.id:
            abort(403)
        if resignation.status not in EDITABLE_STATUSES:
            return None
        return resignation

    active = active_request_for_user(user.id)
    if active and active.status in EDITABLE_STATUSES:
        return active
    return None


def create_draft(user):
    active = active_request_for_user(user.id)
    if active:
        if active.status in EDITABLE_STATUSES:
            return active
        raise ValueError(
            'Masih ada pengajuan aktif. Selesaikan atau batalkan pengajuan tersebut '
            'sebelum membuat pengajuan baru.'
        )

    resignation = ResignationRequest(
        user_id=user.id,
        status='draft',
        submission_date=today_jakarta(),
    )
    db.session.add(resignation)
    db.session.flush()
    resignation.reference_no = (
        f'RESIGN-{resignation.submission_date.year}-{resignation.id:05d}'
    )
    add_audit(resignation, user, 'draft_created', None, 'draft')
    return resignation


def update_from_form(resignation):
    resignation.employee_name = clean_text(
        request.form.get('employee_name'), 150
    )
    resignation.employee_nik = clean_text(
        request.form.get('employee_nik'), 80
    )
    resignation.position = clean_text(request.form.get('position'), 120)
    resignation.employee_division = clean_text(
        request.form.get('employee_division'), 100
    )

    # Nama atasan hanya menjadi data formulir dan diisi manual oleh karyawan.
    # Approval pengajuan dilakukan langsung oleh HRD/Admin.
    resignation.supervisor_name = clean_text(
        request.form.get('supervisor_name'),
        150,
    )
    resignation.supervisor_id = None

    employment_status = clean_text(
        request.form.get('employment_status'), 20
    ).lower()
    resignation.employment_status = (
        employment_status if employment_status in {'pkwtt', 'pkwt'} else None
    )

    try:
        resignation.start_date = parse_date(request.form.get('start_date'))
        resignation.effective_date = parse_date(request.form.get('effective_date'))
    except ValueError as exc:
        raise ValueError('Format tanggal pada formulir tidak valid.') from exc

    resignation.submission_date = resignation.submission_date or today_jakarta()
    if resignation.effective_date:
        resignation.notice_days = (
            resignation.effective_date - resignation.submission_date
        ).days
    else:
        resignation.notice_days = None
    resignation.short_notice_reason = clean_multiline(
        request.form.get('short_notice_reason'), 2000
    )

    allowed_reasons = {key for key, _ in REASON_OPTIONS}
    resignation.reason_codes = [
        value for value in request.form.getlist('reason_codes')
        if value in allowed_reasons
    ]
    resignation.reason_other = clean_multiline(
        request.form.get('reason_other'), 2000
    )

    commitment_items = set(request.form.getlist('commitment_items'))
    resignation.commitment_accepted = commitment_items == {
        'work', 'handover', 'assets', 'administration'
    }
    resignation.no_service_bond_confirmed = (
        request.form.get('no_service_bond_confirmed') == '1'
    )

    resignation.correspondence_address = clean_multiline(
        request.form.get('correspondence_address'), 4000
    )
    resignation.phone_number = clean_text(
        request.form.get('phone_number'), 40
    )
    resignation.personal_email = clean_text(
        request.form.get('personal_email'), 160
    ).lower()
    resignation.bank_name = clean_text(request.form.get('bank_name'), 100)
    resignation.bank_account_number = clean_text(
        request.form.get('bank_account_number'), 100
    )
    resignation.bank_account_holder = clean_text(
        request.form.get('bank_account_holder'), 150
    )
    resignation.declaration_accepted = (
        request.form.get('declaration_accepted') == '1'
    )


def validate_for_submission(resignation):
    required_fields = (
        ('Nama lengkap', resignation.employee_name),
        ('NIK / ID karyawan', resignation.employee_nik),
        ('Jabatan / posisi', resignation.position),
        ('Departemen / divisi', resignation.employee_division),
        ('Nama atasan langsung', resignation.supervisor_name),
        ('Status kepegawaian', resignation.employment_status),
        ('Tanggal mulai bekerja', resignation.start_date),
        ('Tanggal efektif pengunduran diri', resignation.effective_date),
        ('Alamat korespondensi', resignation.correspondence_address),
        ('Nomor telepon / WhatsApp', resignation.phone_number),
        ('Email pribadi', resignation.personal_email),
        ('Nama bank', resignation.bank_name),
        ('Nomor rekening', resignation.bank_account_number),
        ('Nama pemilik rekening', resignation.bank_account_holder),
    )
    missing = [label for label, value in required_fields if not value]
    if missing:
        raise ValueError('Data berikut wajib diisi: ' + ', '.join(missing) + '.')

    if resignation.employee_division not in DIVISION_OPTIONS:
        raise ValueError('Departemen / divisi yang dipilih tidak valid.')
    if resignation.start_date > resignation.submission_date:
        raise ValueError('Tanggal mulai bekerja tidak boleh setelah tanggal pengajuan.')
    if resignation.notice_days is None or resignation.notice_days <= 0:
        raise ValueError(
            'Tanggal efektif pengunduran diri harus setelah tanggal pengajuan.'
        )
    if resignation.notice_days < 30 and not resignation.short_notice_reason:
        raise ValueError(
            'Alasan pengajuan notice kurang dari 30 hari wajib diisi.'
        )
    if 'lainnya' in resignation.reason_codes and not resignation.reason_other:
        raise ValueError('Isi keterangan untuk alasan lainnya.')
    if not resignation.commitment_accepted:
        raise ValueError('Seluruh komitmen serah terima wajib disetujui.')
    if not resignation.no_service_bond_confirmed:
        raise ValueError('Konfirmasi mengenai ikatan dinas wajib dicentang.')
    if not resignation.declaration_accepted:
        raise ValueError('Pernyataan dan persetujuan akhir wajib dicentang.')
    if '@' not in resignation.personal_email:
        raise ValueError('Format email pribadi tidak valid.')

    duplicate_nik = ResignationRequest.query.filter(
        func.lower(ResignationRequest.employee_nik)
        == resignation.employee_nik.lower(),
        ResignationRequest.user_id != resignation.user_id,
        ResignationRequest.status.in_(ACTIVE_STATUSES),
        ResignationRequest.id != resignation.id,
    ).first()
    if duplicate_nik:
        raise ValueError(
            'NIK tersebut sedang digunakan pada pengajuan aktif milik akun lain.'
        )


def upload_final_document(resignation, document_buffer):
    if not supabase:
        raise RuntimeError('Konfigurasi penyimpanan dokumen belum tersedia.')

    filename = make_document_filename(resignation, final=True)
    storage_path = (
        f'resignation/final/{resignation.submission_date.year}/'
        f'{resignation.id}_{uuid.uuid4().hex}_{filename}'
    )
    document_buffer.seek(0)
    supabase.storage.from_(SUPABASE_STORAGE_BUCKET).upload(
        path=storage_path,
        file=document_buffer.read(),
        file_options={
            'content-type': (
                'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
            ),
            'cache-control': '3600',
            'upsert': 'false',
        },
    )
    return storage_path, filename


def delete_storage_file(storage_path):
    if not supabase or not storage_path:
        return
    try:
        supabase.storage.from_(SUPABASE_STORAGE_BUCKET).remove([storage_path])
    except Exception as exc:
        print('WARNING CLEANUP RESIGNATION DOCUMENT:', repr(exc))


@resignation_bp.route('/list')
def list_requests():
    user = current_logged_user()
    if not user:
        return redirect('/login')

    query = build_visible_query(user)
    status_filter = clean_text(request.args.get('status'), 40)
    name_filter = clean_text(request.args.get('name'), 150)
    if status_filter in STATUS_LABELS:
        query = query.filter(ResignationRequest.status == status_filter)
    else:
        status_filter = ''
    if name_filter:
        query = query.filter(
            ResignationRequest.employee_name.ilike(f'%{name_filter}%')
        )

    page = max(request.args.get('page', 1, type=int) or 1, 1)
    pagination = query.order_by(
        ResignationRequest.created_at.desc()
    ).paginate(page=page, per_page=20, error_out=False)

    base_query = build_visible_query(user)
    counts = {
        'total': base_query.count(),
        'revision_hrd': base_query.filter(
            ResignationRequest.status.in_(
                ('revision_hrd', 'revision_supervisor')
            )
        ).count(),
        'pending_hrd': base_query.filter(
            ResignationRequest.status.in_(
                ('pending_hrd', 'pending_supervisor')
            )
        ).count(),
        'approved': base_query.filter(
            ResignationRequest.status == 'approved'
        ).count(),
    }

    return render_template(
        'resignation/list.html',
        user=user,
        data=pagination.items,
        pagination=pagination,
        counts=counts,
        status_filter=status_filter,
        name_filter=name_filter,
        status_options=STATUS_LABELS,
    )


@resignation_bp.route('/form')
def resignation_form():
    user = current_logged_user()
    if not user:
        return redirect('/login')

    request_id = request.args.get('id', type=int)
    resignation = find_editable_request(user, request_id)
    if request_id and not resignation:
        flash('Pengajuan ini sudah dikirim dan tidak dapat diedit.', 'warning')
        return redirect(url_for('resignation.detail', id=request_id))

    if not request_id:
        active = active_request_for_user(user.id)
        if active and active.status not in EDITABLE_STATUSES:
            flash(
                'Kamu masih memiliki pengajuan aktif. Buka pengajuan tersebut '
                'untuk melihat prosesnya.',
                'warning',
            )
            return redirect(url_for('resignation.detail', id=active.id))
        resignation = resignation or active

    return render_template(
        'resignation/form.html',
        user=user,
        resignation=resignation,
        division_options=DIVISION_OPTIONS,
        reason_options=REASON_OPTIONS,
        today=today_jakarta(),
    )


@resignation_bp.route('/draft', methods=['POST'])
def save_draft():
    user = current_logged_user()
    if not user:
        return jsonify({'ok': False, 'message': 'Sesi login telah berakhir.'}), 401
    validate_csrf()

    request_id = request.form.get('request_id', type=int)
    resignation = find_editable_request(user, request_id)

    try:
        resignation = resignation or create_draft(user)
        update_from_form(resignation)
        db.session.commit()
    except ValueError as exc:
        db.session.rollback()
        return jsonify({'ok': False, 'message': str(exc)}), 400
    except Exception as exc:
        db.session.rollback()
        print('ERROR SAVE RESIGNATION DRAFT:', repr(exc))
        return jsonify({
            'ok': False,
            'message': 'Draft gagal disimpan. Silakan coba kembali.',
        }), 500

    return jsonify({
        'ok': True,
        'request_id': resignation.id,
        'reference_no': resignation.reference_no,
        'saved_at': format_datetime_id(utc_now()),
    })


@resignation_bp.route('/submit', methods=['POST'])
def submit_request():
    user = current_logged_user()
    if not user:
        return redirect('/login')
    validate_csrf()

    request_id = request.form.get('request_id', type=int)
    resignation = find_editable_request(user, request_id)
    old_status = resignation.status if resignation else None

    try:
        resignation = resignation or create_draft(user)
        old_status = old_status or resignation.status
        update_from_form(resignation)
        validate_for_submission(resignation)

        # Pengajuan langsung masuk ke antrean HRD tanpa approval atasan.
        resignation.status = 'pending_hrd'
        resignation.submitted_at = utc_now()
        resignation.supervisor_decision = None
        resignation.supervisor_by = None
        resignation.supervisor_at = None
        resignation.supervisor_note = None
        resignation.hrd_decision = None
        resignation.hrd_by = None
        resignation.hrd_at = None
        resignation.hrd_note = None
        resignation.final_document_path = None
        resignation.final_document_name = None
        add_audit(
            resignation,
            user,
            'submitted',
            old_status,
            'pending_hrd',
        )
        db.session.commit()
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), 'danger')
        target_id = request_id or (
            resignation.id if resignation and resignation.id else None
        )
        return redirect(url_for('resignation.resignation_form', id=target_id))
    except Exception as exc:
        db.session.rollback()
        print('ERROR SUBMIT RESIGNATION:', repr(exc))
        flash('Pengajuan gagal dikirim. Silakan coba kembali.', 'danger')
        return redirect(url_for('resignation.resignation_form', id=request_id))

    flash('Pengajuan berhasil dikirim kepada HRD untuk direview.', 'success')
    return redirect(url_for('resignation.list_requests'))


@resignation_bp.route('/detail/<int:id>')
def detail(id):
    user = current_logged_user()
    if not user:
        return redirect('/login')
    resignation = db.session.get(ResignationRequest, id)
    if not resignation:
        abort(404)
    if not can_view(user, resignation):
        abort(403)

    audit_logs = ResignationAuditLog.query.filter_by(
        request_id=resignation.id
    ).order_by(ResignationAuditLog.created_at.asc()).all()

    return render_template(
        'resignation/detail.html',
        user=user,
        resignation=resignation,
        audit_logs=audit_logs,
        reason_options=dict(REASON_OPTIONS),
        can_view_sensitive=can_view_sensitive_data(user, resignation),
        is_hrd=(user.role or '').lower() in HRD_ROLES,
    )


@resignation_bp.route('/hrd/<int:id>/decision', methods=['POST'])
def hrd_decision(id):
    user = current_logged_user()
    if not user:
        return redirect('/login')
    validate_csrf()
    if (user.role or '').lower() not in HRD_ROLES:
        abort(403)

    resignation = db.session.get(ResignationRequest, id)
    if not resignation:
        abort(404)
    if resignation.status not in {'pending_hrd', 'pending_supervisor'}:
        flash('Pengajuan ini belum siap atau sudah diproses HRD.', 'warning')
        return redirect(url_for('resignation.detail', id=id))

    action = request.form.get('action', '').strip()
    note = clean_multiline(request.form.get('note'), 2000)
    old_status = resignation.status
    now = utc_now()
    uploaded_path = None

    try:
        if action == 'approve':
            resignation.status = 'approved'
            resignation.hrd_decision = 'approved'
            resignation.hrd_by = user.id
            resignation.hrd_at = now
            resignation.hrd_note = note
            document_buffer = build_resignation_document(
                resignation,
                get_user,
                final=True,
            )
            uploaded_path, filename = upload_final_document(
                resignation,
                document_buffer,
            )
            resignation.final_document_path = uploaded_path
            resignation.final_document_name = filename
            audit_action = 'hrd_approved'
            message = 'Pengajuan disetujui HRD dan dokumen Word final telah dibuat.'
            category = 'success'
        elif action in {'revision', 'reject'}:
            if not note:
                flash('Catatan wajib diisi untuk revisi atau penolakan.', 'danger')
                return redirect(url_for('resignation.detail', id=id))
            resignation.status = (
                'revision_hrd' if action == 'revision' else 'rejected_hrd'
            )
            resignation.hrd_decision = (
                'revision' if action == 'revision' else 'rejected'
            )
            resignation.hrd_by = user.id
            resignation.hrd_at = now
            resignation.hrd_note = note
            audit_action = (
                'hrd_requested_revision'
                if action == 'revision' else 'hrd_rejected'
            )
            message = (
                'Pengajuan dikembalikan kepada karyawan untuk diperbaiki.'
                if action == 'revision' else 'Pengajuan ditolak oleh HRD.'
            )
            category = 'warning'
        else:
            flash('Aksi review HRD tidak valid.', 'danger')
            return redirect(url_for('resignation.detail', id=id))

        add_audit(
            resignation,
            user,
            audit_action,
            old_status,
            resignation.status,
            note,
        )
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        delete_storage_file(uploaded_path)
        print('ERROR HRD RESIGNATION DECISION:', repr(exc))
        flash(
            'Keputusan gagal disimpan atau dokumen Word final gagal dibuat.',
            'danger',
        )
        return redirect(url_for('resignation.detail', id=id))

    flash(message, category)
    return redirect(url_for('resignation.detail', id=id))


@resignation_bp.route('/cancel/<int:id>', methods=['POST'])
def cancel_request(id):
    user = current_logged_user()
    if not user:
        return redirect('/login')
    validate_csrf()

    resignation = db.session.get(ResignationRequest, id)
    if not resignation:
        abort(404)
    if resignation.user_id != user.id:
        abort(403)
    if resignation.status not in ACTIVE_STATUSES:
        flash('Pengajuan ini sudah final dan tidak dapat dibatalkan.', 'warning')
        return redirect(url_for('resignation.detail', id=id))

    note = clean_multiline(request.form.get('note'), 2000)
    old_status = resignation.status
    resignation.status = 'cancelled'
    add_audit(resignation, user, 'cancelled', old_status, 'cancelled', note)
    db.session.commit()
    flash('Pengajuan pengunduran diri telah dibatalkan.', 'success')
    return redirect(url_for('resignation.detail', id=id))


@resignation_bp.route('/preview/<int:id>')
def preview_document(id):
    user = current_logged_user()
    if not user:
        return redirect('/login')
    resignation = db.session.get(ResignationRequest, id)
    if not resignation:
        abort(404)
    if not can_view_sensitive_data(user, resignation):
        abort(403)
    if resignation.status == 'approved' and resignation.final_document_path:
        return redirect(url_for('resignation.download_document', id=id))

    buffer = build_resignation_document(resignation, get_user, final=False)
    return send_file(
        buffer,
        mimetype=(
            'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
        ),
        as_attachment=True,
        download_name=make_document_filename(resignation, final=False),
    )


@resignation_bp.route('/download/<int:id>')
def download_document(id):
    user = current_logged_user()
    if not user:
        return redirect('/login')
    resignation = db.session.get(ResignationRequest, id)
    if not resignation:
        abort(404)
    if not can_view_sensitive_data(user, resignation):
        abort(403)
    if resignation.status != 'approved' or not resignation.final_document_path:
        flash('Dokumen Word final belum tersedia.', 'warning')
        return redirect(url_for('resignation.detail', id=id))
    if not supabase:
        flash('Konfigurasi penyimpanan dokumen belum tersedia.', 'danger')
        return redirect(url_for('resignation.detail', id=id))

    try:
        file_bytes = supabase.storage.from_(SUPABASE_STORAGE_BUCKET).download(
            resignation.final_document_path
        )
    except Exception as exc:
        print('ERROR DOWNLOAD RESIGNATION DOCUMENT:', repr(exc))
        flash('Dokumen Word final gagal diunduh.', 'danger')
        return redirect(url_for('resignation.detail', id=id))

    if hasattr(file_bytes, 'data'):
        file_bytes = file_bytes.data
    return send_file(
        io.BytesIO(file_bytes),
        mimetype=(
            mimetypes.guess_type(resignation.final_document_name or '')[0]
            or 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
        ),
        as_attachment=True,
        download_name=(
            resignation.final_document_name
            or make_document_filename(resignation, final=True)
        ),
    )
