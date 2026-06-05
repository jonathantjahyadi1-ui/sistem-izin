from flask import Blueprint, request, render_template, redirect, url_for, flash, session, send_file
from extensions import db
from models import User, PurchaseOrderRequest, PurchaseOrderItem
from datetime import datetime, timedelta
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
def format_tanggal_excel(value, pakai_jam=False):
    if not value:
        return '-'

    try:
        if pakai_jam:
            return value.strftime('%d/%m/%Y %H:%M')
        return value.strftime('%d/%m/%Y')
    except:
        return str(value)


def label_status_po(status):
    labels = {
        'submitted': 'Menunggu Accounting',
        'accounting_approved': 'Disetujui Accounting',
        'accounting_rejected': 'Ditolak Accounting',
        'director_approved': 'Disetujui Direktur',
        'director_rejected': 'Ditolak Direktur',
        'ordered': 'Sudah Dipesan'
    }

    return labels.get(status, status or '-')


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


@po_bp.route('/list')
def list_po():
    if 'user_id' not in session:
        return redirect('/login')

    user = db.session.get(User, session['user_id'])
    if not user:
        session.clear()
        return redirect('/login')

    cutoff = datetime.utcnow() - timedelta(days=7)

    query = PurchaseOrderRequest.query.filter(
        (PurchaseOrderRequest.ordered_at == None) |
        (PurchaseOrderRequest.ordered_at >= cutoff)
    )

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

@po_bp.route('/archive')
def archive_po():
    if 'user_id' not in session:
        return redirect('/login')

    user = db.session.get(User, session['user_id'])
    if not user:
        session.clear()
        return redirect('/login')

    cutoff = datetime.utcnow() - timedelta(days=7)

    query = PurchaseOrderRequest.query.filter(
        PurchaseOrderRequest.status == 'ordered',
        PurchaseOrderRequest.ordered_at != None,
        PurchaseOrderRequest.ordered_at < cutoff
    )

    if user.role not in ['admin', 'direktur', 'accounting']:
        query = query.filter(PurchaseOrderRequest.user_id == user.id)

    data = query.order_by(PurchaseOrderRequest.ordered_at.desc()).all()

    return render_template(
        'purchase_order/archive.html',
        data=data,
        user=user,
        get_user=get_user
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
        action = request.form.get('action')
        reject_reason = request.form.get('reject_reason', '').strip()

        if action == 'accounting_approve':
            if user.role not in ['accounting', 'admin']:
                flash('Hanya accounting yang bisa approve tahap ini.', 'danger')
                return redirect(url_for('purchase_order.detail_po', id=id))

            if po.status != 'submitted':
                flash('PO ini sudah diproses sebelumnya.', 'warning')
                return redirect(url_for('purchase_order.detail_po', id=id))

            po.status = 'accounting_approved'
            po.accounting_approved_at = datetime.utcnow()
            db.session.commit()
            flash('PO disetujui Accounting dan diteruskan ke Direktur.', 'success')

        elif action == 'accounting_reject':
            if user.role not in ['accounting', 'admin']:
                flash('Hanya accounting yang bisa menolak tahap ini.', 'danger')
                return redirect(url_for('purchase_order.detail_po', id=id))

            if not reject_reason:
                flash('Alasan penolakan wajib diisi.', 'danger')
                return redirect(url_for('purchase_order.detail_po', id=id))

            po.status = 'accounting_rejected'
            po.accounting_rejected_at = datetime.utcnow()
            po.reject_reason = reject_reason
            db.session.commit()
            flash('PO ditolak oleh Accounting.', 'warning')

        elif action == 'director_approve':
            if user.role != 'direktur':
                flash('Hanya direktur yang bisa approve tahap ini.', 'danger')
                return redirect(url_for('purchase_order.detail_po', id=id))

            if po.status != 'accounting_approved':
                flash('PO harus disetujui Accounting terlebih dahulu.', 'warning')
                return redirect(url_for('purchase_order.detail_po', id=id))

            po.status = 'director_approved'
            po.director_approved_at = datetime.utcnow()
            db.session.commit()
            flash('PO disetujui Direktur.', 'success')

        elif action == 'director_reject':
            if user.role != 'direktur':
                flash('Hanya direktur yang bisa menolak tahap ini.', 'danger')
                return redirect(url_for('purchase_order.detail_po', id=id))

            if not reject_reason:
                flash('Alasan penolakan wajib diisi.', 'danger')
                return redirect(url_for('purchase_order.detail_po', id=id))

            po.status = 'director_rejected'
            po.director_rejected_at = datetime.utcnow()
            po.reject_reason = reject_reason
            db.session.commit()
            flash('PO ditolak oleh Direktur.', 'warning')

        elif action == 'upload_order_proof':
            if user.role != 'direktur':
                flash('Hanya direktur yang bisa upload bukti pemesanan.', 'danger')
                return redirect(url_for('purchase_order.detail_po', id=id))

            if po.status != 'director_approved':
                flash('Bukti pemesanan hanya bisa diupload setelah disetujui Direktur.', 'danger')
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
            flash('Bukti pemesanan berhasil diupload. Status menjadi sudah dipesan.', 'success')

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

    status_filter = request.args.get('status', '').strip()
    nama_filter = request.args.get('nama', '').strip()

    cutoff = datetime.utcnow() - timedelta(days=7)

    # Sama seperti /purchase-order/list:
    # hanya data aktif, bukan arsip
    query = db.session.query(PurchaseOrderRequest, User).join(
        User, PurchaseOrderRequest.user_id == User.id
    ).filter(
        (PurchaseOrderRequest.ordered_at == None) |
        (PurchaseOrderRequest.ordered_at >= cutoff)
    )

    if status_filter:
        query = query.filter(PurchaseOrderRequest.status == status_filter)

    if nama_filter:
        query = query.filter(User.username.ilike(f'%{nama_filter}%'))

    purchase_orders = query.order_by(PurchaseOrderRequest.created_at.desc()).all()

    rows = []

    for po, pengaju in purchase_orders:
        if po.items:
            for item in po.items:
                rows.append({
                    'ID PO': po.id,
                    'Nama Pengaju': pengaju.username if pengaju else '-',
                    'Divisi': pengaju.divisi if pengaju else '-',
                    'Tanggal Pengajuan': format_tanggal_excel(po.created_at, pakai_jam=True),
                    'Status': label_status_po(po.status),
                    'Alasan Pembelian': po.reason,
                    'Nama Barang': item.item_name,
                    'Estimasi Harga': item.estimated_price,
                    'Qty': item.qty,
                    'Subtotal': item.estimated_price * item.qty,
                    'Total PO': po.total_amount,
                    'Tanggal ACC Accounting': format_tanggal_excel(po.accounting_approved_at, pakai_jam=True),
                    'Tanggal Tolak Accounting': format_tanggal_excel(po.accounting_rejected_at, pakai_jam=True),
                    'Tanggal ACC Direktur': format_tanggal_excel(po.director_approved_at, pakai_jam=True),
                    'Tanggal Tolak Direktur': format_tanggal_excel(po.director_rejected_at, pakai_jam=True),
                    'Tanggal Ordered': format_tanggal_excel(po.ordered_at, pakai_jam=True),
                    'Alasan Ditolak': po.reject_reason if po.reject_reason else '-',
                    'Bukti Pemesanan': po.order_proof if po.order_proof else '-'
                })
        else:
            rows.append({
                'ID PO': po.id,
                'Nama Pengaju': pengaju.username if pengaju else '-',
                'Divisi': pengaju.divisi if pengaju else '-',
                'Tanggal Pengajuan': format_tanggal_excel(po.created_at, pakai_jam=True),
                'Status': label_status_po(po.status),
                'Alasan Pembelian': po.reason,
                'Nama Barang': '-',
                'Estimasi Harga': 0,
                'Qty': 0,
                'Subtotal': 0,
                'Total PO': po.total_amount,
                'Tanggal ACC Accounting': format_tanggal_excel(po.accounting_approved_at, pakai_jam=True),
                'Tanggal Tolak Accounting': format_tanggal_excel(po.accounting_rejected_at, pakai_jam=True),
                'Tanggal ACC Direktur': format_tanggal_excel(po.director_approved_at, pakai_jam=True),
                'Tanggal Tolak Direktur': format_tanggal_excel(po.director_rejected_at, pakai_jam=True),
                'Tanggal Ordered': format_tanggal_excel(po.ordered_at, pakai_jam=True),
                'Alasan Ditolak': po.reject_reason if po.reject_reason else '-',
                'Bukti Pemesanan': po.order_proof if po.order_proof else '-'
            })

    return buat_file_excel(
        rows=rows,
        sheet_name='Data PO Aktif',
        filename_prefix='Rekap_PO_Aktif'
    )

@po_bp.route('/archive/export_excel')
def export_po_archive_excel():
    if 'user_id' not in session:
        return redirect('/login')

    user = db.session.get(User, session['user_id'])

    if not user:
        session.clear()
        return redirect('/login')

    if user.role not in ['admin', 'direktur', 'accounting']:
        flash('Kamu tidak memiliki akses untuk mengunduh arsip PO.', 'danger')
        return redirect(url_for('purchase_order.archive_po'))

    cutoff = datetime.utcnow() - timedelta(days=7)

    query = db.session.query(PurchaseOrderRequest, User).join(
        User, PurchaseOrderRequest.user_id == User.id
    ).filter(
        PurchaseOrderRequest.status == 'ordered',
        PurchaseOrderRequest.ordered_at != None,
        PurchaseOrderRequest.ordered_at < cutoff
    )

    purchase_orders = query.order_by(PurchaseOrderRequest.ordered_at.desc()).all()

    rows = []

    for po, pengaju in purchase_orders:
        if po.items:
            for item in po.items:
                rows.append({
                    'ID PO': po.id,
                    'Nama Pengaju': pengaju.username if pengaju else '-',
                    'Divisi': pengaju.divisi if pengaju else '-',
                    'Tanggal Pengajuan': format_tanggal_excel(po.created_at, pakai_jam=True),
                    'Tanggal Ordered': format_tanggal_excel(po.ordered_at, pakai_jam=True),
                    'Status': label_status_po(po.status),
                    'Alasan Pembelian': po.reason,
                    'Nama Barang': item.item_name,
                    'Estimasi Harga': item.estimated_price,
                    'Qty': item.qty,
                    'Subtotal': item.estimated_price * item.qty,
                    'Total PO': po.total_amount,
                    'Bukti Pemesanan': po.order_proof if po.order_proof else '-'
                })
        else:
            rows.append({
                'ID PO': po.id,
                'Nama Pengaju': pengaju.username if pengaju else '-',
                'Divisi': pengaju.divisi if pengaju else '-',
                'Tanggal Pengajuan': format_tanggal_excel(po.created_at, pakai_jam=True),
                'Tanggal Ordered': format_tanggal_excel(po.ordered_at, pakai_jam=True),
                'Status': label_status_po(po.status),
                'Alasan Pembelian': po.reason,
                'Nama Barang': '-',
                'Estimasi Harga': 0,
                'Qty': 0,
                'Subtotal': 0,
                'Total PO': po.total_amount,
                'Bukti Pemesanan': po.order_proof if po.order_proof else '-'
            })

    return buat_file_excel(
        rows=rows,
        sheet_name='Arsip PO',
        filename_prefix='Arsip_PO'
    )