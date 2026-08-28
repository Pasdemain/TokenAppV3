import os
import threading
import time
import urllib.request
from flask import Flask, render_template, redirect, url_for, session, flash, send_from_directory, request, jsonify
from werkzeug.security import generate_password_hash
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime
from database import get_db_connection, init_db
from auth import auth_bp, login_required, feature_required, admin_required
from token_routes import token_bp
from shopping_routes import shopping_bp
from scratch_routes import scratch_bp
from wheel_routes import wheel_bp
from flashcard_routes import flashcard_bp
from competency_routes import competency_bp
from santa_routes import santa_bp
from molkky_routes import molkky_bp
from streetfood_routes import streetfood_bp
from trip_routes import trip_bp
import i18n as i18n_module
from i18n import t as _t

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
app.register_blueprint(molkky_bp)
app.register_blueprint(streetfood_bp)
app.register_blueprint(trip_bp)

# Initialize i18n (exposes `t(key)` to templates)
i18n_module.init_app(app)


@app.route('/set-lang', methods=['POST'])
def set_lang():
    lang = request.form.get('lang', '').strip()
    if lang in i18n_module.SUPPORTED_LANGS:
        session['lang'] = lang
        if 'user_id' in session:
            conn = get_db_connection()
            cur = conn.cursor()
            try:
                cur.execute("UPDATE users SET lang = %s WHERE id = %s", (lang, session['user_id']))
                conn.commit()
            finally:
                cur.close()
                conn.close()
    next_url = request.form.get('next') or request.referrer or url_for('dashboard')
    return redirect(next_url)


# Initialize database on startup
with app.app_context():
    init_db()


@app.context_processor
def inject_user_features():
    if 'user_id' in session:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        try:
            cur.execute(
                "SELECT feature_name, is_enabled FROM user_features WHERE user_id = %s",
                (session['user_id'],)
            )
            features = {row['feature_name']: row['is_enabled'] for row in cur.fetchall()}
            if not session.get('is_admin'):
                cur.execute("SELECT is_admin FROM users WHERE id = %s", (session['user_id'],))
                row = cur.fetchone()
                if row and row['is_admin']:
                    session['is_admin'] = True
            cur.execute("""
                SELECT COUNT(*) as cnt FROM scratch_prize_proposals
                WHERE reviewer_id = %s AND status = 'pending'
            """, (session['user_id'],))
            pending_proposals = cur.fetchone()['cnt']
            # Pending street-food orders for cantines this user owns or co-manages
            try:
                cur.execute("""
                    SELECT COUNT(*) as cnt FROM streetfood_orders o
                    WHERE o.payment_status = 'pending'
                      AND o.cantine_id IN (
                        SELECT id FROM streetfood_cantines WHERE owner_id = %s
                        UNION
                        SELECT cantine_id FROM streetfood_managers WHERE user_id = %s
                      )
                """, (session['user_id'], session['user_id']))
                pending_streetfood = cur.fetchone()['cnt']
            except Exception:
                pending_streetfood = 0
        finally:
            cur.close()
            conn.close()
        app_name = session.get('app_name')
        app_icon = session.get('app_icon') or '💑'
        return {
            'user_features': features,
            'pending_scratch_proposals': pending_proposals,
            'pending_streetfood_orders': pending_streetfood,
            'app_name': app_name or 'LoveBirds',
            'app_icon': app_icon,
            'show_app_name_popup': app_name is None,
        }
    return {'user_features': {}, 'pending_scratch_proposals': 0,
            'pending_streetfood_orders': 0,
            'app_name': 'LoveBirds', 'app_icon': '💑', 'show_app_name_popup': False}

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
@admin_required
def admin():
    from flask import request

    if request.method == 'POST':
        action = request.form.get('action')

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
            cur.execute("DELETE FROM molkky_throws")
            cur.execute("DELETE FROM molkky_members")
            cur.execute("DELETE FROM molkky_teams")
            cur.execute("DELETE FROM molkky_games")
            cur.execute("DELETE FROM streetfood_order_supplements")
            cur.execute("DELETE FROM streetfood_orders")
            cur.execute("DELETE FROM streetfood_supplements")
            cur.execute("DELETE FROM streetfood_dishes")
            cur.execute("DELETE FROM streetfood_wallets")
            cur.execute("DELETE FROM streetfood_managers")
            cur.execute("DELETE FROM streetfood_cantines")
            cur.execute("DELETE FROM trip_signups")
            cur.execute("DELETE FROM trip_activities")
            cur.execute("DELETE FROM users")
            conn.commit()
            cur.close()
            conn.close()
            flash(_t('admin.flash_all_deleted'), 'success')
            session.clear()
            return redirect(url_for('auth.login'))

        elif action == 'toggle_feature':
            target_user_id = request.form.get('target_user_id', type=int)
            feature_name = request.form.get('feature_name', '').strip()
            is_enabled = request.form.get('is_enabled') == '1'
            if not target_user_id or not feature_name:
                flash(_t('admin.flash_invalid_data'), 'error')
            else:
                conn = get_db_connection()
                cur = conn.cursor()
                try:
                    cur.execute("""
                        INSERT INTO user_features (user_id, feature_name, is_enabled)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (user_id, feature_name)
                        DO UPDATE SET is_enabled = EXCLUDED.is_enabled
                    """, (target_user_id, feature_name, is_enabled))
                    conn.commit()
                    if is_enabled:
                        flash(_t('admin.flash_feature_enabled', feature=feature_name), 'success')
                    else:
                        flash(_t('admin.flash_feature_disabled', feature=feature_name), 'success')
                except Exception as e:
                    conn.rollback()
                    flash(_t('admin.flash_update_error'), 'error')
                    print(f"Toggle feature error: {e}")
                finally:
                    cur.close()
                    conn.close()

        elif action == 'set_feature_default':
            feature_name = request.form.get('feature_name', '').strip()
            is_enabled = request.form.get('is_enabled') == '1'
            if not feature_name:
                flash(_t('admin.flash_invalid_feature'), 'error')
            else:
                conn = get_db_connection()
                cur = conn.cursor()
                try:
                    cur.execute("""
                        INSERT INTO feature_defaults (feature_name, is_enabled)
                        VALUES (%s, %s)
                        ON CONFLICT (feature_name)
                        DO UPDATE SET is_enabled = EXCLUDED.is_enabled
                    """, (feature_name, is_enabled))
                    conn.commit()
                    if is_enabled:
                        flash(_t('admin.flash_default_enabled', feature=feature_name), 'success')
                    else:
                        flash(_t('admin.flash_default_disabled', feature=feature_name), 'success')
                except Exception as e:
                    conn.rollback()
                    flash(_t('admin.flash_update_error'), 'error')
                    print(f"Set feature default error: {e}")
                finally:
                    cur.close()
                    conn.close()

        elif action == 'change_password':
            target_username = request.form.get('target_username', '').strip()
            new_password = request.form.get('new_password', '')
            if not target_username or not new_password:
                flash(_t('admin.flash_pwd_required'), 'error')
            elif len(new_password) < 6:
                flash(_t('admin.flash_pwd_too_short'), 'error')
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
                        flash(_t('admin.flash_user_not_found', username=target_username), 'error')
                    else:
                        conn.commit()
                        flash(_t('admin.flash_password_changed', username=target_username), 'success')
                except Exception as e:
                    conn.rollback()
                    flash(_t('admin.flash_pwd_change_error'), 'error')
                    print(f"Change password error: {e}")
                finally:
                    cur.close()
                    conn.close()

        else:
            flash(_t('admin.flash_invalid_action'), 'error')

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT id, username FROM users ORDER BY username")
    users = cur.fetchall()

    # Feature defaults
    cur.execute("SELECT feature_name, is_enabled FROM feature_defaults ORDER BY feature_name")
    feature_defaults = {row['feature_name']: row['is_enabled'] for row in cur.fetchall()}

    # User features: {user_id: {feature_name: {is_enabled, partner_id}}}
    cur.execute("""
        SELECT uf.user_id, uf.feature_name, uf.is_enabled, uf.partner_id, u2.username as partner_username
        FROM user_features uf
        LEFT JOIN users u2 ON uf.partner_id = u2.id
        ORDER BY uf.user_id, uf.feature_name
    """)
    user_features_rows = cur.fetchall()
    user_features_map = {}
    for row in user_features_rows:
        uid = row['user_id']
        if uid not in user_features_map:
            user_features_map[uid] = {}
        user_features_map[uid][row['feature_name']] = {
            'is_enabled': row['is_enabled'],
            'partner_id': row['partner_id'],
            'partner_username': row['partner_username'],
        }
    cur.execute("""
        SELECT sp.*, u.username
        FROM scratch_prizes sp
        JOIN users u ON sp.user_id = u.id
        ORDER BY u.username, sp.id
    """)
    prizes = cur.fetchall()
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

    from auth import ALL_FEATURES as admin_all_features
    return render_template('admin.html', users=users, prizes=prizes,
                           fc_languages=fc_languages,
                           fc_categories=fc_categories,
                           leitner_intervals=leitner_intervals,
                           fc_reports=fc_reports,
                           cq_stats=cq_stats,
                           user_features_map=user_features_map,
                           all_features=admin_all_features,
                           feature_defaults=feature_defaults)

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
    
    return render_template('profile.html', username=session.get('username'), stats=stats,
                           app_name=session.get('app_name') or 'LoveBirds',
                           app_icon=session.get('app_icon') or '💑',
                           allowed_icons=ALLOWED_APP_ICONS)


ALLOWED_APP_ICONS = [
    '💑', '💕', '🏠', '🌟', '🎯', '🌸', '🦋', '🌈',
    '🎮', '🌙', '⚡', '🎵', '🐱', '🐶', '🦊', '🍀',
    '🔥', '💎', '🎭', '🎪',
]


@app.route('/profile/app-prefs', methods=['POST'])
@login_required
def save_app_prefs():
    app_name = request.form.get('app_name', '').strip()[:50] or 'LoveBirds'
    app_icon = request.form.get('app_icon', '💑')
    if app_icon not in ALLOWED_APP_ICONS:
        app_icon = '💑'
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("UPDATE users SET app_name = %s, app_icon = %s WHERE id = %s",
                    (app_name, app_icon, session['user_id']))
        conn.commit()
        session['app_name'] = app_name
        session['app_icon'] = app_icon
        return jsonify({'ok': True, 'app_name': app_name, 'app_icon': app_icon})
    except Exception as e:
        conn.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500
    finally:
        cur.close()
        conn.close()


_SERVEDAY_NAMES = {
    'fr': {
        'days': ['lundi', 'mardi', 'mercredi', 'jeudi', 'vendredi', 'samedi', 'dimanche'],
        'months': ['janvier', 'février', 'mars', 'avril', 'mai', 'juin', 'juillet',
                   'août', 'septembre', 'octobre', 'novembre', 'décembre'],
    },
    'en': {
        'days': ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'],
        'months': ['January', 'February', 'March', 'April', 'May', 'June', 'July',
                   'August', 'September', 'October', 'November', 'December'],
    },
}


@app.template_filter('serveday')
def serveday(d):
    """Localized human date for a dish serve day, e.g. 'jeudi 11 juin'."""
    if d is None:
        return ''
    lang = i18n_module.current_lang()
    names = _SERVEDAY_NAMES.get(lang, _SERVEDAY_NAMES['en'])
    day_name = names['days'][d.weekday()]
    month_name = names['months'][d.month - 1]
    if lang == 'en':
        return f"{day_name} {month_name} {d.day}"
    return f"{day_name} {d.day} {month_name}"


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
