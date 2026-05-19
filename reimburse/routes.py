from flask import Blueprint, request, render_template, redirect, url_for, flash, session
from extensions import db
from .models import ReimburseRequest, ReimburseItem
from datetime import datetime, timedelta
import os

reimburse_bp = Blueprint('reimburse', __name__, template_folder='../templates/reimburse')
UPLOAD_FOLDER = 'uploads'


def get_user(user_id):
    from izin import User
    return db.session.get(User, user_id)


@reimburse_bp.route('/list')
def list_reimburse():
    from izin import User    
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
    if user.role not in ['admin', 'direktur']:
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
    return render_template('list.html', data=data, user=user, get_user=get_user, nama_filter=nama_filter)


@reimburse_bp.route('/archive')
def archive():
    from models import User
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
    if user.role not in ['admin', 'direktur']:
        query = query.filter(ReimburseRequest.user_id == user.id)

    data = query.order_by(ReimburseRequest.paid_at.desc()).all()
    return render_template('archive.html', data=data, user=user, get_user=get_user)

@reimburse_bp.route('/detail/<int:id>', methods=['GET', 'POST'])
def detail(id):
    from izin import User
    if 'user_id' not in session:
        return redirect('/login')
    user = db.session.get(User, session['user_id'])
    if not user:
        return redirect('/login')

    reimb = ReimburseRequest.query.get_or_404(id)

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

    return render_template('detail.html', reimb=reimb, user=user, get_user=get_user)


@reimburse_bp.route('/submit', methods=['GET', 'POST'])
def submit():
    from izin import User
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

    return render_template('form.html', user=user)