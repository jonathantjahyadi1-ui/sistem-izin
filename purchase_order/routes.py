from flask import Blueprint, request, render_template, redirect, url_for, flash, session, send_file
from extensions import db
from models import User, PurchaseOrderRequest, PurchaseOrderItem
from datetime import datetime
import os
import io
import pandas as pd


po_bp = Blueprint(
    'purchase_order',
    __name__,
    template_folder='../templates'
)

UPLOAD_FOLDER = 'uploads'


def get_user(user_id):
    return db.session.get(User, user_id)


@po_bp.route('/list')
def list_po():
    if 'user_id' not in session:
        return redirect('/login')

    user = db.session.get(User, session['user_id'])
    if not user:
        session.clear()
        return redirect('/login')

    query = PurchaseOrderRequest.query

    if user.role not in ['admin', 'direktur', 'accounting']:
        query = query.filter(PurchaseOrderRequest.user_id == user.id)

    status_filter = request.args.get('status', '')
    nama_filter = request.args.get('nama', '')

    if status_filter:
        query = query.filter(PurchaseOrderRequest.status == status_filter)

    if nama_filter and user.role in ['admin', 'direktur', 'accounting']:
        query = query.join(User, PurchaseOrderRequest.user_id == User.id)
        query = query.filter(User.username.ilike(f'%{nama_filter}%'))

    data = query.order_by(PurchaseOrderRequest.created_at.desc()).all()

    return render_template(
        'purchase_order/list.html',
        data=data,
        user=user,
        get_user=get_user,
        status_filter=status_filter,
        nama_filter=nama_filter
    )


@po_bp.route('/submit', methods=['GET', 'POST'])
def submit_po():
    if 'user_id' not in session:
        return redirect('/login')

    user = db.session.get(User, session['user_id'])
    if not user:
        session.clear()
        return redirect('/login')

    if request.method == 'POST':
        item_names = request.form.getlist('item_name[]')
        prices = request.form.getlist('estimated_price[]')
        qtys = request.form.getlist('qty[]')
        notes = request.form.getlist('note[]')
        reason = request.form.get('reason', '').strip()

        if not reason:
            flash('Alasan pembelian wajib diisi.', 'danger')
            return redirect(url_for('purchase_order.submit_po'))

        total = 0
        items = []

        for name, price, qty, note in zip(item_names, prices, qtys, notes):
            name = name.strip()
            note = note.strip() if note else ''

            if not name:
                continue

            try:
                estimated_price = int(price) if price else 0
                quantity = int(qty) if qty else 1
            except ValueError:
                flash('Harga dan qty harus berupa angka.', 'danger')
                return redirect(url_for('purchase_order.submit_po'))

            if estimated_price < 0 or quantity <= 0:
                flash('Harga tidak boleh minus dan qty minimal 1.', 'danger')
                return redirect(url_for('purchase_order.submit_po'))

            total += estimated_price * quantity

            items.append({
                'item_name': name,
                'estimated_price': estimated_price,
                'qty': quantity,
                'note': note
            })

        if not items:
            flash('Minimal satu barang valid harus diisi.', 'danger')
            return redirect(url_for('purchase_order.submit_po'))

        po = PurchaseOrderRequest(
            user_id=user.id,
            reason=reason,
            total_amount=total,
            status='submitted'
        )

        db.session.add(po)
        db.session.flush()

        for item in items:
            db.session.add(PurchaseOrderItem(
                po_id=po.id,
                item_name=item['item_name'],
                estimated_price=item['estimated_price'],
                qty=item['qty'],
                note=item['note']
            ))

        db.session.commit()

        flash('Purchase Order berhasil diajukan.', 'success')
        return redirect(url_for('purchase_order.list_po'))

    return render_template('purchase_order/form.html', user=user)


@po_bp.route('/detail/<int:id>', methods=['GET', 'POST'])
def detail_po(id):
    if 'user_id' not in session:
        return redirect('/login')

    user = db.session.get(User, session['user_id'])
    if not user:
        session.clear()
        return redirect('/login')

    po = PurchaseOrderRequest.query.get_or_404(id)

    if user.role not in ['admin', 'direktur', 'accounting'] and po.user_id != user.id:
        flash('Kamu tidak punya akses ke PO ini.', 'danger')
        return redirect(url_for('purchase_order.list_po'))

    if request.method == 'POST':
        if user.role != 'direktur':
            flash('Hanya direktur yang bisa memproses Purchase Order.', 'danger')
            return redirect(url_for('purchase_order.detail_po', id=id))

        action = request.form.get('action')

        if action == 'approve':
            if po.status != 'submitted':
                flash('PO ini sudah diproses sebelumnya.', 'warning')
                return redirect(url_for('purchase_order.detail_po', id=id))

            po.status = 'approved'
            po.approved_at = datetime.utcnow()
            db.session.commit()
            flash('Purchase Order berhasil disetujui.', 'success')

        elif action == 'reject':
            if po.status != 'submitted':
                flash('PO ini sudah diproses sebelumnya.', 'warning')
                return redirect(url_for('purchase_order.detail_po', id=id))

            po.status = 'rejected'
            po.rejected_at = datetime.utcnow()
            db.session.commit()
            flash('Purchase Order ditolak.', 'warning')

        elif action == 'upload_order_proof':
            if po.status != 'approved':
                flash('Bukti pemesanan hanya bisa diupload setelah PO disetujui.', 'danger')
                return redirect(url_for('purchase_order.detail_po', id=id))

            file = request.files.get('order_proof')

            if not file or file.filename == '':
                flash('File bukti pemesanan wajib dipilih.', 'danger')
                return redirect(url_for('purchase_order.detail_po', id=id))

            os.makedirs(UPLOAD_FOLDER, exist_ok=True)

            filename = f"po_order_{datetime.now().timestamp()}_{file.filename}"
            file.save(os.path.join(UPLOAD_FOLDER, filename))

            po.order_proof = filename
            po.ordered_at = datetime.utcnow()
            po.status = 'ordered'

            db.session.commit()
            flash('Bukti pemesanan berhasil diupload.', 'success')

        else:
            flash('Aksi tidak valid.', 'danger')

        return redirect(url_for('purchase_order.detail_po', id=id))

    return render_template(
        'purchase_order/detail.html',
        po=po,
        user=user,
        get_user=get_user
    )


@po_bp.route('/export_excel')
def export_po_excel():
    if 'user_id' not in session:
        return redirect('/login')

    user = db.session.get(User, session['user_id'])
    if not user:
        session.clear()
        return redirect('/login')

    if user.role not in ['admin', 'direktur', 'accounting']:
        flash('Kamu tidak memiliki akses untuk mengunduh data PO.', 'danger')
        return redirect(url_for('purchase_order.list_po'))

    query = PurchaseOrderRequest.query

    status_filter = request.args.get('status', '')
    nama_filter = request.args.get('nama', '')

    if status_filter:
        query = query.filter(PurchaseOrderRequest.status == status_filter)

    if nama_filter:
        query = query.join(User, PurchaseOrderRequest.user_id == User.id)
        query = query.filter(User.username.ilike(f'%{nama_filter}%'))

    purchase_orders = query.order_by(PurchaseOrderRequest.created_at.desc()).all()

    rows = []

    for po in purchase_orders:
        pengaju = get_user(po.user_id)

        for item in po.items:
            rows.append({
                'ID PO': po.id,
                'Pengaju': pengaju.username if pengaju else '-',
                'Divisi': pengaju.divisi if pengaju else '-',
                'Tanggal Pengajuan': po.created_at.strftime('%d/%m/%Y %H:%M') if po.created_at else '-',
                'Status': po.status,
                'Alasan Pembelian': po.reason,
                'Nama Barang': item.item_name,
                'Estimasi Harga': item.estimated_price,
                'Qty': item.qty,
                'Subtotal': item.estimated_price * item.qty,
                'Total PO': po.total_amount,
                'Tanggal Approve': po.approved_at.strftime('%d/%m/%Y %H:%M') if po.approved_at else '-',
                'Tanggal Reject': po.rejected_at.strftime('%d/%m/%Y %H:%M') if po.rejected_at else '-',
                'Tanggal Ordered': po.ordered_at.strftime('%d/%m/%Y %H:%M') if po.ordered_at else '-',
                'Bukti Pemesanan': po.order_proof if po.order_proof else '-'
            })

    output = io.BytesIO()

    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df = pd.DataFrame(rows)
        df.to_excel(writer, index=False, sheet_name='Data PO')

    output.seek(0)

    filename = f"Data_PO_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"

    return send_file(
        output,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        as_attachment=True,
        download_name=filename
    )