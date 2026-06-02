from flask import Blueprint, request, render_template, redirect, url_for, flash, session
from extensions import db
from models import User
from .models import ReimburseRequest, ReimburseItem
from datetime import datetime, timedelta
import os

reimburse_bp = Blueprint('reimburse', __name__, template_folder='../templates/reimburse')
UPLOAD_FOLDER = 'uploads'


def get_user(user_id):
    return db.session.get(User, user_id)


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
    import pandas as pd
    import io
    from flask import send_file

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

    reimbursements = ReimburseRequest.query.filter(
        ReimburseRequest.paid_at != None,
        ReimburseRequest.paid_at < cutoff
    ).order_by(ReimburseRequest.paid_at.desc()).all()

    rows = []

    for r in reimbursements:
        pengaju = get_user(r.user_id)

        for item in r.items:
            rows.append({
                'ID Reimburse': r.id,
                'Pengaju': pengaju.username if pengaju else '-',
                'Tanggal Pengajuan': r.created_at.strftime('%d/%m/%Y %H:%M') if r.created_at else '-',
                'Tanggal Dibayar': r.paid_at.strftime('%d/%m/%Y %H:%M') if r.paid_at else '-',
                'Status': r.status,
                'Nama Item': item.item_name,
                'Harga': item.price,
                'Qty': item.qty,
                'Subtotal': item.price * item.qty,
                'Total Reimburse': r.total_amount
            })

    df = pd.DataFrame(rows)

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Arsip Reimburse')

    output.seek(0)

    filename = f"Arsip_Reimburse_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"

    return send_file(
        output,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        as_attachment=True,
        download_name=filename
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
            filename = f"payment_{datetime.now().timestamp()}_{file.filename}"
            file.save(os.path.join(UPLOAD_FOLDER, filename))
            reimb.payment_proof = filename
            reimb.paid_at = datetime.utcnow()
            reimb.status = 'paid'
            db.session.commit()
            flash('Bukti pembayaran berhasil diunggah.', 'success')

        return redirect(url_for('reimburse.detail', id=id))

    return render_template(
        'reimburse/detail.html',
        reimb=reimb,
        user=user,
        get_user=get_user
    )

@reimburse_bp.route('/export_excel')
def export_excel():
    import pandas as pd
    import io
    from flask import send_file

    if 'user_id' not in session:
        return redirect('/login')

    user = db.session.get(User, session['user_id'])

    if user.role not in ['admin', 'direktur', 'accounting']:
        return redirect('/reimburse/list')

    reimbursements = ReimburseRequest.query.order_by(
        ReimburseRequest.created_at.desc()
    ).all()

    rows = []

    for r in reimbursements:
        pengaju = db.session.get(User, r.user_id)

        for item in r.items:
            rows.append({
                'ID Reimburse': r.id,
                'Pengaju': pengaju.username if pengaju else '-',
                'Tanggal Pengajuan': r.created_at.strftime('%d/%m/%Y %H:%M'),
                'Status': r.status,
                'Nama Item': item.item_name,
                'Harga': item.price,
                'Qty': item.qty,
                'Subtotal': item.price * item.qty,
                'Total Reimburse': r.total_amount,
                'Tanggal Bayar': r.paid_at.strftime('%d/%m/%Y %H:%M') if r.paid_at else '-'
            })

    df = pd.DataFrame(rows)

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Data Reimburse')

    output.seek(0)

    filename = f"Data_Reimburse_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"

    return send_file(
        output,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        as_attachment=True,
        download_name=filename
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

        if not item_names:
            flash('Minimal satu item harus diisi.', 'danger')
            return redirect(url_for('reimburse.submit'))

        total = 0
        items = []
        for n, p, q in zip(item_names, prices, qtys):
            price = int(p) if p else 0
            qty = int(q) if q else 1
            total += price * qty
            items.append({'item_name': n, 'price': price, 'qty': qty})

        file = request.files.get('receipt')
        filename = None
        if file and file.filename != '':
            filename = f"receipt_{datetime.now().timestamp()}_{file.filename}"
            file.save(os.path.join(UPLOAD_FOLDER, filename))

        reimb = ReimburseRequest(
            user_id=user.id,
            total_amount=total,
            receipt_photo=filename
        )
        db.session.add(reimb)
        db.session.flush()

        for it in items:
            db.session.add(ReimburseItem(
                reimburse_id=reimb.id,
                item_name=it['item_name'],
                price=it['price'],
                qty=it['qty']
            ))

        db.session.commit()
        flash('Pengajuan reimburse berhasil!', 'success')
        return redirect(url_for('reimburse.list_reimburse'))

    return render_template('reimburse/form.html', user=user)
