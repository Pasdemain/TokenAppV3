from flask import Blueprint, render_template, request, redirect, url_for, session, flash, jsonify
from database import get_db_connection
from psycopg2.extras import RealDictCursor
from auth import login_required
from datetime import datetime

shopping_bp = Blueprint('shopping', __name__)

@shopping_bp.route('/shopping')
@login_required
def shopping_lists():
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    # Get all shopping lists user has access to
    cur.execute("""
        SELECT DISTINCT sl.*, u.username as creator_username,
               COUNT(DISTINCT si.id) as item_count,
               SUM(CASE WHEN si.is_completed THEN 1 ELSE 0 END) as completed_count
        FROM shopping_lists sl
        JOIN users u ON sl.created_by = u.id
        LEFT JOIN shopping_items si ON sl.id = si.list_id
        LEFT JOIN shopping_list_members slm ON sl.id = slm.list_id
        WHERE (sl.created_by = %s OR slm.user_id = %s) AND sl.is_active = TRUE
        GROUP BY sl.id, u.username
        ORDER BY sl.created_at DESC
    """, (session['user_id'], session['user_id']))
    
    lists = cur.fetchall()
    
    # Calculate progress for each list
    for lst in lists:
        if lst['item_count'] > 0:
            lst['progress'] = int((lst['completed_count'] or 0) / lst['item_count'] * 100)
        else:
            lst['progress'] = 0
    
    cur.close()
    conn.close()
    
    return render_template('shopping_lists.html', lists=lists)

@shopping_bp.route('/shopping/create', methods=['GET', 'POST'])
@login_required
def create_shopping_list():
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        share_with = request.form.getlist('share_with')
        
        if not name:
            flash('List name is required!', 'error')
            return redirect(url_for('shopping.create_shopping_list'))
        
        if len(name) > 100:
            flash('List name must be 100 characters or less!', 'error')
            return redirect(url_for('shopping.create_shopping_list'))
        
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        try:
            # Create shopping list
            cur.execute("""
                INSERT INTO shopping_lists (name, created_by)
                VALUES (%s, %s)
                RETURNING id
            """, (name, session['user_id']))
            
            list_id = cur.fetchone()['id']
            
            # Add creator as owner
            cur.execute("""
                INSERT INTO shopping_list_members (list_id, user_id, role)
                VALUES (%s, %s, 'owner')
            """, (list_id, session['user_id']))
            
            # Add shared users as members
            for user_id in share_with:
                if user_id and int(user_id) != session['user_id']:
                    cur.execute("""
                        INSERT INTO shopping_list_members (list_id, user_id, role)
                        VALUES (%s, %s, 'member')
                        ON CONFLICT (list_id, user_id) DO NOTHING
                    """, (list_id, int(user_id)))
            
            conn.commit()
            flash(f'Shopping list "{name}" created successfully!', 'success')
            return redirect(url_for('shopping.shopping_list_detail', list_id=list_id))
            
        except Exception as e:
            conn.rollback()
            flash('An error occurred while creating the list.', 'error')
            print(f"Shopping list creation error: {e}")
        finally:
            cur.close()
            conn.close()
    
    # Get users for sharing
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("""
        SELECT id, username 
        FROM users 
        WHERE id != %s 
        ORDER BY username
    """, (session['user_id'],))
    users = cur.fetchall()
    cur.close()
    conn.close()
    
    return render_template('create_shopping_list.html', users=users)

@shopping_bp.route('/shopping/<int:list_id>')
@login_required
def shopping_list_detail(list_id):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    # Check access and get list details
    cur.execute("""
        SELECT sl.*, u.username as creator_username
        FROM shopping_lists sl
        JOIN users u ON sl.created_by = u.id
        LEFT JOIN shopping_list_members slm ON sl.id = slm.list_id
        WHERE sl.id = %s AND sl.is_active = TRUE 
        AND (sl.created_by = %s OR slm.user_id = %s)
    """, (list_id, session['user_id'], session['user_id']))
    
    shopping_list = cur.fetchone()
    
    if not shopping_list:
        flash('Shopping list not found or you do not have access!', 'error')
        return redirect(url_for('shopping.shopping_lists'))
    
    # Get items
    cur.execute("""
        SELECT si.*, u1.username as added_by_username, u2.username as completed_by_username
        FROM shopping_items si
        LEFT JOIN users u1 ON si.added_by = u1.id
        LEFT JOIN users u2 ON si.completed_by = u2.id
        WHERE si.list_id = %s
        ORDER BY si.is_completed, si.added_at DESC
    """, (list_id,))
    items = cur.fetchall()
    
    # Get members
    cur.execute("""
        SELECT u.username, slm.role
        FROM shopping_list_members slm
        JOIN users u ON slm.user_id = u.id
        WHERE slm.list_id = %s
        ORDER BY slm.role DESC, u.username
    """, (list_id,))
    members = cur.fetchall()
    
    # Calculate progress
    total_items = len(items)
    completed_items = sum(1 for item in items if item['is_completed'])
    progress = int((completed_items / total_items * 100) if total_items > 0 else 0)
    
    cur.close()
    conn.close()
    
    return render_template('shopping_list_detail.html', 
                         shopping_list=shopping_list,
                         items=items,
                         members=members,
                         progress=progress,
                         is_owner=(shopping_list['created_by'] == session['user_id']))

@shopping_bp.route('/shopping/<int:list_id>/add_item', methods=['POST'])
@login_required
def add_item(list_id):
    name = request.form.get('name', '').strip()
    quantity = request.form.get('quantity', '1').strip()
    category = request.form.get('category', 'pcs')
    
    if not name:
        flash('Item name is required!', 'error')
        return redirect(url_for('shopping.shopping_list_detail', list_id=list_id))
    
    conn = get_db_connection()
    cur = conn.cursor()
    
    try:
        # Check access
        cur.execute("""
            SELECT 1 FROM shopping_lists sl
            LEFT JOIN shopping_list_members slm ON sl.id = slm.list_id
            WHERE sl.id = %s AND sl.is_active = TRUE 
            AND (sl.created_by = %s OR slm.user_id = %s)
        """, (list_id, session['user_id'], session['user_id']))
        
        if not cur.fetchone():
            flash('You do not have access to this list!', 'error')
            return redirect(url_for('shopping.shopping_lists'))
        
        # Add item
        cur.execute("""
            INSERT INTO shopping_items (list_id, name, quantity, category, added_by)
            VALUES (%s, %s, %s, %s, %s)
        """, (list_id, name, quantity, category, session['user_id']))
        
        conn.commit()
        flash(f'"{name}" added to the list!', 'success')
        
    except Exception as e:
        conn.rollback()
        flash('An error occurred while adding the item.', 'error')
        print(f"Add item error: {e}")
    finally:
        cur.close()
        conn.close()
    
    return redirect(url_for('shopping.shopping_list_detail', list_id=list_id))

@shopping_bp.route('/shopping/toggle_item/<int:item_id>', methods=['POST'])
@login_required
def toggle_item(item_id):
    conn = get_db_connection()
    cur = conn.cursor()
    
    try:
        # Get item and check access
        cur.execute("""
            SELECT si.is_completed, si.list_id, si.name
            FROM shopping_items si
            JOIN shopping_lists sl ON si.list_id = sl.id
            LEFT JOIN shopping_list_members slm ON sl.id = slm.list_id
            WHERE si.id = %s AND sl.is_active = TRUE
            AND (sl.created_by = %s OR slm.user_id = %s)
        """, (item_id, session['user_id'], session['user_id']))
        
        item = cur.fetchone()
        
        if not item:
            flash('Item not found or you do not have access!', 'error')
            return redirect(url_for('shopping.shopping_lists'))
        
        is_completed, list_id, item_name = item
        
        # Toggle completion status
        if is_completed:
            cur.execute("""
                UPDATE shopping_items 
                SET is_completed = FALSE, completed_by = NULL, completed_at = NULL
                WHERE id = %s
            """, (item_id,))
            flash(f'"{item_name}" marked as incomplete.', 'info')
        else:
            cur.execute("""
                UPDATE shopping_items 
                SET is_completed = TRUE, completed_by = %s, completed_at = CURRENT_TIMESTAMP
                WHERE id = %s
            """, (session['user_id'], item_id))
            flash(f'"{item_name}" marked as complete!', 'success')
        
        conn.commit()
        
    except Exception as e:
        conn.rollback()
        flash('An error occurred while updating the item.', 'error')
        print(f"Toggle item error: {e}")
    finally:
        cur.close()
        conn.close()
    
    return redirect(url_for('shopping.shopping_list_detail', list_id=list_id))

@shopping_bp.route('/shopping/<int:list_id>/delete', methods=['POST'])
@login_required
def delete_list(list_id):
    conn = get_db_connection()
    cur = conn.cursor()
    
    try:
        # Check if user is the owner
        cur.execute("""
            SELECT name, created_by 
            FROM shopping_lists 
            WHERE id = %s AND is_active = TRUE
        """, (list_id,))
        
        result = cur.fetchone()
        
        if not result:
            flash('Shopping list not found!', 'error')
        elif result[1] != session['user_id']:  # created_by
            flash('Only the list owner can delete the list!', 'error')
        else:
            # Soft delete the list
            cur.execute("""
                UPDATE shopping_lists 
                SET is_active = FALSE 
                WHERE id = %s
            """, (list_id,))
            conn.commit()
            flash(f'Shopping list "{result[0]}" has been deleted.', 'info')
            return redirect(url_for('shopping.shopping_lists'))
            
    except Exception as e:
        conn.rollback()
        flash('An error occurred while deleting the list.', 'error')
        print(f"Delete list error: {e}")
    finally:
        cur.close()
        conn.close()
    
    return redirect(url_for('shopping.shopping_list_detail', list_id=list_id))
