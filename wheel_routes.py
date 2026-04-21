import random
from flask import Blueprint, render_template, request, jsonify, flash, redirect, url_for, session
from database import get_db_connection
from auth import feature_required
from psycopg2.extras import RealDictCursor

wheel_bp = Blueprint('wheel', __name__)


@wheel_bp.route('/wheel')
@feature_required('wheel')
def wheel_page():
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute(
            "SELECT id, name, flag_emoji FROM wheel_countries WHERE user_id = %s AND is_active = TRUE ORDER BY name",
            (session['user_id'],)
        )
        countries = cur.fetchall()
        cur.execute("""
            SELECT DISTINCT u.id, u.username
            FROM users u
            JOIN wheel_countries wc ON wc.user_id = u.id
            WHERE u.id != %s
            ORDER BY u.username
        """, (session['user_id'],))
        other_users = cur.fetchall()
    except Exception as e:
        print(f"Wheel page error: {e}")
        countries = []
        other_users = []
    finally:
        cur.close()
        conn.close()
    return render_template('wheel.html', countries=countries, other_users=other_users)


@wheel_bp.route('/wheel/spin', methods=['POST'])
@feature_required('wheel')
def spin_wheel():
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute(
            "SELECT id, name, flag_emoji FROM wheel_countries WHERE user_id = %s AND is_active = TRUE",
            (session['user_id'],)
        )
        countries = cur.fetchall()
        if not countries:
            return jsonify({'error': 'No countries available'}), 400
        result = random.choice(countries)
        return jsonify({'id': result['id'], 'name': result['name'], 'flag': result['flag_emoji']})
    except Exception as e:
        print(f"Spin error: {e}")
        return jsonify({'error': 'Spin failed'}), 500
    finally:
        cur.close()
        conn.close()


@wheel_bp.route('/wheel/countries/save', methods=['POST'])
@feature_required('wheel')
def save_countries():
    data = request.get_json()
    if data is None:
        return jsonify({'ok': False, 'error': 'No data'}), 400
    countries = data.get('countries', [])
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("DELETE FROM wheel_countries WHERE user_id = %s", (session['user_id'],))
        for c in countries:
            name = c.get('name', '').strip()
            flag = c.get('flag', '').strip()
            if name and flag:
                cur.execute(
                    "INSERT INTO wheel_countries (user_id, name, flag_emoji) VALUES (%s, %s, %s)",
                    (session['user_id'], name, flag)
                )
        conn.commit()
        return jsonify({'ok': True})
    except Exception as e:
        conn.rollback()
        print(f"Save countries error: {e}")
        return jsonify({'ok': False, 'error': str(e)}), 500
    finally:
        cur.close()
        conn.close()


@wheel_bp.route('/wheel/countries/import/<int:source_user_id>', methods=['POST'])
@feature_required('wheel')
def import_countries(source_user_id):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute(
            "SELECT name, flag_emoji FROM wheel_countries WHERE user_id = %s AND is_active = TRUE",
            (source_user_id,)
        )
        source = cur.fetchall()
        if not source:
            flash("Cet utilisateur n'a aucun pays à importer.", 'warning')
        else:
            cur2 = conn.cursor()
            cur2.execute("DELETE FROM wheel_countries WHERE user_id = %s", (session['user_id'],))
            for c in source:
                cur2.execute(
                    "INSERT INTO wheel_countries (user_id, name, flag_emoji) VALUES (%s, %s, %s)",
                    (session['user_id'], c['name'], c['flag_emoji'])
                )
            cur2.close()
            conn.commit()
            flash(f"{len(source)} pays importés !", 'success')
    except Exception as e:
        conn.rollback()
        flash("Erreur lors de l'importation.", 'error')
        print(f"Import countries error: {e}")
    finally:
        cur.close()
        conn.close()
    return redirect(url_for('wheel.wheel_page'))

