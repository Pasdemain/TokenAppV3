import os
from flask import Flask, render_template, redirect, url_for, session, flash, send_from_directory
from werkzeug.security import generate_password_hash
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime
from database import get_db_connection, init_db
from auth import auth_bp, login_required
from token_routes import token_bp
from shopping_routes import shopping_bp

app = Flask(__name__, static_url_path='/static', static_folder='static')
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-key-change-in-production')

# Register blueprints
app.register_blueprint(auth_bp)
app.register_blueprint(token_bp)
app.register_blueprint(shopping_bp)

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
    
    cur.close()
    conn.close()
    
    return render_template('dashboard.html', 
                         created_tokens=created_tokens,
                         received_tokens=received_tokens,
                         shopping_lists=shopping_lists,
                         username=session.get('username'))

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
                
                # Clear all data
                cur.execute("DELETE FROM shopping_items")
                cur.execute("DELETE FROM shopping_list_members")
                cur.execute("DELETE FROM shopping_lists")
                cur.execute("DELETE FROM tokens")
                cur.execute("DELETE FROM users")
                
                conn.commit()
                cur.close()
                conn.close()
                
                flash('All data has been cleared successfully!', 'success')
                session.clear()
                return redirect(url_for('auth.login'))
            else:
                flash('Invalid action!', 'error')
        else:
            flash('Invalid admin password!', 'error')
    
    return render_template('admin.html')

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

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
