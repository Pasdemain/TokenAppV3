from datetime import datetime
from decimal import Decimal, InvalidOperation
from flask import Blueprint, render_template, request, redirect, url_for, session, flash
from psycopg2.extras import RealDictCursor
from database import get_db_connection
from auth import feature_required
from i18n import t as _t

streetfood_bp = Blueprint('streetfood', __name__)

CURRENCIES = {'EUR': '€', 'CHF': 'CHF'}
DEFAULT_LOGOS = ['cat', '🍜', '🌮', '🍕', '🍔', '🥘', '🍱', '🥡', '🌯', '🍗', '🥗', '🍛']


def _money(val):
    """Coerce a user-supplied amount to a 2-decimal Decimal (>= 0 by default)."""
    try:
        d = Decimal(str(val).replace(',', '.'))
    except (InvalidOperation, AttributeError, TypeError):
        return Decimal('0.00')
    return d.quantize(Decimal('0.01'))


def _owned_cantine(cur, user_id):
    cur.execute("SELECT * FROM streetfood_cantines WHERE owner_id = %s", (user_id,))
    return cur.fetchone()


def _managed_cantine(cur, user_id):
    """Return the cantine this user can manage (owns OR is a manager of), or None."""
    cantine = _owned_cantine(cur, user_id)
    if cantine:
        return cantine
    cur.execute("""
        SELECT c.* FROM streetfood_cantines c
        JOIN streetfood_managers m ON m.cantine_id = c.id
        WHERE m.user_id = %s
        LIMIT 1
    """, (user_id,))
    return cur.fetchone()


def _can_manage(cur, cantine, user_id):
    if not cantine:
        return False
    if cantine['owner_id'] == user_id:
        return True
    cur.execute(
        "SELECT 1 FROM streetfood_managers WHERE cantine_id = %s AND user_id = %s",
        (cantine['id'], user_id)
    )
    return cur.fetchone() is not None


def _get_wallet(cur, cantine_id, user_id):
    """Return current balance (Decimal), creating the wallet row if missing."""
    cur.execute(
        "SELECT balance FROM streetfood_wallets WHERE cantine_id = %s AND user_id = %s",
        (cantine_id, user_id)
    )
    row = cur.fetchone()
    if row is None:
        cur.execute(
            "INSERT INTO streetfood_wallets (cantine_id, user_id, balance) VALUES (%s, %s, 0)",
            (cantine_id, user_id)
        )
        return Decimal('0.00')
    return Decimal(row['balance'])


def _dish_with_extras(cur, cantine_id):
    """Active dishes of a cantine with supplements and ordered-quantity counts."""
    cur.execute("""
        SELECT d.*,
               COALESCE((SELECT SUM(quantity) FROM streetfood_orders o WHERE o.dish_id = d.id), 0) AS ordered_qty
        FROM streetfood_dishes d
        WHERE d.cantine_id = %s AND d.status = 'active'
        ORDER BY d.created_at DESC
    """, (cantine_id,))
    dishes = cur.fetchall()
    for d in dishes:
        cur.execute(
            "SELECT * FROM streetfood_supplements WHERE dish_id = %s ORDER BY id",
            (d['id'],)
        )
        d['supplements'] = cur.fetchall()
        d['slots_left'] = (d['max_orders'] - d['ordered_qty']) if d['max_orders'] is not None else None
        d['deadline_passed'] = bool(d['order_deadline'] and d['order_deadline'] < datetime.now())
        d['serve_passed'] = bool(d['serve_date'] and d['serve_date'] < datetime.now().date())
    return dishes


# ── Consumer side ─────────────────────────────────────────────────────────────

@streetfood_bp.route('/streetfood')
@feature_required('streetfood')
def home():
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    cur.execute("""
        SELECT c.*, u.username AS owner_username,
               (SELECT COUNT(*) FROM streetfood_dishes d
                WHERE d.cantine_id = c.id AND d.status = 'active') AS active_dishes
        FROM streetfood_cantines c
        JOIN users u ON c.owner_id = u.id
        WHERE c.is_open = TRUE
        ORDER BY c.created_at DESC
    """)
    cantines = cur.fetchall()

    my_cantine = _owned_cantine(cur, session['user_id'])
    managed = _managed_cantine(cur, session['user_id'])

    cur.close()
    conn.close()
    return render_template('streetfood_home.html', cantines=cantines,
                           my_cantine=my_cantine, managed=managed,
                           currencies=CURRENCIES)


@streetfood_bp.route('/streetfood/c/<int:cantine_id>')
@feature_required('streetfood')
def cantine(cantine_id):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    cur.execute("""
        SELECT c.*, u.username AS owner_username
        FROM streetfood_cantines c JOIN users u ON c.owner_id = u.id
        WHERE c.id = %s
    """, (cantine_id,))
    cantine = cur.fetchone()
    if not cantine:
        cur.close()
        conn.close()
        flash(_t('streetfood.flash_cantine_not_found'), 'error')
        return redirect(url_for('streetfood.home'))

    dishes = _dish_with_extras(cur, cantine_id)
    balance = _get_wallet(cur, cantine_id, session['user_id'])
    conn.commit()

    # Orders the current user already placed here (recent)
    cur.execute("""
        SELECT o.*, d.name AS dish_name
        FROM streetfood_orders o JOIN streetfood_dishes d ON o.dish_id = d.id
        WHERE o.cantine_id = %s AND o.user_id = %s
        ORDER BY o.created_at DESC LIMIT 10
    """, (cantine_id, session['user_id']))
    my_orders = cur.fetchall()
    for o in my_orders:
        cur.execute("SELECT name, price FROM streetfood_order_supplements WHERE order_id = %s", (o['id'],))
        o['supplements'] = cur.fetchall()

    can_manage = _can_manage(cur, cantine, session['user_id'])

    cur.close()
    conn.close()
    return render_template('streetfood_cantine.html', cantine=cantine, dishes=dishes,
                           balance=balance, my_orders=my_orders, can_manage=can_manage,
                           currency_symbol=CURRENCIES.get(cantine['currency'], cantine['currency']))


@streetfood_bp.route('/streetfood/c/<int:cantine_id>/order', methods=['POST'])
@feature_required('streetfood')
def place_order(cantine_id):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("SELECT * FROM streetfood_cantines WHERE id = %s", (cantine_id,))
        cantine = cur.fetchone()
        if not cantine or not cantine['is_open']:
            flash(_t('streetfood.flash_cantine_closed'), 'error')
            return redirect(url_for('streetfood.home'))

        dish_id = request.form.get('dish_id', type=int)
        cur.execute(
            "SELECT * FROM streetfood_dishes WHERE id = %s AND cantine_id = %s",
            (dish_id, cantine_id)
        )
        dish = cur.fetchone()
        if not dish or dish['status'] != 'active':
            flash(_t('streetfood.flash_dish_unavailable'), 'error')
            return redirect(url_for('streetfood.cantine', cantine_id=cantine_id))

        if dish['order_deadline'] and dish['order_deadline'] < datetime.now():
            flash(_t('streetfood.flash_deadline_passed'), 'error')
            return redirect(url_for('streetfood.cantine', cantine_id=cantine_id))

        if dish['serve_date'] and dish['serve_date'] < datetime.now().date():
            flash(_t('streetfood.flash_deadline_passed'), 'error')
            return redirect(url_for('streetfood.cantine', cantine_id=cantine_id))

        quantity = max(1, request.form.get('quantity', 1, type=int))

        # Capacity check
        if dish['max_orders'] is not None:
            cur.execute(
                "SELECT COALESCE(SUM(quantity), 0) AS q FROM streetfood_orders WHERE dish_id = %s",
                (dish_id,)
            )
            already = cur.fetchone()['q']
            if already + quantity > dish['max_orders']:
                flash(_t('streetfood.flash_sold_out'), 'error')
                return redirect(url_for('streetfood.cantine', cantine_id=cantine_id))

        # Resolve chosen supplements (validate they belong to this dish)
        supp_ids = request.form.getlist('supplement_ids', type=int)
        chosen = []
        supplements_total = Decimal('0.00')
        if supp_ids:
            cur.execute(
                "SELECT * FROM streetfood_supplements WHERE dish_id = %s AND id = ANY(%s)",
                (dish_id, supp_ids)
            )
            for s in cur.fetchall():
                chosen.append(s)
                supplements_total += Decimal(s['price'])

        base_price = Decimal(dish['price'])
        unit = base_price + supplements_total
        total = (unit * quantity).quantize(Decimal('0.01'))
        note = request.form.get('note', '').strip()[:500] or None

        # Wallet logic: pay from credit if available, else go negative & await validation
        balance = _get_wallet(cur, cantine_id, session['user_id'])
        if balance >= total:
            payment_status = 'paid'
            paid_at = datetime.utcnow()
        else:
            payment_status = 'pending'
            paid_at = None
        cur.execute(
            "UPDATE streetfood_wallets SET balance = balance - %s WHERE cantine_id = %s AND user_id = %s",
            (total, cantine_id, session['user_id'])
        )

        cur.execute("""
            INSERT INTO streetfood_orders
                (dish_id, cantine_id, user_id, quantity, base_price, supplements_total,
                 total, note, payment_status, paid_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (dish_id, cantine_id, session['user_id'], quantity, base_price,
              supplements_total, total, note, payment_status, paid_at))
        order_id = cur.fetchone()['id']

        for s in chosen:
            cur.execute("""
                INSERT INTO streetfood_order_supplements (order_id, supplement_id, name, price)
                VALUES (%s, %s, %s, %s)
            """, (order_id, s['id'], s['name'], s['price']))

        conn.commit()
        if payment_status == 'paid':
            flash(_t('streetfood.flash_order_paid'), 'success')
        else:
            flash(_t('streetfood.flash_order_pending'), 'success')
    except Exception as e:
        conn.rollback()
        flash(_t('streetfood.flash_order_error'), 'error')
        print(f"Street food order error: {e}")
        import traceback; traceback.print_exc()
    finally:
        cur.close()
        conn.close()
    return redirect(url_for('streetfood.cantine', cantine_id=cantine_id))


# ── Owner / manager side ──────────────────────────────────────────────────────

@streetfood_bp.route('/streetfood/manage')
@feature_required('streetfood')
def manage():
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    cantine = _owned_cantine(cur, session['user_id'])
    dishes = []
    managers = []
    other_users = []
    dish_templates = []
    if cantine:
        dishes = _dish_with_extras(cur, cantine['id'])
        # also show closed dishes for history/reactivation
        cur.execute("""
            SELECT d.*,
                   COALESCE((SELECT SUM(quantity) FROM streetfood_orders o WHERE o.dish_id = d.id), 0) AS ordered_qty
            FROM streetfood_dishes d
            WHERE d.cantine_id = %s AND d.status = 'closed'
            ORDER BY d.created_at DESC LIMIT 10
        """, (cantine['id'],))
        closed_dishes = cur.fetchall()

        cur.execute("""
            SELECT m.user_id, u.username FROM streetfood_managers m
            JOIN users u ON m.user_id = u.id WHERE m.cantine_id = %s ORDER BY u.username
        """, (cantine['id'],))
        managers = cur.fetchall()

        # Library of previously created dishes (deduped by name, latest version),
        # used to pre-fill the new-dish form as a reusable template.
        cur.execute("""
            SELECT DISTINCT ON (name) id, name, description, ingredients, spice_level, price
            FROM streetfood_dishes
            WHERE cantine_id = %s
            ORDER BY name, created_at DESC
        """, (cantine['id'],))
        for row in cur.fetchall():
            cur.execute(
                "SELECT name, price FROM streetfood_supplements WHERE dish_id = %s ORDER BY id",
                (row['id'],)
            )
            supps = [{'name': s['name'], 'price': float(s['price'])} for s in cur.fetchall()]
            dish_templates.append({
                'name': row['name'],
                'description': row['description'] or '',
                'ingredients': row['ingredients'] or '',
                'spice_level': row['spice_level'] or 0,
                'price': float(row['price']),
                'supplements': supps,
            })
        dish_templates.sort(key=lambda t: t['name'].lower())
    else:
        closed_dishes = []

    cur.execute("SELECT id, username FROM users WHERE id != %s ORDER BY username", (session['user_id'],))
    other_users = cur.fetchall()

    cur.close()
    conn.close()
    return render_template('streetfood_manage.html', cantine=cantine, dishes=dishes,
                           closed_dishes=closed_dishes, managers=managers,
                           other_users=other_users, currencies=CURRENCIES,
                           default_logos=DEFAULT_LOGOS,
                           dish_templates=dish_templates,
                           currency_symbol=CURRENCIES.get(cantine['currency'], '') if cantine else '')


@streetfood_bp.route('/streetfood/manage/cantine', methods=['POST'])
@feature_required('streetfood')
def save_cantine():
    name = request.form.get('name', '').strip()[:100]
    logo = request.form.get('logo', 'cat').strip()[:20] or 'cat'
    color = request.form.get('color', '#6366f1').strip()[:20]
    currency = request.form.get('currency', 'EUR')
    if currency not in CURRENCIES:
        currency = 'EUR'
    is_open = request.form.get('is_open') == 'on'

    if not name:
        flash(_t('streetfood.flash_name_required'), 'error')
        return redirect(url_for('streetfood.manage'))

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO streetfood_cantines (owner_id, name, logo, color, currency, is_open)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (owner_id) DO UPDATE
            SET name = EXCLUDED.name, logo = EXCLUDED.logo, color = EXCLUDED.color,
                currency = EXCLUDED.currency, is_open = EXCLUDED.is_open
        """, (session['user_id'], name, logo, color, currency, is_open))
        conn.commit()
        flash(_t('streetfood.flash_cantine_saved'), 'success')
    except Exception as e:
        conn.rollback()
        flash(_t('streetfood.flash_cantine_error'), 'error')
        print(f"Save cantine error: {e}")
    finally:
        cur.close()
        conn.close()
    return redirect(url_for('streetfood.manage'))


@streetfood_bp.route('/streetfood/manage/dish', methods=['POST'])
@streetfood_bp.route('/streetfood/manage/dish/<int:dish_id>', methods=['POST'])
@feature_required('streetfood')
def save_dish(dish_id=None):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cantine = _owned_cantine(cur, session['user_id'])
        if not cantine:
            flash(_t('streetfood.flash_create_cantine_first'), 'error')
            return redirect(url_for('streetfood.manage'))

        name = request.form.get('name', '').strip()[:100]
        if not name:
            flash(_t('streetfood.flash_dish_name_required'), 'error')
            return redirect(url_for('streetfood.manage'))

        description = request.form.get('description', '').strip() or None
        ingredients = request.form.get('ingredients', '').strip() or None
        spice_level = min(4, max(0, request.form.get('spice_level', 0, type=int)))
        price = _money(request.form.get('price', '0'))
        max_orders = request.form.get('max_orders', type=int)
        if max_orders is not None and max_orders <= 0:
            max_orders = None

        serve_raw = request.form.get('serve_date', '').strip()
        serve_date = None
        if serve_raw:
            try:
                serve_date = datetime.fromisoformat(serve_raw).date()
            except ValueError:
                serve_date = None

        deadline_raw = request.form.get('order_deadline', '').strip()
        order_deadline = None
        if deadline_raw:
            try:
                order_deadline = datetime.fromisoformat(deadline_raw)
            except ValueError:
                order_deadline = None

        # The order deadline must leave time to cook: it cannot fall after the serve day.
        if order_deadline and serve_date and order_deadline.date() > serve_date:
            flash(_t('streetfood.flash_deadline_after_serve'), 'error')
            return redirect(url_for('streetfood.manage'))

        if dish_id:
            cur.execute(
                "SELECT id FROM streetfood_dishes WHERE id = %s AND cantine_id = %s",
                (dish_id, cantine['id'])
            )
            if not cur.fetchone():
                flash(_t('streetfood.flash_dish_unavailable'), 'error')
                return redirect(url_for('streetfood.manage'))
            cur.execute("""
                UPDATE streetfood_dishes
                SET name=%s, description=%s, ingredients=%s, spice_level=%s,
                    price=%s, max_orders=%s, order_deadline=%s, serve_date=%s
                WHERE id=%s
            """, (name, description, ingredients, spice_level, price,
                  max_orders, order_deadline, serve_date, dish_id))
        else:
            cur.execute("""
                INSERT INTO streetfood_dishes
                    (cantine_id, name, description, ingredients, spice_level,
                     price, max_orders, order_deadline, serve_date)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
            """, (cantine['id'], name, description, ingredients, spice_level,
                  price, max_orders, order_deadline, serve_date))
            dish_id = cur.fetchone()['id']

        # Replace supplements with the submitted set
        cur.execute("DELETE FROM streetfood_supplements WHERE dish_id = %s", (dish_id,))
        supp_names = request.form.getlist('supp_name')
        supp_prices = request.form.getlist('supp_price')
        for i, sname in enumerate(supp_names):
            sname = sname.strip()[:100]
            if not sname:
                continue
            sprice = _money(supp_prices[i]) if i < len(supp_prices) else Decimal('0.00')
            cur.execute(
                "INSERT INTO streetfood_supplements (dish_id, name, price) VALUES (%s, %s, %s)",
                (dish_id, sname, sprice)
            )

        conn.commit()
        flash(_t('streetfood.flash_dish_saved'), 'success')
    except Exception as e:
        conn.rollback()
        flash(_t('streetfood.flash_dish_error'), 'error')
        print(f"Save dish error: {e}")
        import traceback; traceback.print_exc()
    finally:
        cur.close()
        conn.close()
    return redirect(url_for('streetfood.manage'))


@streetfood_bp.route('/streetfood/manage/dish/<int:dish_id>/status', methods=['POST'])
@feature_required('streetfood')
def toggle_dish(dish_id):
    new_status = request.form.get('status')
    if new_status not in ('active', 'closed'):
        new_status = 'closed'
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cantine = _owned_cantine(cur, session['user_id'])
        if cantine:
            cur.execute(
                "UPDATE streetfood_dishes SET status = %s WHERE id = %s AND cantine_id = %s",
                (new_status, dish_id, cantine['id'])
            )
            conn.commit()
            flash(_t('streetfood.flash_dish_saved'), 'success')
    finally:
        cur.close()
        conn.close()
    return redirect(url_for('streetfood.manage'))


@streetfood_bp.route('/streetfood/manage/dish/<int:dish_id>/delete', methods=['POST'])
@feature_required('streetfood')
def delete_dish(dish_id):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cantine = _owned_cantine(cur, session['user_id'])
        if cantine:
            cur.execute(
                "DELETE FROM streetfood_dishes WHERE id = %s AND cantine_id = %s",
                (dish_id, cantine['id'])
            )
            conn.commit()
            flash(_t('streetfood.flash_dish_deleted'), 'success')
    finally:
        cur.close()
        conn.close()
    return redirect(url_for('streetfood.manage'))


@streetfood_bp.route('/streetfood/manage/managers/add', methods=['POST'])
@feature_required('streetfood')
def add_manager():
    target_id = request.form.get('user_id', type=int)
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cantine = _owned_cantine(cur, session['user_id'])
        if cantine and target_id and target_id != session['user_id']:
            cur.execute("""
                INSERT INTO streetfood_managers (cantine_id, user_id) VALUES (%s, %s)
                ON CONFLICT (cantine_id, user_id) DO NOTHING
            """, (cantine['id'], target_id))
            conn.commit()
            flash(_t('streetfood.flash_manager_added'), 'success')
    finally:
        cur.close()
        conn.close()
    return redirect(url_for('streetfood.manage'))


@streetfood_bp.route('/streetfood/manage/managers/<int:user_id>/remove', methods=['POST'])
@feature_required('streetfood')
def remove_manager(user_id):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cantine = _owned_cantine(cur, session['user_id'])
        if cantine:
            cur.execute(
                "DELETE FROM streetfood_managers WHERE cantine_id = %s AND user_id = %s",
                (cantine['id'], user_id)
            )
            conn.commit()
            flash(_t('streetfood.flash_manager_removed'), 'success')
    finally:
        cur.close()
        conn.close()
    return redirect(url_for('streetfood.manage'))


@streetfood_bp.route('/streetfood/orders')
@feature_required('streetfood')
def orders():
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    cantine = _managed_cantine(cur, session['user_id'])
    if not cantine:
        cur.close()
        conn.close()
        flash(_t('streetfood.flash_no_cantine_access'), 'error')
        return redirect(url_for('streetfood.home'))

    cur.execute("""
        SELECT o.*, d.name AS dish_name, u.username
        FROM streetfood_orders o
        JOIN streetfood_dishes d ON o.dish_id = d.id
        JOIN users u ON o.user_id = u.id
        WHERE o.cantine_id = %s
        ORDER BY o.created_at DESC LIMIT 200
    """, (cantine['id'],))
    all_orders = cur.fetchall()
    for o in all_orders:
        cur.execute("SELECT name, price FROM streetfood_order_supplements WHERE order_id = %s", (o['id'],))
        o['supplements'] = cur.fetchall()

    # Totals
    cur.execute("""
        SELECT
            COALESCE(SUM(CASE WHEN payment_status = 'paid' THEN total ELSE 0 END), 0) AS received,
            COALESCE(SUM(CASE WHEN payment_status = 'pending' THEN total ELSE 0 END), 0) AS pending
        FROM streetfood_orders WHERE cantine_id = %s
    """, (cantine['id'],))
    totals = cur.fetchone()

    # Wallets / balances
    cur.execute("""
        SELECT w.balance, u.username, u.id AS user_id
        FROM streetfood_wallets w JOIN users u ON w.user_id = u.id
        WHERE w.cantine_id = %s AND w.balance <> 0
        ORDER BY w.balance ASC
    """, (cantine['id'],))
    wallets = cur.fetchall()

    cur.execute("SELECT id, username FROM users WHERE id != %s ORDER BY username", (cantine['owner_id'],))
    other_users = cur.fetchall()

    is_owner = cantine['owner_id'] == session['user_id']

    cur.close()
    conn.close()
    return render_template('streetfood_orders.html', cantine=cantine, all_orders=all_orders,
                           totals=totals, wallets=wallets, other_users=other_users,
                           is_owner=is_owner,
                           currency_symbol=CURRENCIES.get(cantine['currency'], cantine['currency']))


@streetfood_bp.route('/streetfood/orders/<int:order_id>/validate', methods=['POST'])
@feature_required('streetfood')
def validate_payment(order_id):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("SELECT * FROM streetfood_orders WHERE id = %s", (order_id,))
        order = cur.fetchone()
        if not order:
            flash(_t('streetfood.flash_order_not_found'), 'error')
            return redirect(url_for('streetfood.orders'))

        cur.execute("SELECT * FROM streetfood_cantines WHERE id = %s", (order['cantine_id'],))
        cantine = cur.fetchone()
        if not _can_manage(cur, cantine, session['user_id']):
            flash(_t('streetfood.flash_no_cantine_access'), 'error')
            return redirect(url_for('streetfood.home'))

        if order['payment_status'] == 'pending':
            # Money received in real life: clear the debt that was booked at order time
            cur.execute(
                "UPDATE streetfood_wallets SET balance = balance + %s WHERE cantine_id = %s AND user_id = %s",
                (order['total'], order['cantine_id'], order['user_id'])
            )
            cur.execute(
                "UPDATE streetfood_orders SET payment_status = 'paid', paid_at = %s WHERE id = %s",
                (datetime.utcnow(), order_id)
            )
            conn.commit()
            flash(_t('streetfood.flash_payment_validated'), 'success')
    except Exception as e:
        conn.rollback()
        flash(_t('streetfood.flash_order_error'), 'error')
        print(f"Validate payment error: {e}")
    finally:
        cur.close()
        conn.close()
    return redirect(url_for('streetfood.orders'))


@streetfood_bp.route('/streetfood/wallets/recharge', methods=['POST'])
@feature_required('streetfood')
def recharge_wallet():
    target_id = request.form.get('user_id', type=int)
    amount = _money(request.form.get('amount', '0'))
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cantine = _managed_cantine(cur, session['user_id'])
        if not _can_manage(cur, cantine, session['user_id']):
            flash(_t('streetfood.flash_no_cantine_access'), 'error')
            return redirect(url_for('streetfood.home'))
        if target_id and amount != 0:
            cur.execute("""
                INSERT INTO streetfood_wallets (cantine_id, user_id, balance)
                VALUES (%s, %s, %s)
                ON CONFLICT (cantine_id, user_id)
                DO UPDATE SET balance = streetfood_wallets.balance + EXCLUDED.balance
            """, (cantine['id'], target_id, amount))
            conn.commit()
            flash(_t('streetfood.flash_wallet_recharged'), 'success')
    except Exception as e:
        conn.rollback()
        flash(_t('streetfood.flash_order_error'), 'error')
        print(f"Recharge wallet error: {e}")
    finally:
        cur.close()
        conn.close()
    return redirect(url_for('streetfood.orders'))
