from flask import Blueprint, render_template, request, redirect, url_for, session, flash, jsonify
from database import get_db_connection
from psycopg2.extras import RealDictCursor
from auth import feature_required
from datetime import datetime
from i18n import t as _t

token_bp = Blueprint('tokens', __name__)


def _auto_complete_expired_tokens(conn, cur):
    cur.execute("""
        UPDATE tokens
        SET status = 'completed'
        WHERE status = 'in_progress'
          AND used_at + (duration_minutes * interval '1 minute') <= NOW()
    """)
    conn.commit()


@token_bp.route('/tokens')
@feature_required('tokens')
def tokens_page():
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    _auto_complete_expired_tokens(conn, cur)

    cur.execute("""
        SELECT t.*, u.username as recipient_username
        FROM tokens t
        JOIN users u ON t.recipient_id = u.id
        WHERE t.creator_id = %s
        ORDER BY t.created_at DESC
    """, (session['user_id'],))
    created_tokens = cur.fetchall()

    cur.execute("""
        SELECT t.*, u.username as creator_username
        FROM tokens t
        JOIN users u ON t.creator_id = u.id
        WHERE t.recipient_id = %s
        ORDER BY t.created_at DESC
    """, (session['user_id'],))
    all_received = cur.fetchall()

    cur.close()
    conn.close()

    active_received = [t for t in all_received if t['status'] != 'completed']
    completed_received = [t for t in all_received if t['status'] == 'completed']

    active_created = [t for t in created_tokens if t['status'] != 'completed']
    completed_created = [t for t in created_tokens if t['status'] == 'completed']

    return render_template('tokens.html',
                           active_created=active_created,
                           completed_created=completed_created,
                           active_received=active_received,
                           completed_received=completed_received)

@token_bp.route('/tokens/create', methods=['GET', 'POST'])
@feature_required('tokens')
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
            flash(_t('tokens.flash_fields_required'), 'error')
        elif duration_minutes < 1:
            flash(_t('tokens.flash_duration_min'), 'error')
        elif len(name) > 50:
            flash(_t('tokens.flash_name_too_long'), 'error')
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
                
                flash(_t('tokens.flash_created', name=name, recipient=recipient["username"]), 'success')
                return redirect(url_for('tokens.tokens_page'))

            except Exception as e:
                conn.rollback()
                flash(_t('tokens.flash_error_create'), 'error')
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
@feature_required('tokens')
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
            flash(_t('tokens.flash_not_found'), 'error')
        elif token['recipient_id'] != session['user_id']:
            flash(_t('tokens.flash_only_recipient_start'), 'error')
        elif token['status'] != 'available':
            flash(_t('tokens.flash_already_in_use'), 'warning')
        else:
            # Update token status
            cur.execute("""
                UPDATE tokens
                SET status = 'in_progress', used_at = CURRENT_TIMESTAMP
                WHERE id = %s
            """, (token_id,))
            conn.commit()
            flash(_t('tokens.flash_started', name=token["name"]), 'success')

    except Exception as e:
        conn.rollback()
        flash(_t('tokens.flash_error_start'), 'error')
        print(f"Token start error: {e}")
    finally:
        cur.close()
        conn.close()
    
    return redirect(url_for('tokens.tokens_page'))

@token_bp.route('/tokens/<int:token_id>/complete', methods=['POST'])
@feature_required('tokens')
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
            flash(_t('tokens.flash_not_found'), 'error')
        elif token['recipient_id'] != session['user_id']:
            flash(_t('tokens.flash_only_recipient_complete'), 'error')
        elif token['status'] == 'completed':
            flash(_t('tokens.flash_already_completed'), 'warning')
        elif token['status'] != 'in_progress':
            flash(_t('tokens.flash_must_start_first'), 'warning')
        else:
            # Update token status
            cur.execute("""
                UPDATE tokens
                SET status = 'completed'
                WHERE id = %s
            """, (token_id,))
            conn.commit()
            flash(_t('tokens.flash_completed', name=token["name"]), 'success')

    except Exception as e:
        conn.rollback()
        flash(_t('tokens.flash_error_complete'), 'error')
        print(f"Token complete error: {e}")
    finally:
        cur.close()
        conn.close()
    
    return redirect(url_for('tokens.tokens_page'))

@token_bp.route('/tokens/<int:token_id>/cancel', methods=['POST'])
@feature_required('tokens')
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
            flash(_t('tokens.flash_not_found'), 'error')
        elif token['creator_id'] != session['user_id']:
            flash(_t('tokens.flash_only_creator_cancel'), 'error')
        elif token['status'] == 'completed':
            flash(_t('tokens.flash_cannot_cancel_completed'), 'warning')
        else:
            # Delete token
            cur.execute("DELETE FROM tokens WHERE id = %s", (token_id,))
            conn.commit()
            flash(_t('tokens.flash_cancelled', name=token["name"]), 'info')

    except Exception as e:
        conn.rollback()
        flash(_t('tokens.flash_error_cancel'), 'error')
        print(f"Token cancel error: {e}")
    finally:
        cur.close()
        conn.close()
    
    return redirect(url_for('tokens.tokens_page'))
