from datetime import datetime
from flask import (Blueprint, render_template, request, redirect, url_for,
                   flash, make_response)
from psycopg2.extras import RealDictCursor
from database import get_db_connection

trip_bp = Blueprint('trip', __name__)

NAME_COOKIE = 'trip_name'

# Activity categories (value -> "emoji Label")
CATEGORIES = [
    ('rando',  '🥾 Rando'),
    ('plage',  '🏖️ Plage'),
    ('resto',  '🍽️ Resto'),
    ('visite', '🏰 Visite'),
    ('creperie', '🥞 Crêperie'),
    ('soiree', '🎉 Soirée'),
    ('trajet', '🚗 Trajet'),
    ('sport',  '🛶 Sport'),
    ('autre',  '📌 Autre'),
]
CATEGORY_ICON = {k: v.split(' ', 1)[0] for k, v in CATEGORIES}
CATEGORY_LABEL = dict(CATEGORIES)


def _current_name():
    return (request.cookies.get(NAME_COOKIE) or '').strip()[:60]


def _normalize_url(raw):
    """Return a safe http(s) URL, or None if invalid/unsafe."""
    url = (raw or '').strip()
    if not url:
        return None
    if '://' not in url:
        url = 'https://' + url
    low = url.lower()
    if not (low.startswith('http://') or low.startswith('https://')):
        return None
    return url[:500]


@trip_bp.route('/voyage')
def trip_page():
    name = _current_name()
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    cur.execute("""
        SELECT * FROM trip_activities
        ORDER BY activity_date ASC NULLS LAST, id ASC
    """)
    activities = cur.fetchall()

    for a in activities:
        cur.execute(
            "SELECT name FROM trip_signups WHERE activity_id = %s ORDER BY created_at",
            (a['id'],)
        )
        participants = [r['name'] for r in cur.fetchall()]
        a['participants'] = participants
        a['count'] = len(participants)
        a['is_full'] = a['capacity'] is not None and a['count'] >= a['capacity']
        a['joined'] = name in participants
        a['icon'] = CATEGORY_ICON.get(a['category'], '📌')
        a['cat_label'] = CATEGORY_LABEL.get(a['category'], '📌 Autre')

    cur.execute("SELECT * FROM trip_links ORDER BY created_at")
    links = cur.fetchall()

    cur.close()
    conn.close()

    # Group by day (dated first, undated last)
    buckets = {}
    for a in activities:
        buckets.setdefault(a['activity_date'], []).append(a)
    dated = sorted(k for k in buckets if k is not None)
    groups = [{'date': k, 'activities': buckets[k]} for k in dated]
    if None in buckets:
        groups.append({'date': None, 'activities': buckets[None]})

    total_people = len({p for a in activities for p in a['participants']})

    return render_template('trip.html', groups=groups, my_name=name,
                           categories=CATEGORIES, total_activities=len(activities),
                           total_people=total_people, links=links)


@trip_bp.route('/voyage/set-name', methods=['POST'])
def set_name():
    name = (request.form.get('name') or '').strip()[:60]
    resp = make_response(redirect(url_for('trip.trip_page')))
    if name:
        resp.set_cookie(NAME_COOKIE, name, max_age=365 * 24 * 3600, samesite='Lax')
    else:
        resp.delete_cookie(NAME_COOKIE)
        flash("Indique ton prénom pour participer.", 'warning')
    return resp


@trip_bp.route('/voyage/activity', methods=['POST'])
def add_activity():
    name = _current_name()
    if not name:
        flash("Renseigne d'abord ton prénom en haut de la page.", 'warning')
        return redirect(url_for('trip.trip_page'))

    title = (request.form.get('title') or '').strip()[:120]
    if not title:
        flash("Donne un titre à l'activité.", 'error')
        return redirect(url_for('trip.trip_page'))

    description = (request.form.get('description') or '').strip() or None
    category = request.form.get('category', 'autre')
    if category not in CATEGORY_LABEL:
        category = 'autre'
    location = (request.form.get('location') or '').strip()[:120] or None
    start_time = (request.form.get('start_time') or '').strip()[:20] or None
    capacity = request.form.get('capacity', type=int)
    if capacity is not None and capacity <= 0:
        capacity = None

    date_raw = (request.form.get('activity_date') or '').strip()
    activity_date = None
    if date_raw:
        try:
            activity_date = datetime.fromisoformat(date_raw).date()
        except ValueError:
            activity_date = None

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO trip_activities
                (title, description, category, activity_date, start_time, location, capacity, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """, (title, description, category, activity_date, start_time, location, capacity, name))
        conn.commit()
        flash("Activité ajoutée ! 🎉", 'success')
    except Exception as e:
        conn.rollback()
        flash("Oups, l'activité n'a pas pu être ajoutée.", 'error')
        print(f"Trip add activity error: {e}")
    finally:
        cur.close()
        conn.close()
    return redirect(url_for('trip.trip_page'))


@trip_bp.route('/voyage/activity/<int:activity_id>/join', methods=['POST'])
def join_activity(activity_id):
    name = _current_name()
    if not name:
        flash("Renseigne d'abord ton prénom en haut de la page.", 'warning')
        return redirect(url_for('trip.trip_page'))

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("SELECT capacity FROM trip_activities WHERE id = %s", (activity_id,))
        act = cur.fetchone()
        if not act:
            flash("Activité introuvable.", 'error')
            return redirect(url_for('trip.trip_page'))
        if act['capacity'] is not None:
            cur.execute("SELECT COUNT(*) AS c FROM trip_signups WHERE activity_id = %s", (activity_id,))
            already = cur.fetchone()['c']
            cur.execute(
                "SELECT 1 FROM trip_signups WHERE activity_id = %s AND name = %s",
                (activity_id, name)
            )
            is_in = cur.fetchone() is not None
            if not is_in and already >= act['capacity']:
                flash("C'est complet pour cette activité 😢", 'warning')
                return redirect(url_for('trip.trip_page'))
        cur.execute("""
            INSERT INTO trip_signups (activity_id, name) VALUES (%s, %s)
            ON CONFLICT (activity_id, name) DO NOTHING
        """, (activity_id, name))
        conn.commit()
        flash("Inscrit ! ⛵", 'success')
    except Exception as e:
        conn.rollback()
        flash("Erreur lors de l'inscription.", 'error')
        print(f"Trip join error: {e}")
    finally:
        cur.close()
        conn.close()
    return redirect(url_for('trip.trip_page'))


@trip_bp.route('/voyage/activity/<int:activity_id>/leave', methods=['POST'])
def leave_activity(activity_id):
    name = _current_name()
    if name:
        conn = get_db_connection()
        cur = conn.cursor()
        try:
            cur.execute(
                "DELETE FROM trip_signups WHERE activity_id = %s AND name = %s",
                (activity_id, name)
            )
            conn.commit()
            flash("Désinscrit.", 'info')
        finally:
            cur.close()
            conn.close()
    return redirect(url_for('trip.trip_page'))


@trip_bp.route('/voyage/activity/<int:activity_id>/delete', methods=['POST'])
def delete_activity(activity_id):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("DELETE FROM trip_activities WHERE id = %s", (activity_id,))
        conn.commit()
        flash("Activité supprimée.", 'info')
    finally:
        cur.close()
        conn.close()
    return redirect(url_for('trip.trip_page'))


@trip_bp.route('/voyage/link', methods=['POST'])
def add_link():
    url = _normalize_url(request.form.get('url'))
    if not url:
        flash("Lien invalide (il doit commencer par http:// ou https://).", 'error')
        return redirect(url_for('trip.trip_page'))
    label = (request.form.get('label') or '').strip()[:80] or None

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO trip_links (label, url, added_by) VALUES (%s, %s, %s)",
            (label, url, _current_name() or None)
        )
        conn.commit()
        flash("Lien ajouté ! 🔗", 'success')
    except Exception as e:
        conn.rollback()
        flash("Le lien n'a pas pu être ajouté.", 'error')
        print(f"Trip add link error: {e}")
    finally:
        cur.close()
        conn.close()
    return redirect(url_for('trip.trip_page'))


@trip_bp.route('/voyage/link/<int:link_id>/delete', methods=['POST'])
def delete_link(link_id):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("DELETE FROM trip_links WHERE id = %s", (link_id,))
        conn.commit()
        flash("Lien supprimé.", 'info')
    finally:
        cur.close()
        conn.close()
    return redirect(url_for('trip.trip_page'))
