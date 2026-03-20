import secrets
from flask import Blueprint, render_template, request, redirect, url_for, session, flash, make_response
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
from database import get_db_connection
from psycopg2.extras import RealDictCursor

auth_bp = Blueprint('auth', __name__)

REMEMBER_COOKIE = 'remember_token'
REMEMBER_DAYS = 30


def login_required(f):
    """Decorator to require login for routes"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            # Try remember_me cookie
            token = request.cookies.get(REMEMBER_COOKIE)
            if token:
                conn = get_db_connection()
                cur = conn.cursor(cursor_factory=RealDictCursor)
                try:
                    cur.execute(
                        "SELECT id, username FROM users WHERE remember_token = %s",
                        (token,)
                    )
                    user = cur.fetchone()
                    if user:
                        session['user_id'] = user['id']
                        session['username'] = user['username']
                        return f(*args, **kwargs)
                except Exception as e:
                    print(f"Remember me error: {e}")
                finally:
                    cur.close()
                    conn.close()
            flash('Please log in to access this page.', 'warning')
            return redirect(url_for('auth.login'))
        return f(*args, **kwargs)
    return decorated_function


@auth_bp.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        confirm_password = request.form.get('confirm_password', '')

        if not username or not password:
            flash('Username and password are required!', 'error')
            return render_template('register.html')

        if len(username) > 20:
            flash('Username must be 20 characters or less!', 'error')
            return render_template('register.html')

        if password != confirm_password:
            flash('Passwords do not match!', 'error')
            return render_template('register.html')

        if len(password) < 6:
            flash('Password must be at least 6 characters!', 'error')
            return render_template('register.html')

        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        try:
            cur.execute("SELECT id FROM users WHERE username = %s", (username,))
            if cur.fetchone():
                flash('Username already exists!', 'error')
                return render_template('register.html')

            password_hash = generate_password_hash(password)
            cur.execute(
                "INSERT INTO users (username, password_hash) VALUES (%s, %s) RETURNING id",
                (username, password_hash)
            )
            user_id = cur.fetchone()['id']
            conn.commit()

            session['user_id'] = user_id
            session['username'] = username

            flash('Registration successful! Welcome to CoupleApp!', 'success')
            return redirect(url_for('dashboard'))

        except Exception as e:
            conn.rollback()
            flash('An error occurred during registration. Please try again.', 'error')
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
            flash('Username and password are required!', 'error')
            return render_template('login.html')

        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        try:
            cur.execute("SELECT id, username, password_hash FROM users WHERE username = %s", (username,))
            user = cur.fetchone()

            if user and check_password_hash(user['password_hash'], password):
                session['user_id'] = user['id']
                session['username'] = user['username']

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
                        httponly=True, samesite='Lax'
                    )

                flash(f'Welcome back, {username}!', 'success')
                return response
            else:
                flash('Invalid username or password!', 'error')

        except Exception as e:
            flash('An error occurred during login. Please try again.', 'error')
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

    flash(f'Goodbye, {username}! You have been logged out.', 'info')
    return response
