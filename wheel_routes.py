import random
from flask import Blueprint, render_template, request, jsonify, flash, redirect, url_for
from database import get_db_connection
from auth import login_required
from psycopg2.extras import RealDictCursor

wheel_bp = Blueprint('wheel', __name__)

ADMIN_PASSWORD = 'Tom123'


@wheel_bp.route('/wheel')
@login_required
def wheel_page():
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute(
            "SELECT id, name, flag_emoji FROM wheel_countries WHERE is_active = TRUE ORDER BY name"
        )
        countries = cur.fetchall()
    except Exception as e:
        print(f"Wheel page error: {e}")
        countries = []
    finally:
        cur.close()
        conn.close()
    return render_template('wheel.html', countries=countries)


@wheel_bp.route('/wheel/spin', methods=['POST'])
@login_required
def spin_wheel():
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute(
            "SELECT id, name, flag_emoji FROM wheel_countries WHERE is_active = TRUE"
        )
        countries = cur.fetchall()

        if not countries:
            return jsonify({'error': 'No countries available'}), 400

        result = random.choice(countries)
        return jsonify({
            'id': result['id'],
            'name': result['name'],
            'flag': result['flag_emoji']
        })
    except Exception as e:
        print(f"Spin error: {e}")
        return jsonify({'error': 'Spin failed'}), 500
    finally:
        cur.close()
        conn.close()


@wheel_bp.route('/admin/wheel/countries/add', methods=['POST'])
def add_country():
    if request.form.get('password', '') != ADMIN_PASSWORD:
        flash('Invalid admin password!', 'error')
        return redirect(url_for('admin'))

    name = request.form.get('name', '').strip()
    flag_emoji = request.form.get('flag_emoji', '').strip()

    if not name or not flag_emoji:
        flash('Country name and flag are required!', 'error')
        return redirect(url_for('admin'))

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO wheel_countries (name, flag_emoji) VALUES (%s, %s)",
            (name, flag_emoji)
        )
        conn.commit()
        flash(f'Country "{name}" added!', 'success')
    except Exception as e:
        conn.rollback()
        flash('Error adding country.', 'error')
        print(f"Add country error: {e}")
    finally:
        cur.close()
        conn.close()

    return redirect(url_for('admin'))


@wheel_bp.route('/admin/wheel/countries/<int:country_id>/delete', methods=['POST'])
def delete_country(country_id):
    if request.form.get('password', '') != ADMIN_PASSWORD:
        flash('Invalid admin password!', 'error')
        return redirect(url_for('admin'))

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("DELETE FROM wheel_countries WHERE id = %s", (country_id,))
        conn.commit()
        flash('Country deleted.', 'info')
    except Exception as e:
        conn.rollback()
        flash('Error deleting country.', 'error')
        print(f"Delete country error: {e}")
    finally:
        cur.close()
        conn.close()

    return redirect(url_for('admin'))
