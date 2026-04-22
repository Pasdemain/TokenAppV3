import os
import secrets
from flask import Blueprint, render_template, request, redirect, url_for, session, flash, make_response, current_app
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
from database import get_db_connection
from psycopg2.extras import RealDictCursor
from i18n import t as _t

auth_bp = Blueprint('auth', __name__)

REMEMBER_COOKIE = 'remember_token'
REMEMBER_DAYS = 30
IS_HTTPS = os.environ.get('RENDER_EXTERNAL_URL', '').startswith('https')


ALL_FEATURES = ['tokens', 'shopping', 'scratch', 'wheel', 'flashcards', 'competency', 'santa', 'molkky']


def _restore_session_from_cookie():
    """Try to restore session from remember-me cookie. Returns True if session restored."""
    token = request.cookies.get(REMEMBER_COOKIE)
    if not token:
        return False
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute(
            "SELECT id, username, is_admin, app_name, app_icon, lang FROM users WHERE remember_token = %s", (token,)
        )
        user = cur.fetchone()
        if user:
            session.permanent = True
            session['user_id'] = user['id']
            session['username'] = user['username']
            session['is_admin'] = bool(user.get('is_admin', False))
            session['app_name'] = user.get('app_name')
            session['app_icon'] = user.get('app_icon') or '💑'
            if user.get('lang'):
                session['lang'] = user['lang']
            return True
    except Exception as e:
        print(f"Remember me error: {e}")
    finally:
        cur.close()
        conn.close()
    return False


def login_required(f):
    """Decorator to require login for routes"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            if not _restore_session_from_cookie():
                flash(_t('auth.please_login'), 'warning')
                return redirect(url_for('auth.login'))
        return f(*args, **kwargs)
    return decorated_function


def admin_required(f):
    """Decorator to require admin role."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            if not _restore_session_from_cookie():
                flash(_t('auth.please_login'), 'warning')
                return redirect(url_for('auth.login'))
        if not session.get('is_admin'):
            # Re-check DB in case is_admin was set after login
            conn = get_db_connection()
            cur = conn.cursor(cursor_factory=RealDictCursor)
            try:
                cur.execute("SELECT is_admin FROM users WHERE id = %s", (session['user_id'],))
                row = cur.fetchone()
                if row and row['is_admin']:
                    session['is_admin'] = True
            except Exception as e:
                print(f"Admin check error: {e}")
            finally:
                cur.close()
                conn.close()
        if not session.get('is_admin'):
            flash(_t('auth.admin_only'), 'error')
            return redirect(url_for('dashboard'))
        return f(*args, **kwargs)
    return decorated_function


def feature_required(feature_name):
    """Decorator to require login AND that the feature is enabled for the user."""
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if 'user_id' not in session:
                if not _restore_session_from_cookie():
                    flash(_t('auth.please_login'), 'warning')
                    return redirect(url_for('auth.login'))
            conn = get_db_connection()
            cur = conn.cursor(cursor_factory=RealDictCursor)
            try:
                cur.execute(
                    "SELECT is_enabled FROM user_features WHERE user_id = %s AND feature_name = %s",
                    (session['user_id'], feature_name)
                )
                row = cur.fetchone()
            finally:
                cur.close()
                conn.close()
            if not row or not row['is_enabled']:
                flash(_t('auth.feature_not_allowed'), 'error')
                return redirect(url_for('dashboard'))
            return f(*args, **kwargs)
        return decorated_function
    return decorator


@auth_bp.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        confirm_password = request.form.get('confirm_password', '')

        if not username or not password:
            flash(_t('auth.err_username_password_required'), 'error')
            return render_template('register.html')

        if len(username) > 20:
            flash(_t('auth.err_username_too_long'), 'error')
            return render_template('register.html')

        if password != confirm_password:
            flash(_t('auth.err_passwords_mismatch'), 'error')
            return render_template('register.html')

        if len(password) < 6:
            flash(_t('auth.err_password_too_short'), 'error')
            return render_template('register.html')

        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        try:
            cur.execute("SELECT id FROM users WHERE username = %s", (username,))
            if cur.fetchone():
                flash(_t('auth.err_username_taken'), 'error')
                return render_template('register.html')

            password_hash = generate_password_hash(password)
            cur.execute(
                "INSERT INTO users (username, password_hash) VALUES (%s, %s) RETURNING id",
                (username, password_hash)
            )
            user_id = cur.fetchone()['id']

            cur.execute("SELECT feature_name, is_enabled FROM feature_defaults")
            defaults = cur.fetchall()
            # Fall back to all-enabled if no defaults configured yet
            if not defaults:
                defaults = [{'feature_name': f, 'is_enabled': True} for f in ALL_FEATURES]
            for d in defaults:
                cur.execute(
                    "INSERT INTO user_features (user_id, feature_name, is_enabled) VALUES (%s, %s, %s)",
                    (user_id, d['feature_name'], d['is_enabled'])
                )
            conn.commit()

            session['user_id'] = user_id
            session['username'] = username
            session['app_name'] = None  # triggers first-visit popup
            session['app_icon'] = '💑'

            flash(_t('auth.register_success'), 'success')
            return redirect(url_for('dashboard'))

        except Exception as e:
            conn.rollback()
            flash(_t('auth.err_register_generic'), 'error')
            print(f"Registration error: {e}")
        finally:
            cur.close()
            conn.close()

    return render_template('register.html')


@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        remember = request.form.get('remember_me') == 'on'

        if not username or not password:
            flash(_t('auth.err_username_password_required'), 'error')
            return render_template('login.html')

        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        try:
            cur.execute(
                "SELECT id, username, password_hash, is_admin, app_name, app_icon, lang FROM users WHERE username = %s",
                (username,)
            )
            user = cur.fetchone()

            if user and check_password_hash(user['password_hash'], password):
                session.permanent = True
                session['user_id'] = user['id']
                session['username'] = user['username']
                session['is_admin'] = bool(user.get('is_admin', False))
                session['app_name'] = user.get('app_name')
                session['app_icon'] = user.get('app_icon') or '💑'
                if user.get('lang'):
                    session['lang'] = user['lang']

                response = make_response(redirect(url_for('dashboard')))

                if remember:
                    token = secrets.token_hex(32)
                    cur.execute(
                        "UPDATE users SET remember_token = %s WHERE id = %s",
                        (token, user['id'])
                    )
                    conn.commit()
                    response.set_cookie(
                        REMEMBER_COOKIE, token,
                        max_age=REMEMBER_DAYS * 24 * 3600,
                        httponly=True, samesite='Lax',
                        secure=IS_HTTPS
                    )

                flash(_t('auth.welcome_back', username=username), 'success')
                return response
            else:
                flash(_t('auth.err_invalid_credentials'), 'error')

        except Exception as e:
            flash(_t('auth.err_login_generic'), 'error')
            print(f"Login error: {e}")
        finally:
            cur.close()
            conn.close()

    return render_template('login.html')


@auth_bp.route('/logout')
def logout():
    username = session.get('username', 'User')
    user_id = session.get('user_id')
    session.clear()

    response = make_response(redirect(url_for('auth.login')))
    response.delete_cookie(REMEMBER_COOKIE)

    if user_id:
        conn = get_db_connection()
        cur = conn.cursor()
        try:
            cur.execute("UPDATE users SET remember_token = NULL WHERE id = %s", (user_id,))
            conn.commit()
        except Exception as e:
            print(f"Logout clear token error: {e}")
        finally:
            cur.close()
            conn.close()

    flash(_t('auth.goodbye', username=username), 'info')
    return response
