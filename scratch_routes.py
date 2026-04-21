import random
from datetime import date, datetime
from flask import Blueprint, render_template, redirect, url_for, session, flash, request, jsonify
from psycopg2.extras import RealDictCursor
from werkzeug.security import check_password_hash
from database import get_db_connection
from auth import feature_required

GROUP_PCTS = {1: 15.0, 2: 7.5, 3: 4.5, 4: 2.0, 5: 1.0}

scratch_bp = Blueprint('scratch', __name__)


def _get_partner(cur, user_id):
    """Return partner row {id, username} or None for a given user's scratch config."""
    cur.execute(
        "SELECT partner_id FROM user_features WHERE user_id = %s AND feature_name = 'scratch'",
        (user_id,)
    )
    uf = cur.fetchone()
    partner_id = uf['partner_id'] if uf and uf['partner_id'] else None
    if not partner_id:
        return None
    cur.execute("SELECT id, username FROM users WHERE id = %s", (partner_id,))
    return cur.fetchone()


def _prizes_by_group(cur, user_id):
    """Return dict {group_number: [prizes]} for a user."""
    cur.execute(
        "SELECT * FROM scratch_prizes WHERE user_id = %s ORDER BY group_number, id",
        (user_id,)
    )
    groups = {i: [] for i in range(6)}
    for p in cur.fetchall():
        g = p['group_number'] if p['group_number'] is not None else 0
        groups[g].append(p)
    return groups


@scratch_bp.route('/scratch')
@feature_required('scratch')
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

    partner = _get_partner(cur, session['user_id'])

    cur.execute(
        "SELECT id, username FROM users WHERE id != %s ORDER BY username",
        (session['user_id'],)
    )
    other_users = cur.fetchall()

    cur.close()
    conn.close()

    return render_template('scratch.html', ticket=ticket, has_prizes=has_prizes,
                           partner=partner, other_users=other_users)


@scratch_bp.route('/scratch/set_partner', methods=['POST'])
@feature_required('scratch')
def set_partner():
    partner_id = request.form.get('partner_id', type=int) or None
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO user_features (user_id, feature_name, is_enabled, partner_id)
            VALUES (%s, 'scratch', TRUE, %s)
            ON CONFLICT (user_id, feature_name)
            DO UPDATE SET partner_id = EXCLUDED.partner_id
        """, (session['user_id'], partner_id))
        conn.commit()
        flash('Partenaire mis à jour !', 'success')
    except Exception as e:
        conn.rollback()
        flash('Erreur lors de la mise à jour.', 'error')
        print(f"Set partner error: {e}")
    finally:
        cur.close()
        conn.close()
    return redirect(url_for('scratch.scratch_page'))


@scratch_bp.route('/scratch/manage')
@feature_required('scratch')
def manage_prizes():
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    partner = _get_partner(cur, session['user_id'])
    my_groups = _prizes_by_group(cur, session['user_id'])
    partner_groups = _prizes_by_group(cur, partner['id']) if partner else {i: [] for i in range(6)}

    cur.close()
    conn.close()

    return render_template('scratch_manage.html',
                           partner=partner,
                           my_groups=my_groups,
                           partner_groups=partner_groups,
                           group_pcts=GROUP_PCTS)


@scratch_bp.route('/scratch/manage/save', methods=['POST'])
@feature_required('scratch')
def save_prizes():
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    try:
        cur.execute("SELECT id, username, password_hash FROM users WHERE id = %s", (session['user_id'],))
        me = cur.fetchone()

        partner = _get_partner(cur, session['user_id'])
        if not partner:
            flash('Aucun partenaire configuré.', 'error')
            return redirect(url_for('scratch.manage_prizes'))

        cur.execute("SELECT password_hash FROM users WHERE id = %s", (partner['id'],))
        partner_row = cur.fetchone()

        my_password = request.form.get('my_password', '')
        partner_password = request.form.get('partner_password', '')

        if not check_password_hash(me['password_hash'], my_password):
            flash('Votre mot de passe est incorrect.', 'error')
            return redirect(url_for('scratch.manage_prizes'))

        if not check_password_hash(partner_row['password_hash'], partner_password):
            flash(f"Le mot de passe de {partner['username']} est incorrect.", 'error')
            return redirect(url_for('scratch.manage_prizes'))

        def save_for_user(uid, prefix):
            cur2 = conn.cursor()
            cur2.execute("DELETE FROM scratch_prizes WHERE user_id = %s", (uid,))
            # Try Again always at 70%
            cur2.execute("""
                INSERT INTO scratch_prizes (user_id, name, probability, is_loser, group_number)
                VALUES (%s, 'Try Again', 70.0, TRUE, 0)
            """, (uid,))
            for g in range(1, 6):
                names = request.form.getlist(f'{prefix}_g{g}_name')
                tnames = request.form.getlist(f'{prefix}_g{g}_token_name')
                tdurs = request.form.getlist(f'{prefix}_g{g}_token_dur')
                prizes_data = [
                    (names[i].strip(),
                     tnames[i].strip() if i < len(tnames) else '',
                     int(tdurs[i]) if i < len(tdurs) and tdurs[i].strip().isdigit() else 30)
                    for i in range(len(names)) if names[i].strip()
                ]
                if prizes_data:
                    pct_each = round(GROUP_PCTS[g] / len(prizes_data), 4)
                    for (name, tname, tdur) in prizes_data:
                        cur2.execute("""
                            INSERT INTO scratch_prizes
                                (user_id, name, token_name, token_duration_minutes, probability, is_loser, group_number)
                            VALUES (%s, %s, %s, %s, %s, FALSE, %s)
                        """, (uid, name, tname or name, tdur, pct_each, g))
            cur2.close()

        save_for_user(session['user_id'], 'my')
        save_for_user(partner['id'], 'partner')
        conn.commit()
        flash('Prix enregistrés avec succès !', 'success')
    except Exception as e:
        conn.rollback()
        flash("Erreur lors de l'enregistrement.", 'error')
        print(f"Save prizes error: {e}")
    finally:
        cur.close()
        conn.close()

    return redirect(url_for('scratch.manage_prizes'))


@scratch_bp.route('/scratch/play', methods=['POST'])
@feature_required('scratch')
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
        # Find configured scratch partner for this user
        cur.execute(
            "SELECT partner_id FROM user_features WHERE user_id = %s AND feature_name = 'scratch'",
            (session['user_id'],)
        )
        feature_row = cur.fetchone()
        partner_id = feature_row['partner_id'] if feature_row and feature_row['partner_id'] else None

        if partner_id:
            cur.execute("""
                INSERT INTO tokens (creator_id, recipient_id, name, description, duration_minutes, status)
                VALUES (%s, %s, %s, %s, %s, 'available')
            """, (
                partner_id,
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
