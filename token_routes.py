from flask import Blueprint, render_template, request, redirect, url_for, session, flash, jsonify
from database import get_db_connection
from psycopg2.extras import RealDictCursor
from auth import login_required
from datetime import datetime

token_bp = Blueprint('tokens', __name__)

@token_bp.route('/tokens')
@login_required
def tokens_page():
    """Main tokens management page"""
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
    
    cur.close()
    conn.close()
    
    return render_template('tokens.html', 
                         created_tokens=created_tokens,
                         received_tokens=received_tokens)

@token_bp.route('/tokens/create', methods=['GET', 'POST'])
@login_required
def create_token():
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    if request.method == 'POST':
        recipient_id = request.form.get('recipient_id')
        name = request.form.get('name', '').strip()
        description = request.form.get('description', '').strip()
        duration_minutes = request.form.get('duration_minutes', type=int)
        
        # Validation
        if not recipient_id or not name or not duration_minutes:
            flash('All fields are required!', 'error')
        elif duration_minutes < 1:
            flash('Duration must be at least 1 minute!', 'error')
        elif len(name) > 50:
            flash('Token name must be 50 characters or less!', 'error')
        else:
            try:
                # Create token
                cur.execute("""
                    INSERT INTO tokens (creator_id, recipient_id, name, description, duration_minutes)
                    VALUES (%s, %s, %s, %s, %s)
                    RETURNING id
                """, (session['user_id'], recipient_id, name, description, duration_minutes))
                
                token_id = cur.fetchone()['id']
                conn.commit()
                
                # Get recipient username for message
                cur.execute("SELECT username FROM users WHERE id = %s", (recipient_id,))
                recipient = cur.fetchone()
                
                flash(f'Token "{name}" created successfully for {recipient["username"]}!', 'success')
                return redirect(url_for('tokens.tokens_page'))
                
            except Exception as e:
                conn.rollback()
                flash('An error occurred while creating the token.', 'error')
                print(f"Token creation error: {e}")
    
    # Get list of users for recipient dropdown (exclude current user)
    cur.execute("""
        SELECT id, username 
        FROM users 
        WHERE id != %s 
        ORDER BY username
    """, (session['user_id'],))
    users = cur.fetchall()
    
    cur.close()
    conn.close()
    
    return render_template('create_token.html', users=users)

@token_bp.route('/tokens/<int:token_id>/start', methods=['POST'])
@login_required
def start_token(token_id):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    try:
        # Check if user is the recipient
        cur.execute("""
            SELECT recipient_id, status, name
            FROM tokens
            WHERE id = %s
        """, (token_id,))
        token = cur.fetchone()

        if not token:
            flash('Token not found!', 'error')
        elif token['recipient_id'] != session['user_id']:
            flash('You can only start tokens assigned to you!', 'error')
        elif token['status'] != 'available':
            flash('This token is already in use or completed!', 'warning')
        else:
            # Update token status
            cur.execute("""
                UPDATE tokens
                SET status = 'in_progress', used_at = CURRENT_TIMESTAMP
                WHERE id = %s
            """, (token_id,))
            conn.commit()
            flash(f'Token "{token["name"]}" started!', 'success')
            
    except Exception as e:
        conn.rollback()
        flash('An error occurred while starting the token.', 'error')
        print(f"Token start error: {e}")
    finally:
        cur.close()
        conn.close()
    
    return redirect(url_for('tokens.tokens_page'))

@token_bp.route('/tokens/<int:token_id>/complete', methods=['POST'])
@login_required
def complete_token(token_id):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    try:
        # Check if user is the recipient
        cur.execute("""
            SELECT recipient_id, status, name
            FROM tokens
            WHERE id = %s
        """, (token_id,))
        token = cur.fetchone()

        if not token:
            flash('Token not found!', 'error')
        elif token['recipient_id'] != session['user_id']:
            flash('You can only complete tokens assigned to you!', 'error')
        elif token['status'] == 'completed':
            flash('This token is already completed!', 'warning')
        elif token['status'] != 'in_progress':
            flash('You must start the token before completing it!', 'warning')
        else:
            # Update token status
            cur.execute("""
                UPDATE tokens
                SET status = 'completed'
                WHERE id = %s
            """, (token_id,))
            conn.commit()
            flash(f'Token "{token["name"]}" completed! Great job!', 'success')
            
    except Exception as e:
        conn.rollback()
        flash('An error occurred while completing the token.', 'error')
        print(f"Token complete error: {e}")
    finally:
        cur.close()
        conn.close()
    
    return redirect(url_for('tokens.tokens_page'))

@token_bp.route('/tokens/<int:token_id>/cancel', methods=['POST'])
@login_required
def cancel_token(token_id):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    try:
        # Check if user is the creator
        cur.execute("""
            SELECT creator_id, status, name
            FROM tokens
            WHERE id = %s
        """, (token_id,))
        token = cur.fetchone()

        if not token:
            flash('Token not found!', 'error')
        elif token['creator_id'] != session['user_id']:
            flash('You can only cancel tokens you created!', 'error')
        elif token['status'] == 'completed':
            flash('Cannot cancel a completed token!', 'warning')
        else:
            # Delete token
            cur.execute("DELETE FROM tokens WHERE id = %s", (token_id,))
            conn.commit()
            flash(f'Token "{token["name"]}" has been cancelled.', 'info')
            
    except Exception as e:
        conn.rollback()
        flash('An error occurred while cancelling the token.', 'error')
        print(f"Token cancel error: {e}")
    finally:
        cur.close()
        conn.close()
    
    return redirect(url_for('tokens.tokens_page'))
