import json
import random
from collections import defaultdict
from datetime import date, datetime
from flask import Blueprint, render_template, redirect, url_for, session, flash, request, jsonify
from psycopg2.extras import RealDictCursor
from database import get_db_connection
from auth import feature_required, admin_required
from i18n import t as _t

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
        flash(_t('scratch.flash_partner_updated'), 'success')
    except Exception as e:
        conn.rollback()
        flash(_t('scratch.flash_partner_error'), 'error')
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


@scratch_bp.route('/scratch/manage/propose', methods=['POST'])
@feature_required('scratch')
def propose_prizes():
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        partner = _get_partner(cur, session['user_id'])
        if not partner:
            flash(_t('scratch.flash_no_partner_configured'), 'error')
            return redirect(url_for('scratch.manage_prizes'))

        # A proposal always carries both pools; the reviewer sees everything at once.
        scope = 'both'

        def prizes_from_form(prefix):
            prizes = []
            for g in range(1, 6):
                tnames = request.form.getlist(f'{prefix}_g{g}_token_name')
                tdescs = request.form.getlist(f'{prefix}_g{g}_token_desc')
                tdurs  = request.form.getlist(f'{prefix}_g{g}_token_dur')
                for i, tname in enumerate(tnames):
                    tname = tname.strip()
                    if not tname:
                        continue
                    prizes.append({
                        'group_number': g,
                        'token_name': tname,
                        'token_description': tdescs[i].strip() if i < len(tdescs) else '',
                        'token_duration_minutes': int(tdurs[i]) if i < len(tdurs) and str(tdurs[i]).strip().isdigit() else 30,
                    })
            return prizes

        my_prizes = prizes_from_form('my')
        partner_prizes = prizes_from_form('partner')

        # Replace any pending proposal from this proposer to this reviewer
        cur.execute("""
            DELETE FROM scratch_prize_proposals
            WHERE proposer_id = %s AND reviewer_id = %s AND status = 'pending'
        """, (session['user_id'], partner['id']))

        cur.execute("""
            INSERT INTO scratch_prize_proposals
                (proposer_id, reviewer_id, scope, my_prizes, partner_prizes)
            VALUES (%s, %s, %s, %s::jsonb, %s::jsonb)
        """, (session['user_id'], partner['id'], scope,
              json.dumps(my_prizes) if my_prizes is not None else None,
              json.dumps(partner_prizes) if partner_prizes is not None else None))

        conn.commit()
        flash(_t('scratch.flash_proposal_sent', name=partner["username"]), 'success')
    except Exception as e:
        conn.rollback()
        flash(_t('scratch.flash_propose_error'), 'error')
        print(f"Propose prizes error: {e}")
        import traceback; traceback.print_exc()
    finally:
        cur.close()
        conn.close()
    return redirect(url_for('scratch.manage_prizes'))


@scratch_bp.route('/scratch/proposals')
@feature_required('scratch')
def proposals_page():
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("""
            SELECT p.*, u.username as proposer_username
            FROM scratch_prize_proposals p
            JOIN users u ON p.proposer_id = u.id
            WHERE p.reviewer_id = %s
            ORDER BY p.created_at DESC
            LIMIT 30
        """, (session['user_id'],))
        incoming = [dict(r) for r in cur.fetchall()]

        cur.execute("""
            SELECT p.*, u.username as reviewer_username
            FROM scratch_prize_proposals p
            JOIN users u ON p.reviewer_id = u.id
            WHERE p.proposer_id = %s
            ORDER BY p.created_at DESC
            LIMIT 30
        """, (session['user_id'],))
        outgoing = [dict(r) for r in cur.fetchall()]

        # Pre-group prizes by group_number for template rendering
        def by_group(prizes_list):
            groups = {}
            if prizes_list:
                for p in prizes_list:
                    g = int(p.get('group_number', 0))
                    groups.setdefault(g, []).append(p)
            return dict(sorted(groups.items()))

        for row in incoming:
            row['my_prizes_grouped'] = by_group(row.get('my_prizes'))
            row['partner_prizes_grouped'] = by_group(row.get('partner_prizes'))

        my_groups = _prizes_by_group(cur, session['user_id'])
    except Exception as e:
        print(f"Proposals page error: {e}")
        incoming = []
        outgoing = []
        my_groups = {i: [] for i in range(6)}
    finally:
        cur.close()
        conn.close()

    return render_template('scratch_proposals.html',
                           incoming=incoming, outgoing=outgoing,
                           my_groups=my_groups, group_pcts=GROUP_PCTS)


@scratch_bp.route('/scratch/proposals/<int:proposal_id>/review', methods=['POST'])
@feature_required('scratch')
def review_proposal(proposal_id):
    action = request.form.get('action')
    if action not in ('accept', 'reject'):
        flash(_t('scratch.flash_invalid_action'), 'error')
        return redirect(url_for('scratch.proposals_page'))

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("""
            SELECT * FROM scratch_prize_proposals
            WHERE id = %s AND reviewer_id = %s AND status = 'pending'
        """, (proposal_id, session['user_id']))
        proposal = cur.fetchone()
        if not proposal:
            flash(_t('scratch.flash_proposal_not_found'), 'error')
            return redirect(url_for('scratch.proposals_page'))

        if action == 'accept':
            def apply_prizes(uid, prizes_raw):
                if not prizes_raw:
                    return
                prizes = prizes_raw if isinstance(prizes_raw, list) else json.loads(prizes_raw)
                cur.execute("DELETE FROM scratch_prizes WHERE user_id = %s", (uid,))
                cur.execute("""
                    INSERT INTO scratch_prizes (user_id, name, probability, is_loser, group_number)
                    VALUES (%s, 'Try Again', 70.0, TRUE, 0)
                """, (uid,))
                by_g = defaultdict(list)
                for p in prizes:
                    by_g[int(p['group_number'])].append(p)
                for g, gprizes in by_g.items():
                    pct_each = round(GROUP_PCTS[g] / len(gprizes), 4)
                    for p in gprizes:
                        cur.execute("""
                            INSERT INTO scratch_prizes
                                (user_id, name, token_name, token_description,
                                 token_duration_minutes, probability, is_loser, group_number)
                            VALUES (%s, %s, %s, %s, %s, %s, FALSE, %s)
                        """, (uid, p['token_name'], p['token_name'],
                              p.get('token_description') or None,
                              p.get('token_duration_minutes', 30),
                              pct_each, g))

            scope = proposal['scope']
            if scope in ('my', 'both'):
                apply_prizes(proposal['proposer_id'], proposal['my_prizes'])
            if scope in ('partner', 'both'):
                apply_prizes(proposal['reviewer_id'], proposal['partner_prizes'])

            cur.execute("""
                UPDATE scratch_prize_proposals SET status = 'accepted', reviewed_at = NOW()
                WHERE id = %s
            """, (proposal_id,))
            conn.commit()
            flash(_t('scratch.flash_proposal_accepted'), 'success')
        else:
            cur.execute("""
                UPDATE scratch_prize_proposals SET status = 'rejected', reviewed_at = NOW()
                WHERE id = %s
            """, (proposal_id,))
            conn.commit()
            flash(_t('scratch.flash_proposal_rejected'), 'info')
    except Exception as e:
        conn.rollback()
        print(f"Review proposal error: {e}")
        import traceback; traceback.print_exc()
        flash(_t('scratch.flash_process_error'), 'error')
    finally:
        cur.close()
        conn.close()
    return redirect(url_for('scratch.proposals_page'))


@scratch_bp.route('/scratch/play', methods=['POST'])
@feature_required('scratch')
def play_scratch():
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    today = date.today()

    is_test_user = session.get('username', '').lower() == 'test'
    if not is_test_user:
        cur.execute(
            "SELECT id FROM scratch_tickets WHERE user_id = %s AND ticket_date = %s",
            (session['user_id'], today)
        )
        if cur.fetchone():
            cur.close()
            conn.close()
            return jsonify({'error': _t('scratch.error_ticket_used_today')}), 400

    cur.execute("SELECT * FROM scratch_prizes WHERE user_id = %s", (session['user_id'],))
    prizes = cur.fetchall()

    if not prizes:
        cur.close()
        conn.close()
        return jsonify({'error': _t('scratch.error_no_prizes_configured')}), 400

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
                selected['token_description'] or _t('scratch.default_token_description'),
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
@admin_required
def admin_add_prize():

    user_id = request.form.get('user_id', type=int)
    name = request.form.get('name', '').strip()
    token_name = request.form.get('token_name', '').strip()
    token_description = request.form.get('token_description', '').strip()
    token_duration = request.form.get('token_duration', 30, type=int)
    probability = request.form.get('probability', 0.0, type=float)
    is_loser = request.form.get('is_loser') == 'on'

    if not user_id or not name or probability <= 0:
        flash(_t('scratch.flash_invalid_data_prize'), 'error')
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

    flash(_t('scratch.flash_prize_added', name=name), 'success')
    return redirect(url_for('admin'))


@scratch_bp.route('/admin/prizes/<int:prize_id>/delete', methods=['POST'])
@admin_required
def admin_delete_prize(prize_id):

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM scratch_prizes WHERE id = %s", (prize_id,))
    conn.commit()
    cur.close()
    conn.close()

    flash(_t('scratch.flash_prize_deleted'), 'success')
    return redirect(url_for('admin'))
