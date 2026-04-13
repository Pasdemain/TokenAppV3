import os
import threading
import time
import urllib.request
from flask import Flask, render_template, redirect, url_for, session, flash, send_from_directory
from werkzeug.security import generate_password_hash
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime
from database import get_db_connection, init_db
from auth import auth_bp, login_required
from token_routes import token_bp
from shopping_routes import shopping_bp
from scratch_routes import scratch_bp
from wheel_routes import wheel_bp
from flashcard_routes import flashcard_bp
from competency_routes import competency_bp
from santa_routes import santa_bp

app = Flask(__name__, static_url_path='/static', static_folder='static')
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-key-change-in-production')

# Session cookie config — needed for iOS Safari compatibility
app.config['SESSION_COOKIE_SECURE'] = os.environ.get('RENDER_EXTERNAL_URL', '').startswith('https')
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['PERMANENT_SESSION_LIFETIME'] = 30 * 24 * 3600  # 30 days

# Register blueprints
app.register_blueprint(auth_bp)
app.register_blueprint(token_bp)
app.register_blueprint(shopping_bp)
app.register_blueprint(scratch_bp)
app.register_blueprint(wheel_bp)
app.register_blueprint(flashcard_bp)
app.register_blueprint(competency_bp)
app.register_blueprint(santa_bp)

# Initialize database on startup
with app.app_context():
    init_db()

# PWA routes - serve manifest and service worker at root
@app.route('/manifest.json')
def manifest():
    return send_from_directory('static', 'manifest.json', mimetype='application/manifest+json')

@app.route('/sw.js')
def service_worker():
    return send_from_directory('static', 'sw.js', mimetype='application/javascript')

@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return redirect(url_for('auth.login'))

@app.route('/dashboard')
@login_required
def dashboard():
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    # Get tokens created by user
    cur.execute("""
        SELECT t.*, u.username as recipient_username
        FROM tokens t
        JOIN users u ON t.recipient_id = u.id
        WHERE t.creator_id = %s
        ORDER BY t.created_at DESC
    """, (session['user_id'],))
    created_tokens = cur.fetchall()
    
    # Get tokens received by user
    cur.execute("""
        SELECT t.*, u.username as creator_username
        FROM tokens t
        JOIN users u ON t.creator_id = u.id
        WHERE t.recipient_id = %s
        ORDER BY t.created_at DESC
    """, (session['user_id'],))
    received_tokens = cur.fetchall()
    
    # Get user's shopping lists
    cur.execute("""
        SELECT sl.*, COUNT(si.id) as item_count, 
               SUM(CASE WHEN si.is_completed THEN 1 ELSE 0 END) as completed_count
        FROM shopping_lists sl
        LEFT JOIN shopping_items si ON sl.id = si.list_id
        LEFT JOIN shopping_list_members slm ON sl.id = slm.list_id
        WHERE (sl.created_by = %s OR slm.user_id = %s) AND sl.is_active = TRUE
        GROUP BY sl.id
        ORDER BY sl.created_at DESC
        LIMIT 5
    """, (session['user_id'], session['user_id']))
    shopping_lists = cur.fetchall()
    
    # Check if scratch ticket is available today
    from datetime import date as _date
    cur.execute(
        "SELECT id FROM scratch_tickets WHERE user_id = %s AND ticket_date = %s",
        (session['user_id'], _date.today())
    )
    ticket_played_today = cur.fetchone() is not None

    # Check if prizes are configured for this user
    cur.execute("SELECT COUNT(*) as cnt FROM scratch_prizes WHERE user_id = %s", (session['user_id'],))
    has_scratch_prizes = cur.fetchone()['cnt'] > 0

    # Flashcard stats for dashboard
    from datetime import date as _date2
    cur.execute("""
        SELECT COUNT(*) as cnt FROM user_flashcards
        WHERE user_id = %s AND next_review_date <= %s
    """, (session['user_id'], _date2.today()))
    fc_due_count = cur.fetchone()['cnt']

    cur.execute("SELECT COUNT(*) as cnt FROM user_flashcards WHERE user_id = %s",
                (session['user_id'],))
    fc_total = cur.fetchone()['cnt']

    # Latest competency test result
    cur.execute("""
        SELECT final_level, estimated_score, target_lang, completed_at
        FROM competency_tests
        WHERE user_id = %s AND status = 'completed'
        ORDER BY completed_at DESC LIMIT 1
    """, (session['user_id'],))
    last_competency = cur.fetchone()

    # Active Secret Santa groups
    cur.execute("""
        SELECT sg.id, sg.name, sg.status, sg.event_date,
               (SELECT COUNT(*) FROM santa_members WHERE group_id = sg.id) as member_count
        FROM santa_groups sg
        WHERE (sg.creator_id = %s
               OR EXISTS (SELECT 1 FROM santa_members sm
                          WHERE sm.group_id = sg.id AND sm.user_id = %s))
          AND sg.status IN ('open', 'drawn')
        ORDER BY sg.created_at DESC
        LIMIT 3
    """, (session['user_id'], session['user_id']))
    santa_groups = cur.fetchall()

    cur.close()
    conn.close()

    return render_template('dashboard.html',
                           created_tokens=created_tokens,
                           received_tokens=received_tokens,
                           shopping_lists=shopping_lists,
                           username=session.get('username'),
                           ticket_played_today=ticket_played_today,
                           has_scratch_prizes=has_scratch_prizes,
                           fc_due_count=fc_due_count,
                           fc_total=fc_total,
                           last_competency=last_competency,
                           santa_groups=santa_groups)

@app.route('/admin', methods=['GET', 'POST'])
def admin():
    from flask import request

    if request.method == 'POST':
        password = request.form.get('password')
        action = request.form.get('action')

        if password == 'Tom123':
            if action == 'clear_all':
                conn = get_db_connection()
                cur = conn.cursor()

                cur.execute("DELETE FROM competency_answers")
                cur.execute("DELETE FROM competency_tests")
                cur.execute("DELETE FROM competency_questions")
                cur.execute("DELETE FROM flashcard_reports")
                cur.execute("DELETE FROM user_flashcards")
                cur.execute("DELETE FROM flashcard_distractors")
                cur.execute("DELETE FROM flashcards")
                cur.execute("DELETE FROM flashcard_categories")
                cur.execute("DELETE FROM languages")
                cur.execute("DELETE FROM scratch_tickets")
                cur.execute("DELETE FROM scratch_prizes")
                cur.execute("DELETE FROM shopping_items")
                cur.execute("DELETE FROM shopping_list_members")
                cur.execute("DELETE FROM shopping_lists")
                cur.execute("DELETE FROM tokens")
                cur.execute("DELETE FROM santa_members")
                cur.execute("DELETE FROM santa_groups")
                cur.execute("DELETE FROM wheel_countries")
                cur.execute("DELETE FROM users")

                conn.commit()
                cur.close()
                conn.close()

                flash('Toutes les données ont été supprimées !', 'success')
                session.clear()
                return redirect(url_for('auth.login'))

            elif action == 'change_password':
                from flask import request as _req
                target_username = _req.form.get('target_username', '').strip()
                new_password = _req.form.get('new_password', '')
                if not target_username or not new_password:
                    flash('Username and new password are required.', 'error')
                elif len(new_password) < 6:
                    flash('Password must be at least 6 characters.', 'error')
                else:
                    conn = get_db_connection()
                    cur = conn.cursor()
                    try:
                        new_hash = generate_password_hash(new_password)
                        cur.execute(
                            "UPDATE users SET password_hash = %s, remember_token = NULL WHERE username = %s",
                            (new_hash, target_username)
                        )
                        if cur.rowcount == 0:
                            flash(f'User "{target_username}" not found.', 'error')
                        else:
                            conn.commit()
                            flash(f'Password changed for "{target_username}"!', 'success')
                    except Exception as e:
                        conn.rollback()
                        flash('Error changing password.', 'error')
                        print(f"Change password error: {e}")
                    finally:
                        cur.close()
                        conn.close()
            else:
                flash('Action invalide !', 'error')
        else:
            flash('Mot de passe admin incorrect !', 'error')

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT id, username FROM users ORDER BY username")
    users = cur.fetchall()
    cur.execute("""
        SELECT sp.*, u.username
        FROM scratch_prizes sp
        JOIN users u ON sp.user_id = u.id
        ORDER BY u.username, sp.id
    """)
    prizes = cur.fetchall()
    cur.execute("SELECT id, name, flag_emoji, is_active FROM wheel_countries ORDER BY name")
    wheel_countries = cur.fetchall()

    # Flashcard admin data
    cur.execute("SELECT id, code, name, flag_emoji FROM languages ORDER BY name")
    fc_languages = cur.fetchall()
    cur.execute("""
        SELECT fc.id, fc.name, fc.icon, COUNT(f.id) as card_count
        FROM flashcard_categories fc
        LEFT JOIN flashcards f ON f.category_id = fc.id
        GROUP BY fc.id, fc.name, fc.icon ORDER BY fc.name
    """)
    fc_categories = cur.fetchall()
    cur.execute("SELECT box_number, days_interval FROM leitner_intervals ORDER BY box_number")
    leitner_intervals = {r['box_number']: r['days_interval'] for r in cur.fetchall()}
    # Fill defaults if empty
    if not leitner_intervals:
        leitner_intervals = {1:1, 2:2, 3:4, 4:7, 5:14, 6:30, 7:90}

    # Flashcard reports
    cur.execute("""
        SELECT fr.id, fr.comment, fr.source_lang, fr.target_lang, fr.created_at,
               u.username, f.translations
        FROM flashcard_reports fr
        JOIN users u ON fr.user_id = u.id
        JOIN flashcards f ON fr.flashcard_id = f.id
        ORDER BY fr.created_at DESC
        LIMIT 50
    """)
    fc_reports = cur.fetchall()

    # Competency question stats
    cur.execute("""
        SELECT skill, level_hint, COUNT(*) as cnt
        FROM competency_questions
        GROUP BY skill, level_hint ORDER BY skill, level_hint
    """)
    cq_stats = cur.fetchall()

    cur.close()
    conn.close()

    return render_template('admin.html', users=users, prizes=prizes,
                           wheel_countries=wheel_countries,
                           fc_languages=fc_languages,
                           fc_categories=fc_categories,
                           leitner_intervals=leitner_intervals,
                           fc_reports=fc_reports,
                           cq_stats=cq_stats)

@app.route('/profile')
@login_required
def profile():
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    # Get user stats
    cur.execute("""
        SELECT 
            (SELECT COUNT(*) FROM tokens WHERE creator_id = %s) as tokens_created,
            (SELECT COUNT(*) FROM tokens WHERE recipient_id = %s) as tokens_received,
            (SELECT COUNT(*) FROM tokens WHERE recipient_id = %s AND status = 'completed') as tokens_completed,
            (SELECT COUNT(*) FROM shopping_lists WHERE created_by = %s) as lists_created
    """, (session['user_id'], session['user_id'], session['user_id'], session['user_id']))
    stats = cur.fetchone()
    
    cur.close()
    conn.close()
    
    return render_template('profile.html', username=session.get('username'), stats=stats)

@app.template_filter('timeago')
def timeago(timestamp):
    if timestamp is None:
        return 'Never'
    now = datetime.utcnow()
    diff = now - timestamp
    
    if diff.days > 7:
        return timestamp.strftime('%b %d, %Y')
    elif diff.days > 0:
        return f"{diff.days} day{'s' if diff.days > 1 else ''} ago"
    elif diff.seconds > 3600:
        hours = diff.seconds // 3600
        return f"{hours} hour{'s' if hours > 1 else ''} ago"
    elif diff.seconds > 60:
        minutes = diff.seconds // 60
        return f"{minutes} minute{'s' if minutes > 1 else ''} ago"
    else:
        return "Just now"

# Keep-alive: ping the app every 14 minutes to prevent Render from sleeping
def keep_alive():
    app_url = os.environ.get('RENDER_EXTERNAL_URL')
    if not app_url:
        return
    while True:
        time.sleep(840)  # 14 minutes
        try:
            urllib.request.urlopen(app_url)
        except Exception:
            pass

threading.Thread(target=keep_alive, daemon=True).start()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
