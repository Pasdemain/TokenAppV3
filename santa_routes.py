import random
from flask import Blueprint, render_template, request, redirect, url_for, session, flash
from database import get_db_connection
from psycopg2.extras import RealDictCursor
from auth import feature_required
from i18n import t as _t

santa_bp = Blueprint('santa', __name__)

MIN_MEMBERS_FOR_DRAW = 3


def _fetch_group(cur, group_id):
    """Fetch a santa group with creator username."""
    cur.execute("""
        SELECT sg.*, u.username as creator_username
        FROM santa_groups sg
        JOIN users u ON sg.creator_id = u.id
        WHERE sg.id = %s
    """, (group_id,))
    return cur.fetchone()


def _user_is_member(cur, group_id, user_id):
    cur.execute(
        "SELECT 1 FROM santa_members WHERE group_id = %s AND user_id = %s",
        (group_id, user_id)
    )
    return cur.fetchone() is not None


@santa_bp.route('/santa')
@feature_required('santa')
def santa_home():
    """List all santa groups the user is part of (created or member)."""
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    cur.execute("""
        SELECT DISTINCT sg.*, u.username as creator_username,
               (SELECT COUNT(*) FROM santa_members WHERE group_id = sg.id) as member_count
        FROM santa_groups sg
        JOIN users u ON sg.creator_id = u.id
        LEFT JOIN santa_members sm ON sg.id = sm.group_id
        WHERE sg.creator_id = %s OR sm.user_id = %s
        ORDER BY sg.created_at DESC
    """, (session['user_id'], session['user_id']))
    groups = cur.fetchall()

    cur.close()
    conn.close()

    return render_template('santa.html', groups=groups)


@santa_bp.route('/santa/create', methods=['GET', 'POST'])
@feature_required('santa')
def create_santa():
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        description = request.form.get('description', '').strip()
        budget = request.form.get('budget', '').strip()
        event_date = request.form.get('event_date', '').strip() or None
        member_ids = request.form.getlist('members')

        if not name:
            flash(_t('santa.flash_name_required'), 'error')
        elif len(name) > 100:
            flash(_t('santa.flash_name_too_long'), 'error')
        else:
            try:
                # Create group
                cur.execute("""
                    INSERT INTO santa_groups (name, description, budget, event_date, creator_id)
                    VALUES (%s, %s, %s, %s, %s)
                    RETURNING id
                """, (name, description or None, budget or None, event_date, session['user_id']))
                group_id = cur.fetchone()['id']

                # Always add creator as a member
                cur.execute("""
                    INSERT INTO santa_members (group_id, user_id)
                    VALUES (%s, %s)
                """, (group_id, session['user_id']))

                # Add selected members (skip creator if present, and duplicates)
                added = set([session['user_id']])
                for mid in member_ids:
                    try:
                        mid_int = int(mid)
                    except (TypeError, ValueError):
                        continue
                    if mid_int in added:
                        continue
                    cur.execute("""
                        INSERT INTO santa_members (group_id, user_id)
                        VALUES (%s, %s)
                        ON CONFLICT (group_id, user_id) DO NOTHING
                    """, (group_id, mid_int))
                    added.add(mid_int)

                conn.commit()
                flash(_t('santa.flash_group_created', name=name), 'success')
                return redirect(url_for('santa.santa_detail', group_id=group_id))

            except Exception as e:
                conn.rollback()
                flash(_t('santa.flash_create_error'), 'error')
                print(f"Santa create error: {e}")

    # Get all other users for selection
    cur.execute("""
        SELECT id, username
        FROM users
        WHERE id != %s
        ORDER BY username
    """, (session['user_id'],))
    users = cur.fetchall()

    cur.close()
    conn.close()

    return render_template('create_santa.html', users=users)


@santa_bp.route('/santa/<int:group_id>')
@feature_required('santa')
def santa_detail(group_id):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    group = _fetch_group(cur, group_id)
    if not group:
        cur.close()
        conn.close()
        flash(_t('santa.flash_group_not_found'), 'error')
        return redirect(url_for('santa.santa_home'))

    # Must be a member (creator is always a member)
    if not _user_is_member(cur, group_id, session['user_id']):
        cur.close()
        conn.close()
        flash(_t('santa.flash_not_member'), 'error')
        return redirect(url_for('santa.santa_home'))

    # All members of the group
    cur.execute("""
        SELECT sm.*, u.username
        FROM santa_members sm
        JOIN users u ON sm.user_id = u.id
        WHERE sm.group_id = %s
        ORDER BY u.username
    """, (group_id,))
    members = cur.fetchall()

    # Current user's own membership row (for wishlist + assignment)
    my_row = None
    for m in members:
        if m['user_id'] == session['user_id']:
            my_row = m
            break

    # If drawn, fetch the assigned person's info + wishlist
    assigned_info = None
    if group['status'] == 'drawn' and my_row and my_row['assigned_to']:
        cur.execute("""
            SELECT u.username, sm.wishlist
            FROM santa_members sm
            JOIN users u ON sm.user_id = u.id
            WHERE sm.group_id = %s AND sm.user_id = %s
        """, (group_id, my_row['assigned_to']))
        assigned_info = cur.fetchone()

    # Users who could still be added (only if group is open)
    addable_users = []
    if group['status'] == 'open' and group['creator_id'] == session['user_id']:
        cur.execute("""
            SELECT u.id, u.username
            FROM users u
            WHERE u.id NOT IN (
                SELECT user_id FROM santa_members WHERE group_id = %s
            )
            ORDER BY u.username
        """, (group_id,))
        addable_users = cur.fetchall()

    cur.close()
    conn.close()

    is_creator = (group['creator_id'] == session['user_id'])
    can_draw = (is_creator and group['status'] == 'open'
                and len(members) >= MIN_MEMBERS_FOR_DRAW)

    return render_template('santa_detail.html',
                           group=group,
                           members=members,
                           my_row=my_row,
                           assigned_info=assigned_info,
                           addable_users=addable_users,
                           is_creator=is_creator,
                           can_draw=can_draw,
                           min_members=MIN_MEMBERS_FOR_DRAW)


@santa_bp.route('/santa/<int:group_id>/wishlist', methods=['POST'])
@feature_required('santa')
def update_wishlist(group_id):
    wishlist = request.form.get('wishlist', '').strip()

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    try:
        if not _user_is_member(cur, group_id, session['user_id']):
            flash(_t('santa.flash_not_member'), 'error')
        else:
            cur.execute("""
                UPDATE santa_members
                SET wishlist = %s
                WHERE group_id = %s AND user_id = %s
            """, (wishlist or None, group_id, session['user_id']))
            conn.commit()
            flash(_t('santa.flash_wishlist_updated'), 'success')
    except Exception as e:
        conn.rollback()
        flash(_t('santa.flash_wishlist_error'), 'error')
        print(f"Santa wishlist error: {e}")
    finally:
        cur.close()
        conn.close()

    return redirect(url_for('santa.santa_detail', group_id=group_id))


@santa_bp.route('/santa/<int:group_id>/add_member', methods=['POST'])
@feature_required('santa')
def add_member(group_id):
    new_user_id = request.form.get('user_id', type=int)

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    try:
        group = _fetch_group(cur, group_id)
        if not group:
            flash(_t('santa.flash_group_not_found'), 'error')
        elif group['creator_id'] != session['user_id']:
            flash(_t('santa.flash_only_creator_add'), 'error')
        elif group['status'] != 'open':
            flash(_t('santa.flash_no_add_after_draw'), 'warning')
        elif not new_user_id:
            flash(_t('santa.flash_select_user'), 'error')
        else:
            cur.execute("""
                INSERT INTO santa_members (group_id, user_id)
                VALUES (%s, %s)
                ON CONFLICT (group_id, user_id) DO NOTHING
            """, (group_id, new_user_id))
            conn.commit()
            flash(_t('santa.flash_member_added'), 'success')
    except Exception as e:
        conn.rollback()
        flash(_t('santa.flash_add_member_error'), 'error')
        print(f"Santa add_member error: {e}")
    finally:
        cur.close()
        conn.close()

    return redirect(url_for('santa.santa_detail', group_id=group_id))


@santa_bp.route('/santa/<int:group_id>/remove_member/<int:user_id>', methods=['POST'])
@feature_required('santa')
def remove_member(group_id, user_id):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    try:
        group = _fetch_group(cur, group_id)
        if not group:
            flash(_t('santa.flash_group_not_found'), 'error')
        elif group['creator_id'] != session['user_id']:
            flash(_t('santa.flash_only_creator_remove'), 'error')
        elif group['status'] != 'open':
            flash(_t('santa.flash_no_remove_after_draw'), 'warning')
        elif user_id == group['creator_id']:
            flash(_t('santa.flash_creator_not_removable'), 'warning')
        else:
            cur.execute("""
                DELETE FROM santa_members
                WHERE group_id = %s AND user_id = %s
            """, (group_id, user_id))
            conn.commit()
            flash(_t('santa.flash_member_removed'), 'info')
    except Exception as e:
        conn.rollback()
        flash(_t('santa.flash_remove_member_error'), 'error')
        print(f"Santa remove_member error: {e}")
    finally:
        cur.close()
        conn.close()

    return redirect(url_for('santa.santa_detail', group_id=group_id))


@santa_bp.route('/santa/<int:group_id>/draw', methods=['POST'])
@feature_required('santa')
def draw_santa(group_id):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    try:
        group = _fetch_group(cur, group_id)
        if not group:
            flash(_t('santa.flash_group_not_found'), 'error')
            return redirect(url_for('santa.santa_home'))

        if group['creator_id'] != session['user_id']:
            flash(_t('santa.flash_only_creator_draw'), 'error')
            return redirect(url_for('santa.santa_detail', group_id=group_id))

        if group['status'] != 'open':
            flash(_t('santa.flash_already_drawn'), 'warning')
            return redirect(url_for('santa.santa_detail', group_id=group_id))

        cur.execute("""
            SELECT user_id FROM santa_members
            WHERE group_id = %s
        """, (group_id,))
        member_rows = cur.fetchall()
        member_ids = [r['user_id'] for r in member_rows]

        if len(member_ids) < MIN_MEMBERS_FOR_DRAW:
            flash(
                _t('santa.flash_need_min_members', n=MIN_MEMBERS_FOR_DRAW),
                'warning'
            )
            return redirect(url_for('santa.santa_detail', group_id=group_id))

        # Shuffle and rotate — guaranteed derangement (no fixed point)
        shuffled = member_ids[:]
        random.shuffle(shuffled)
        n = len(shuffled)
        # shuffled[i] gives to shuffled[(i + 1) % n]
        for i in range(n):
            giver = shuffled[i]
            receiver = shuffled[(i + 1) % n]
            cur.execute("""
                UPDATE santa_members
                SET assigned_to = %s
                WHERE group_id = %s AND user_id = %s
            """, (receiver, group_id, giver))

        cur.execute("""
            UPDATE santa_groups
            SET status = 'drawn', drawn_at = CURRENT_TIMESTAMP
            WHERE id = %s
        """, (group_id,))
        conn.commit()
        flash(_t('santa.flash_draw_done'), 'success')

    except Exception as e:
        conn.rollback()
        flash(_t('santa.flash_draw_error'), 'error')
        print(f"Santa draw error: {e}")
    finally:
        cur.close()
        conn.close()

    return redirect(url_for('santa.santa_detail', group_id=group_id))


@santa_bp.route('/santa/<int:group_id>/delete', methods=['POST'])
@feature_required('santa')
def delete_santa(group_id):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    try:
        group = _fetch_group(cur, group_id)
        if not group:
            flash(_t('santa.flash_group_not_found'), 'error')
        elif group['creator_id'] != session['user_id']:
            flash(_t('santa.flash_only_creator_delete'), 'error')
        else:
            cur.execute("DELETE FROM santa_groups WHERE id = %s", (group_id,))
            conn.commit()
            flash(_t('santa.flash_group_deleted', name=group["name"]), 'info')
    except Exception as e:
        conn.rollback()
        flash(_t('santa.flash_delete_error'), 'error')
        print(f"Santa delete error: {e}")
    finally:
        cur.close()
        conn.close()

    return redirect(url_for('santa.santa_home'))
