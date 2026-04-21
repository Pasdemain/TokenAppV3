import json
from datetime import datetime
from flask import Blueprint, render_template, request, jsonify, flash, redirect, url_for, session
from database import get_db_connection
from auth import feature_required
from psycopg2.extras import RealDictCursor

molkky_bp = Blueprint('molkky', __name__)

TEAM_COLORS = ['#6366f1', '#ef4444', '#10b981', '#f59e0b', '#3b82f6', '#ec4899']


def _serialize(obj):
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: _serialize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_serialize(v) for v in obj]
    return obj


def _get_game_state(cur, game_id):
    cur.execute("""
        SELECT g.*, u.username as creator_username
        FROM molkky_games g
        JOIN users u ON g.created_by = u.id
        WHERE g.id = %s
    """, (game_id,))
    game = cur.fetchone()
    if not game:
        return None

    cur.execute("""
        SELECT t.*,
               COALESCE(
                   json_agg(
                       json_build_object('id', m.id, 'display_name', m.display_name, 'user_id', m.user_id)
                       ORDER BY m.id
                   ) FILTER (WHERE m.id IS NOT NULL), '[]'::json
               ) as members
        FROM molkky_teams t
        LEFT JOIN molkky_members m ON m.team_id = t.id
        WHERE t.game_id = %s
        GROUP BY t.id
        ORDER BY t.turn_order
    """, (game_id,))
    teams = cur.fetchall()

    cur.execute("""
        SELECT th.*, t.name as team_name, t.color as team_color
        FROM molkky_throws th
        JOIN molkky_teams t ON th.team_id = t.id
        WHERE th.game_id = %s
        ORDER BY th.threw_at DESC
        LIMIT 15
    """, (game_id,))
    recent_throws = cur.fetchall()

    return {
        'game': dict(game),
        'teams': [dict(t) for t in teams],
        'recent_throws': [dict(th) for th in recent_throws],
    }


@molkky_bp.route('/molkky')
@feature_required('molkky')
def molkky_home():
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("""
            SELECT DISTINCT g.id, g.name, g.mode, g.status, g.created_at,
                   u.username as creator_username,
                   (SELECT COUNT(*) FROM molkky_teams WHERE game_id = g.id) as team_count
            FROM molkky_games g
            JOIN users u ON g.created_by = u.id
            LEFT JOIN molkky_members mm ON mm.game_id = g.id AND mm.user_id = %s
            WHERE g.created_by = %s OR mm.user_id = %s
            ORDER BY g.created_at DESC
            LIMIT 20
        """, (session['user_id'], session['user_id'], session['user_id']))
        games = cur.fetchall()

        cur.execute("SELECT id, username FROM users WHERE id != %s ORDER BY username",
                    (session['user_id'],))
        other_users = cur.fetchall()
    except Exception as e:
        print(f"Molkky home error: {e}")
        games = []
        other_users = []
    finally:
        cur.close()
        conn.close()
    return render_template('molkky_home.html', games=games, other_users=other_users)


@molkky_bp.route('/molkky/create', methods=['POST'])
@feature_required('molkky')
def molkky_create():
    name = request.form.get('name', '').strip() or 'Mölkky'
    mode = request.form.get('mode', 'teams')
    if mode not in ('solo', 'teams'):
        mode = 'teams'
    penalty_rule = request.form.get('penalty_rule', 'halve')
    if penalty_rule not in ('elimination', 'halve', 'conditional'):
        penalty_rule = 'halve'
    stop_on_winner = request.form.get('stop_on_winner') == '1'

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("""
            INSERT INTO molkky_games (created_by, name, mode, penalty_rule, stop_on_winner, status)
            VALUES (%s, %s, %s, %s, %s, 'setup') RETURNING id
        """, (session['user_id'], name, mode, penalty_rule, stop_on_winner))
        game_id = cur.fetchone()['id']
        if mode == 'teams':
            for i in range(4):
                color = TEAM_COLORS[i % len(TEAM_COLORS)]
                cur.execute("""
                    INSERT INTO molkky_teams (game_id, name, color, score, consecutive_zeros, is_eliminated, turn_order)
                    VALUES (%s, %s, %s, 0, 0, FALSE, %s)
                """, (game_id, f'Équipe {i+1}', color, i))
        conn.commit()
        return redirect(url_for('molkky.molkky_setup', game_id=game_id))
    except Exception as e:
        conn.rollback()
        print(f"Create game error: {e}")
        flash("Erreur lors de la création.", 'error')
        return redirect(url_for('molkky.molkky_home'))
    finally:
        cur.close()
        conn.close()


@molkky_bp.route('/molkky/setup/<int:game_id>')
@feature_required('molkky')
def molkky_setup(game_id):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("SELECT * FROM molkky_games WHERE id = %s AND created_by = %s AND status = 'setup'",
                    (game_id, session['user_id']))
        game = cur.fetchone()
        if not game:
            flash("Partie non trouvée.", 'error')
            return redirect(url_for('molkky.molkky_home'))

        cur.execute("""
            SELECT t.*,
                   COALESCE(
                       json_agg(
                           json_build_object('id', m.id, 'display_name', m.display_name, 'user_id', m.user_id)
                           ORDER BY m.id
                       ) FILTER (WHERE m.id IS NOT NULL), '[]'::json
                   ) as members
            FROM molkky_teams t
            LEFT JOIN molkky_members m ON m.team_id = t.id
            WHERE t.game_id = %s
            GROUP BY t.id
            ORDER BY t.turn_order
        """, (game_id,))
        teams = cur.fetchall()

        cur.execute("""
            SELECT * FROM molkky_members WHERE game_id = %s AND team_id IS NULL
        """, (game_id,))
        unassigned = cur.fetchall()

        cur.execute("SELECT id, username FROM users ORDER BY username")
        all_users = cur.fetchall()

        # IDs already in game
        cur.execute("SELECT user_id FROM molkky_members WHERE game_id = %s AND user_id IS NOT NULL", (game_id,))
        in_game_ids = {r['user_id'] for r in cur.fetchall()}
    except Exception as e:
        print(f"Setup page error: {e}")
        flash("Erreur.", 'error')
        return redirect(url_for('molkky.molkky_home'))
    finally:
        cur.close()
        conn.close()

    return render_template('molkky_setup.html', game=game, teams=teams, unassigned=unassigned,
                           all_users=all_users, in_game_ids=in_game_ids, team_colors=TEAM_COLORS)


@molkky_bp.route('/molkky/setup/<int:game_id>/action', methods=['POST'])
@feature_required('molkky')
def molkky_setup_action(game_id):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("SELECT * FROM molkky_games WHERE id = %s AND created_by = %s AND status = 'setup'",
                    (game_id, session['user_id']))
        game = cur.fetchone()
        if not game:
            return jsonify({'ok': False, 'error': 'Partie non trouvée'}), 404

        data = request.get_json() or {}
        action = data.get('action')

        if action == 'add_team':
            cur.execute("SELECT COUNT(*) as cnt FROM molkky_teams WHERE game_id = %s", (game_id,))
            count = cur.fetchone()['cnt']
            color = TEAM_COLORS[count % len(TEAM_COLORS)]
            name = data.get('name', f'Équipe {count + 1}').strip() or f'Équipe {count + 1}'
            cur.execute("""
                INSERT INTO molkky_teams (game_id, name, color, score, consecutive_zeros, is_eliminated, turn_order)
                VALUES (%s, %s, %s, 0, 0, FALSE, %s) RETURNING id, name, color, turn_order
            """, (game_id, name, color, count))
            team = cur.fetchone()
            conn.commit()
            return jsonify({'ok': True, 'team': dict(team)})

        elif action == 'rename_team':
            team_id = data.get('team_id')
            name = data.get('name', '').strip()
            if not name or not team_id:
                return jsonify({'ok': False, 'error': 'Données invalides'})
            cur.execute("UPDATE molkky_teams SET name = %s WHERE id = %s AND game_id = %s",
                        (name, team_id, game_id))
            conn.commit()
            return jsonify({'ok': True})

        elif action == 'remove_team':
            team_id = data.get('team_id')
            cur.execute("UPDATE molkky_members SET team_id = NULL WHERE team_id = %s AND game_id = %s",
                        (team_id, game_id))
            cur.execute("DELETE FROM molkky_teams WHERE id = %s AND game_id = %s", (team_id, game_id))
            conn.commit()
            return jsonify({'ok': True})

        elif action == 'add_player':
            user_id = data.get('user_id')
            guest_name = (data.get('guest_name') or '').strip()
            team_id = data.get('team_id')

            if user_id:
                cur.execute("SELECT username FROM users WHERE id = %s", (user_id,))
                u = cur.fetchone()
                if not u:
                    return jsonify({'ok': False, 'error': 'Utilisateur introuvable'})
                cur.execute("SELECT id FROM molkky_members WHERE game_id = %s AND user_id = %s",
                            (game_id, user_id))
                if cur.fetchone():
                    return jsonify({'ok': False, 'error': 'Joueur déjà dans la partie'})
                display_name = u['username']
                cur.execute("""
                    INSERT INTO molkky_members (game_id, team_id, user_id, display_name)
                    VALUES (%s, %s, %s, %s) RETURNING id, display_name, user_id, team_id
                """, (game_id, team_id, user_id, display_name))
            elif guest_name:
                cur.execute("""
                    INSERT INTO molkky_members (game_id, team_id, user_id, guest_name, display_name)
                    VALUES (%s, %s, NULL, %s, %s) RETURNING id, display_name, user_id, team_id
                """, (game_id, team_id, guest_name, guest_name))
            else:
                return jsonify({'ok': False, 'error': 'Aucun joueur spécifié'})

            member = cur.fetchone()
            conn.commit()
            return jsonify({'ok': True, 'member': dict(member)})

        elif action == 'remove_player':
            member_id = data.get('member_id')
            cur.execute("DELETE FROM molkky_members WHERE id = %s AND game_id = %s", (member_id, game_id))
            conn.commit()
            return jsonify({'ok': True})

        elif action == 'assign_team':
            member_id = data.get('member_id')
            team_id = data.get('team_id')  # None = unassign
            cur.execute("UPDATE molkky_members SET team_id = %s WHERE id = %s AND game_id = %s",
                        (team_id, member_id, game_id))
            conn.commit()
            return jsonify({'ok': True})

        elif action == 'start':
            if game['mode'] == 'solo':
                cur.execute("SELECT id, display_name FROM molkky_members WHERE game_id = %s ORDER BY id",
                            (game_id,))
                all_members = cur.fetchall()
                if len(all_members) < 2:
                    return jsonify({'ok': False, 'error': 'Au moins 2 joueurs requis.'})
                cur.execute("DELETE FROM molkky_teams WHERE game_id = %s", (game_id,))
                cur.execute("UPDATE molkky_members SET team_id = NULL WHERE game_id = %s", (game_id,))
                for i, member in enumerate(all_members):
                    color = TEAM_COLORS[i % len(TEAM_COLORS)]
                    cur.execute("""
                        INSERT INTO molkky_teams (game_id, name, color, score, consecutive_zeros, is_eliminated, turn_order)
                        VALUES (%s, %s, %s, 0, 0, FALSE, %s) RETURNING id
                    """, (game_id, member['display_name'], color, i))
                    new_team_id = cur.fetchone()['id']
                    cur.execute("UPDATE molkky_members SET team_id = %s WHERE id = %s",
                                (new_team_id, member['id']))
            else:
                cur.execute("""
                    SELECT t.id, t.name, t.turn_order, COUNT(m.id) as member_count
                    FROM molkky_teams t
                    LEFT JOIN molkky_members m ON m.team_id = t.id
                    WHERE t.game_id = %s GROUP BY t.id, t.name, t.turn_order
                    ORDER BY t.turn_order
                """, (game_id,))
                all_teams = cur.fetchall()
                non_empty = [t for t in all_teams if t['member_count'] > 0]
                empty_ids = [t['id'] for t in all_teams if t['member_count'] == 0]
                if len(non_empty) < 2:
                    return jsonify({'ok': False, 'error': 'Au moins 2 équipes avec des joueurs requis.'})
                for tid in empty_ids:
                    cur.execute("DELETE FROM molkky_teams WHERE id = %s", (tid,))
                for i, t in enumerate(non_empty):
                    cur.execute("UPDATE molkky_teams SET turn_order = %s WHERE id = %s", (i, t['id']))

            cur.execute("SELECT id FROM molkky_teams WHERE game_id = %s ORDER BY turn_order LIMIT 1",
                        (game_id,))
            first_team = cur.fetchone()
            cur.execute("UPDATE molkky_games SET status = 'active', current_team_id = %s WHERE id = %s",
                        (first_team['id'], game_id))
            conn.commit()
            return jsonify({'ok': True, 'redirect': url_for('molkky.molkky_game', game_id=game_id)})

        return jsonify({'ok': False, 'error': 'Action inconnue'}), 400

    except Exception as e:
        conn.rollback()
        print(f"Setup action error: {e}")
        import traceback; traceback.print_exc()
        return jsonify({'ok': False, 'error': str(e)}), 500
    finally:
        cur.close()
        conn.close()


@molkky_bp.route('/molkky/game/<int:game_id>')
@feature_required('molkky')
def molkky_game(game_id):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("""
            SELECT g.* FROM molkky_games g
            LEFT JOIN molkky_members mm ON mm.game_id = g.id AND mm.user_id = %s
            WHERE g.id = %s AND (g.created_by = %s OR mm.user_id IS NOT NULL)
        """, (session['user_id'], game_id, session['user_id']))
        game = cur.fetchone()
        if not game:
            flash("Partie non trouvée.", 'error')
            return redirect(url_for('molkky.molkky_home'))
        if game['status'] == 'setup':
            return redirect(url_for('molkky.molkky_setup', game_id=game_id))
        state = _get_game_state(cur, game_id)
        is_controller = (game['created_by'] == session['user_id'])
    except Exception as e:
        print(f"Game page error: {e}")
        flash("Erreur.", 'error')
        return redirect(url_for('molkky.molkky_home'))
    finally:
        cur.close()
        conn.close()

    return render_template('molkky_game.html', game=game, state=state, is_controller=is_controller)


@molkky_bp.route('/molkky/game/<int:game_id>/state')
@feature_required('molkky')
def molkky_state(game_id):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("""
            SELECT g.id FROM molkky_games g
            LEFT JOIN molkky_members mm ON mm.game_id = g.id AND mm.user_id = %s
            WHERE g.id = %s AND (g.created_by = %s OR mm.user_id IS NOT NULL)
        """, (session['user_id'], game_id, session['user_id']))
        if not cur.fetchone():
            return jsonify({'error': 'Not found'}), 404
        state = _get_game_state(cur, game_id)
        return jsonify(_serialize(state))
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        cur.close()
        conn.close()


@molkky_bp.route('/molkky/game/<int:game_id>/throw', methods=['POST'])
@feature_required('molkky')
def molkky_throw(game_id):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("SELECT * FROM molkky_games WHERE id = %s AND created_by = %s AND status = 'active'",
                    (game_id, session['user_id']))
        game = cur.fetchone()
        if not game:
            return jsonify({'ok': False, 'error': 'Non autorisé ou partie non active'}), 403

        data = request.get_json() or {}
        raw_pins = data.get('pins', [])
        pins = [int(p) for p in raw_pins if 1 <= int(p) <= 12]

        if len(pins) == 0:
            score_gained = 0
        elif len(pins) == 1:
            score_gained = pins[0]
        else:
            score_gained = len(pins)

        team_id = game['current_team_id']
        cur.execute("SELECT * FROM molkky_teams WHERE id = %s", (team_id,))
        team = cur.fetchone()

        score_before = team['score']
        new_score = score_before + score_gained
        consecutive_zeros = team['consecutive_zeros']
        is_eliminated = team['is_eliminated']

        if new_score > 50:
            new_score = 25

        if score_gained == 0:
            consecutive_zeros += 1
        else:
            consecutive_zeros = 0

        penalty_applied = False
        if consecutive_zeros >= 3:
            penalty_applied = True
            consecutive_zeros = 0
            rule = game['penalty_rule']
            if rule == 'elimination':
                is_eliminated = True
            elif rule == 'halve':
                new_score = new_score // 2
            elif rule == 'conditional':
                new_score = 0 if new_score < 25 else 25

        winner = (new_score == 50 and not is_eliminated)

        # Advance within-team member rotation
        cur.execute("SELECT COUNT(*) as cnt FROM molkky_members WHERE team_id = %s", (team_id,))
        member_count = cur.fetchone()['cnt']
        old_member_idx = team.get('current_member_idx', 0) or 0
        new_member_idx = (old_member_idx + 1) % member_count if member_count > 0 else 0

        cur.execute("""
            UPDATE molkky_teams
            SET score = %s, consecutive_zeros = %s, is_eliminated = %s, current_member_idx = %s
            WHERE id = %s
        """, (new_score, consecutive_zeros, is_eliminated, new_member_idx, team_id))

        cur.execute("""
            INSERT INTO molkky_throws (game_id, team_id, pins_knocked, score_gained,
                                       score_before, score_after,
                                       consecutive_zeros_before, is_eliminated_before,
                                       member_idx_before)
            VALUES (%s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s)
        """, (game_id, team_id, json.dumps(pins), score_gained,
              score_before, new_score,
              team['consecutive_zeros'], team['is_eliminated'], old_member_idx))

        new_status = 'active'
        winner_team_id = None
        next_team_id = None

        if winner:
            new_status = 'finished'
            winner_team_id = team_id
        else:
            cur.execute("""
                SELECT id, turn_order FROM molkky_teams
                WHERE game_id = %s AND is_eliminated = FALSE ORDER BY turn_order
            """, (game_id,))
            active_teams = cur.fetchall()

            if len(active_teams) == 0:
                new_status = 'finished'
            elif len(active_teams) == 1:
                new_status = 'finished'
                winner_team_id = active_teams[0]['id']
            else:
                current_turn_order = team['turn_order']
                next_team = None
                for t in active_teams:
                    if t['turn_order'] > current_turn_order:
                        next_team = t
                        break
                if next_team is None:
                    next_team = active_teams[0]
                next_team_id = next_team['id']

        cur.execute("""
            UPDATE molkky_games
            SET status = %s, current_team_id = %s, winner_team_id = %s,
                finished_at = CASE WHEN %s = 'finished' THEN NOW() ELSE finished_at END
            WHERE id = %s
        """, (new_status, next_team_id, winner_team_id, new_status, game_id))

        conn.commit()
        state = _get_game_state(cur, game_id)
        return jsonify({'ok': True, 'state': _serialize(state), 'winner': winner,
                        'penalty_applied': penalty_applied, 'game_over': new_status == 'finished'})

    except Exception as e:
        conn.rollback()
        print(f"Throw error: {e}")
        import traceback; traceback.print_exc()
        return jsonify({'ok': False, 'error': str(e)}), 500
    finally:
        cur.close()
        conn.close()


@molkky_bp.route('/molkky/game/<int:game_id>/undo', methods=['POST'])
@feature_required('molkky')
def molkky_undo(game_id):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("SELECT * FROM molkky_games WHERE id = %s AND created_by = %s",
                    (game_id, session['user_id']))
        game = cur.fetchone()
        if not game:
            return jsonify({'ok': False, 'error': 'Non autorisé'}), 403

        cur.execute("""
            SELECT * FROM molkky_throws WHERE game_id = %s ORDER BY threw_at DESC LIMIT 1
        """, (game_id,))
        last_throw = cur.fetchone()
        if not last_throw:
            return jsonify({'ok': False, 'error': 'Aucun lancer à annuler'})

        cur.execute("""
            UPDATE molkky_teams
            SET score = %s, consecutive_zeros = %s, is_eliminated = %s, current_member_idx = %s
            WHERE id = %s
        """, (last_throw['score_before'], last_throw['consecutive_zeros_before'],
              last_throw['is_eliminated_before'], last_throw['member_idx_before'],
              last_throw['team_id']))

        cur.execute("DELETE FROM molkky_throws WHERE id = %s", (last_throw['id'],))

        cur.execute("""
            UPDATE molkky_games
            SET status = 'active', current_team_id = %s, winner_team_id = NULL, finished_at = NULL
            WHERE id = %s
        """, (last_throw['team_id'], game_id))

        conn.commit()
        state = _get_game_state(cur, game_id)
        return jsonify({'ok': True, 'state': _serialize(state)})
    except Exception as e:
        conn.rollback()
        print(f"Undo error: {e}")
        return jsonify({'ok': False, 'error': str(e)}), 500
    finally:
        cur.close()
        conn.close()


@molkky_bp.route('/molkky/game/<int:game_id>/rematch', methods=['POST'])
@feature_required('molkky')
def molkky_rematch(game_id):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("SELECT * FROM molkky_games WHERE id = %s AND created_by = %s", (game_id, session['user_id']))
        old_game = cur.fetchone()
        if not old_game:
            flash("Partie non trouvée.", 'error')
            return redirect(url_for('molkky.molkky_home'))

        cur.execute("""
            INSERT INTO molkky_games (created_by, name, mode, penalty_rule, stop_on_winner, status)
            VALUES (%s, %s, %s, %s, %s, 'active') RETURNING id
        """, (session['user_id'], old_game['name'], old_game['mode'],
              old_game['penalty_rule'], old_game['stop_on_winner']))
        new_game_id = cur.fetchone()['id']

        cur.execute("SELECT * FROM molkky_teams WHERE game_id = %s ORDER BY turn_order", (game_id,))
        old_teams = cur.fetchall()
        team_id_map = {}
        for t in old_teams:
            cur.execute("""
                INSERT INTO molkky_teams (game_id, name, color, score, consecutive_zeros, is_eliminated, turn_order)
                VALUES (%s, %s, %s, 0, 0, FALSE, %s) RETURNING id
            """, (new_game_id, t['name'], t['color'], t['turn_order']))
            team_id_map[t['id']] = cur.fetchone()['id']

        cur.execute("SELECT * FROM molkky_members WHERE game_id = %s", (game_id,))
        for m in cur.fetchall():
            new_tid = team_id_map.get(m['team_id']) if m['team_id'] else None
            cur.execute("""
                INSERT INTO molkky_members (game_id, team_id, user_id, guest_name, display_name)
                VALUES (%s, %s, %s, %s, %s)
            """, (new_game_id, new_tid, m['user_id'], m['guest_name'], m['display_name']))

        if old_teams:
            first_new_id = team_id_map[old_teams[0]['id']]
            cur.execute("UPDATE molkky_games SET current_team_id = %s WHERE id = %s",
                        (first_new_id, new_game_id))

        conn.commit()
        return redirect(url_for('molkky.molkky_game', game_id=new_game_id))
    except Exception as e:
        conn.rollback()
        print(f"Rematch error: {e}")
        flash("Erreur lors du rematch.", 'error')
        return redirect(url_for('molkky.molkky_home'))
    finally:
        cur.close()
        conn.close()
