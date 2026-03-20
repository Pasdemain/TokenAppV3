import random
from datetime import date, datetime
from flask import Blueprint, render_template, redirect, url_for, session, flash, request, jsonify
from psycopg2.extras import RealDictCursor
from database import get_db_connection
from auth import login_required

scratch_bp = Blueprint('scratch', __name__)


@scratch_bp.route('/scratch')
@login_required
def scratch_page():
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    today = date.today()

    cur.execute("""
        SELECT st.*, sp.name as prize_name, sp.is_loser, sp.token_name
        FROM scratch_tickets st
        LEFT JOIN scratch_prizes sp ON st.prize_id = sp.id
        WHERE st.user_id = %s AND st.ticket_date = %s
    """, (session['user_id'], today))
    ticket = cur.fetchone()

    cur.execute("SELECT COUNT(*) as cnt FROM scratch_prizes WHERE user_id = %s", (session['user_id'],))
    has_prizes = cur.fetchone()['cnt'] > 0

    cur.close()
    conn.close()

    return render_template('scratch.html', ticket=ticket, has_prizes=has_prizes)


@scratch_bp.route('/scratch/play', methods=['POST'])
@login_required
def play_scratch():
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    today = date.today()

    cur.execute(
        "SELECT id FROM scratch_tickets WHERE user_id = %s AND ticket_date = %s",
        (session['user_id'], today)
    )
    if cur.fetchone():
        cur.close()
        conn.close()
        return jsonify({'error': "Ticket déjà utilisé aujourd'hui"}), 400

    cur.execute("SELECT * FROM scratch_prizes WHERE user_id = %s", (session['user_id'],))
    prizes = cur.fetchall()

    if not prizes:
        cur.close()
        conn.close()
        return jsonify({'error': 'Aucun prix configuré pour ce compte'}), 400

    # Weighted random selection
    total = sum(float(p['probability']) for p in prizes)
    rand = random.uniform(0, total)
    cumulative = 0.0
    selected = prizes[-1]
    for prize in prizes:
        cumulative += float(prize['probability'])
        if rand <= cumulative:
            selected = prize
            break

    # Record the ticket
    cur.execute("""
        INSERT INTO scratch_tickets (user_id, ticket_date, scratched_at, prize_id)
        VALUES (%s, %s, %s, %s)
    """, (session['user_id'], today, datetime.utcnow(), selected['id']))

    won_token = False
    if not selected['is_loser']:
        # Find partner (other user in system)
        cur.execute("SELECT id FROM users WHERE id != %s LIMIT 1", (session['user_id'],))
        other = cur.fetchone()
        if other:
            cur.execute("""
                INSERT INTO tokens (creator_id, recipient_id, name, description, duration_minutes, status)
                VALUES (%s, %s, %s, %s, %s, 'available')
            """, (
                other['id'],
                session['user_id'],
                selected['token_name'] or selected['name'],
                selected['token_description'] or '🎰 Token gagné par ticket à gratter !',
                selected['token_duration_minutes'] or 30
            ))
            won_token = True

    conn.commit()
    cur.close()
    conn.close()

    return jsonify({
        'prize_name': selected['name'],
        'is_loser': selected['is_loser'],
        'won_token': won_token,
        'token_name': selected['token_name'] or selected['name'],
    })


# ── Admin routes ─────────────────────────────────────────────────────────────

@scratch_bp.route('/admin/prizes/add', methods=['POST'])
def admin_add_prize():
    if request.form.get('password') != 'Tom123':
        flash('Mot de passe admin incorrect !', 'error')
        return redirect(url_for('admin'))

    user_id = request.form.get('user_id', type=int)
    name = request.form.get('name', '').strip()
    token_name = request.form.get('token_name', '').strip()
    token_description = request.form.get('token_description', '').strip()
    token_duration = request.form.get('token_duration', 30, type=int)
    probability = request.form.get('probability', 0.0, type=float)
    is_loser = request.form.get('is_loser') == 'on'

    if not user_id or not name or probability <= 0:
        flash('Données invalides : utilisateur, nom et pourcentage sont requis.', 'error')
        return redirect(url_for('admin'))

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO scratch_prizes (user_id, name, token_name, token_description, token_duration_minutes, probability, is_loser)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
    """, (
        user_id, name,
        token_name or None,
        token_description or None,
        token_duration,
        probability,
        is_loser
    ))
    conn.commit()
    cur.close()
    conn.close()

    flash(f'Prix "{name}" ajouté pour l\'utilisateur !', 'success')
    return redirect(url_for('admin'))


@scratch_bp.route('/admin/prizes/<int:prize_id>/delete', methods=['POST'])
def admin_delete_prize(prize_id):
    if request.form.get('password') != 'Tom123':
        flash('Mot de passe admin incorrect !', 'error')
        return redirect(url_for('admin'))

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM scratch_prizes WHERE id = %s", (prize_id,))
    conn.commit()
    cur.close()
    conn.close()

    flash('Prix supprimé avec succès !', 'success')
    return redirect(url_for('admin'))
